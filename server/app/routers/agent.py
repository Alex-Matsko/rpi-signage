"""API для агентов на Raspberry Pi. Авторизация — Bearer-токен устройства."""
import hashlib
import json
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import config, media, security
from ..db import get_db
from ..deps import current_device
from ..models import Device, MediaFile, Playlist, now

router = APIRouter(prefix="/api/agent")


def build_manifest(device: Device) -> dict:
    """Собирает манифест устройства: элементы действующего плейлиста.

    Истекшие афиши исключаются; будущие (starts_at впереди) включаются,
    чтобы агент начал их показывать вовремя даже без связи с сервером.
    """
    items = []
    playlist: Playlist | None = device.effective_playlist
    if playlist is not None:
        t = now()
        for item in playlist.items:
            poster = item.poster
            if not poster.enabled:
                continue
            if poster.expires_at and poster.expires_at <= t:
                continue
            m = poster.media
            items.append({
                "poster_id": poster.id,
                "name": poster.name,
                "kind": m.kind,
                "mime": m.mime,
                "sha256": m.sha256,
                "size": m.size_bytes,
                "url": f"/api/agent/media/{m.sha256}",
                "duration": (
                    poster.display_seconds if m.kind == "image"
                    else m.duration_sec
                ),
                "starts_at": poster.starts_at.isoformat() if poster.starts_at else None,
                "expires_at": poster.expires_at.isoformat() if poster.expires_at else None,
            })
    version = hashlib.sha256(
        json.dumps(items, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()[:16]
    return {
        "manifest_version": version,
        "poll_interval": config.POLL_INTERVAL,
        "generated_at": now().isoformat(),
        "items": items,
    }


class RegisterIn(BaseModel):
    code: str


@router.post("/register")
def register(payload: RegisterIn, db: Session = Depends(get_db)):
    code = payload.code.strip().upper()
    device = db.query(Device).filter(Device.pairing_code == code).first()
    if device is None:
        raise HTTPException(status_code=404, detail="invalid pairing code")
    token, token_hash = security.new_device_token()
    device.token_hash = token_hash
    device.pairing_code = None
    db.commit()
    return {
        "token": token,
        "device_id": device.id,
        "name": device.name,
        "poll_interval": config.POLL_INTERVAL,
    }


@router.get("/manifest")
def manifest(device: Device = Depends(current_device)):
    return build_manifest(device)


@router.get("/media/{sha256}")
def download_media(
    sha256: str,
    device: Device = Depends(current_device),
    db: Session = Depends(get_db),
):
    mf = db.query(MediaFile).filter(MediaFile.sha256 == sha256).first()
    path = media.media_path(sha256)
    if mf is None or not path.exists():
        raise HTTPException(status_code=404, detail="media not found")
    return FileResponse(path, media_type=mf.mime, filename=mf.orig_name)


class CurrentIn(BaseModel):
    name: str | None = None
    sha256: str | None = None
    since: datetime | None = None


class StatusIn(BaseModel):
    agent_version: str | None = None
    uptime_sec: int | None = None
    temp_c: float | None = None
    disk_free_mb: int | None = None
    cache_done: int | None = None
    cache_total: int | None = None
    current: CurrentIn | None = None


@router.post("/status")
def status(
    payload: StatusIn,
    device: Device = Depends(current_device),
    db: Session = Depends(get_db),
):
    device.last_seen_at = now()
    device.agent_version = payload.agent_version
    device.uptime_sec = payload.uptime_sec
    device.temp_c = payload.temp_c
    device.disk_free_mb = payload.disk_free_mb
    device.cache_done = payload.cache_done
    device.cache_total = payload.cache_total
    if payload.current is not None:
        device.current_name = payload.current.name
        device.current_sha256 = payload.current.sha256
        since = payload.current.since
        device.current_since = since.replace(tzinfo=None) if since else None
    else:
        device.current_name = None
        device.current_sha256 = None
        device.current_since = None
    db.commit()
    # Версия манифеста в ответе позволяет агенту заметить изменения раньше
    # планового опроса.
    return {"ok": True, "manifest_version": build_manifest(device)["manifest_version"]}
