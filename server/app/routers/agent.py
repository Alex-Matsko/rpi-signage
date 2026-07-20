"""API для агентов на Raspberry Pi. Авторизация — Bearer-токен устройства."""
import functools
import hashlib
import json
import os
import re
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from sqlalchemy import or_

from .. import config, media, security
from ..db import get_db
from ..deps import current_device
from ..models import (
    Device, DeviceCommand, DeviceGroupMember, MediaFile, Playlist,
    PlaylistTarget, Poster, PosterTarget, now,
)
from ..terminal import broker

router = APIRouter(prefix="/api/agent")

# Максимальное время удержания длинного опроса команд, сек
COMMAND_POLL_TIMEOUT = 25


@functools.lru_cache(maxsize=1)
def bundled_agent_version() -> str | None:
    """Версия agent.py, который раздаёт этот сервер (для self-update агентов)."""
    agent_dir = Path(
        os.environ.get(
            "SIGNAGE_AGENT_DIR",
            Path(__file__).resolve().parents[3] / "agent",
        )
    )
    try:
        source = (agent_dir / "agent.py").read_text()
        match = re.search(r'^AGENT_VERSION\s*=\s*"([^"]+)"', source, re.M)
        return match.group(1) if match else None
    except OSError:
        return None


def _device_group_ids(device: Device, db: Session) -> set[int]:
    return {
        row.group_id for row in
        db.query(DeviceGroupMember).filter(
            DeviceGroupMember.device_id == device.id).all()
    }


def device_posters(device: Device, db: Session) -> list[Poster]:
    """Афиши, назначенные экрану: напрямую/через город (как раньше) — плюс
    достижимые через включённые плейлисты (город/экран/группа плейлиста).

    Прямой путь через PosterTarget не меняется и идёт первым; плейлисты —
    дополнительный, необязательный способ назначения поверх него. Одна и та
    же афиша, доступная и напрямую, и через плейлист, не дублируется.
    """
    direct_conditions = [PosterTarget.device_id == device.id]
    if device.city_id is not None:
        direct_conditions.append(PosterTarget.city_id == device.city_id)
    direct = (
        db.query(Poster)
        .join(PosterTarget)
        .filter(or_(*direct_conditions))
        .order_by(Poster.sort_order, Poster.created_at, Poster.id)
        .distinct()
        .all()
    )
    seen = {p.id for p in direct}

    group_ids = _device_group_ids(device, db)
    pl_conditions = [PlaylistTarget.device_id == device.id]
    if device.city_id is not None:
        pl_conditions.append(PlaylistTarget.city_id == device.city_id)
    if group_ids:
        pl_conditions.append(PlaylistTarget.group_id.in_(group_ids))

    playlists = (
        db.query(Playlist)
        .join(PlaylistTarget)
        .filter(Playlist.enabled.is_(True))
        .filter(or_(*pl_conditions))
        .distinct()
        .all()
    )
    via_playlist = []
    for pl in sorted(playlists, key=lambda p: (p.created_at, p.id)):
        for item in pl.items:  # relationship уже упорядочен по position
            if item.poster_id in seen:
                continue
            seen.add(item.poster_id)
            via_playlist.append(item.poster)

    return direct + via_playlist


def build_manifest(device: Device, db: Session) -> dict:
    """Собирает манифест устройства: афиши его города и назначенные лично.

    Истекшие афиши исключаются; будущие (starts_at впереди) включаются,
    чтобы агент начал их показывать вовремя даже без связи с сервером.
    """
    items = []
    t = now()
    for poster in device_posters(device, db):
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
            "daily_from": poster.daily_from or None,
            "daily_until": poster.daily_until or None,
            "weekdays": poster.weekdays_mask or None,
        })
    version = hashlib.sha256(
        json.dumps(items, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()[:16]
    return {
        "manifest_version": version,
        "poll_interval": config.POLL_INTERVAL,
        "generated_at": now().isoformat(),
        "agent_version": bundled_agent_version(),
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
def manifest(
    device: Device = Depends(current_device),
    db: Session = Depends(get_db),
):
    return build_manifest(device, db)


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
    local_ip: str | None = None
    web_port: int | None = None


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
    device.local_ip = payload.local_ip
    device.web_port = payload.web_port
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
    return {
        "ok": True,
        "manifest_version": build_manifest(device, db)["manifest_version"],
        "has_commands": db.query(DeviceCommand).filter(
            DeviceCommand.device_id == device.id,
            DeviceCommand.status == "pending",
        ).count() > 0,
    }


# ------------------------------------------------ команды управления экраном

def _pending_commands(device: Device, db: Session) -> list[dict]:
    cmds = (
        db.query(DeviceCommand)
        .filter(DeviceCommand.device_id == device.id,
                DeviceCommand.status == "pending")
        .order_by(DeviceCommand.id)
        .all()
    )
    return [{"id": c.id, "kind": c.kind, "param": c.param} for c in cmds]


@router.get("/commands")
def get_commands(
    device: Device = Depends(current_device),
    db: Session = Depends(get_db),
):
    """Долгий опрос: агент ждёт появления команд, чтобы реагировать быстро."""
    import time
    deadline = time.monotonic() + COMMAND_POLL_TIMEOUT
    while True:
        cmds = _pending_commands(device, db)
        if cmds or time.monotonic() >= deadline:
            device.last_seen_at = now()
            db.commit()
            return {"commands": cmds}
        time.sleep(1.0)
        db.expire_all()


class CommandResultIn(BaseModel):
    status: str  # done | failed
    result: str | None = None


@router.post("/commands/{command_id}/result")
def command_result(
    command_id: int,
    payload: CommandResultIn,
    device: Device = Depends(current_device),
    db: Session = Depends(get_db),
):
    cmd = db.get(DeviceCommand, command_id)
    if cmd is None or cmd.device_id != device.id:
        raise HTTPException(status_code=404, detail="command not found")
    cmd.status = "done" if payload.status == "done" else "failed"
    cmd.result = (payload.result or "")[:4000]
    cmd.done_at = now()
    db.commit()
    return {"ok": True}


@router.post("/screenshot")
async def upload_screenshot(
    request: Request,
    device: Device = Depends(current_device),
    db: Session = Depends(get_db),
):
    """Агент присылает PNG-кадр текущего экрана (сырое тело запроса)."""
    data = await request.body()
    if not data or len(data) > config.MAX_SCREENSHOT_MB * 1024 * 1024:
        raise HTTPException(status_code=400, detail="bad screenshot")
    path = config.SHOT_DIR / f"{device.id}.png"
    path.write_bytes(data)
    device.screenshot_at = now()
    db.commit()
    return {"ok": True}


# ------------------------------------------------ веб-терминал (сторона агента)

@router.get("/term/{session_id}/input")
def term_input(
    session_id: str,
    after: int = 0,
    device: Device = Depends(current_device),
):
    session = broker.get(session_id)
    if session is None or session.device_id != device.id:
        raise HTTPException(status_code=404, detail="session not found")
    data, closed = session.agent_wait_input(after, timeout=20.0)
    return {"data": data.decode("latin1"), "closed": closed,
            "consumed": after + len(data)}


@router.post("/term/{session_id}/output")
async def term_output(
    session_id: str,
    request: Request,
    device: Device = Depends(current_device),
):
    session = broker.get(session_id)
    if session is None or session.device_id != device.id:
        raise HTTPException(status_code=404, detail="session not found")
    session.agent_send(await request.body())
    return Response(status_code=204)


@router.post("/term/{session_id}/close")
def term_close(
    session_id: str,
    device: Device = Depends(current_device),
):
    session = broker.get(session_id)
    if session is not None and session.device_id == device.id:
        broker.close(session_id)
    return {"ok": True}
