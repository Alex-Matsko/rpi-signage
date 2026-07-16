"""Публикация афиш одним процессом: файлы + расписание + города/экраны."""
import re

from fastapi import APIRouter, Depends, Form, Request, UploadFile
from sqlalchemy.orm import Session

from .. import media, worker
from ..db import get_db
from ..deps import current_user, visible_cities
from ..models import Device, MediaFile, Poster, PosterTarget, User
from ..templating import templates
from ..utils import parse_dt_local, redirect

router = APIRouter(prefix="/publish")


def parse_daily(value: str) -> str | None:
    """Проверяет время 'HH:MM' из input type=time ('' -> None)."""
    value = value.strip()
    if not value:
        return None
    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", value):
        return None
    return value


def weekdays_mask(wd: list[int]) -> int | None:
    mask = 0
    for day in wd:
        if 0 <= day <= 6:
            mask |= 1 << day
    return mask if 0 < mask < 127 else None  # все 7 дней = без ограничения


@router.get("")
def publish_page(
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    cities = visible_cities(user, db)
    devices_by_city = {
        c.id: sorted(c.devices, key=lambda d: d.name) for c in cities
    }
    return templates.TemplateResponse(request, "publish.html", {
        "user": user,
        "cities": cities,
        "devices_by_city": devices_by_city,
    })


@router.post("")
def publish(
    request: Request,
    files: list[UploadFile],
    starts_at: str = Form(""),
    expires_at: str = Form(""),
    daily_from: str = Form(""),
    daily_until: str = Form(""),
    wd: list[int] = Form([]),
    display_seconds: int = Form(10),
    city: list[int] = Form([]),
    device: list[int] = Form([]),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    allowed_city_ids = {c.id for c in visible_cities(user, db)}
    city_ids = [c for c in city if c in allowed_city_ids]
    device_ids = [
        d.id for d in db.query(Device).filter(Device.id.in_(device or [])).all()
        if d.city_id in allowed_city_ids
    ]
    if not city_ids and not device_ids:
        return redirect("/publish", err="Выберите хотя бы один город или экран.")
    if not files or all(not f.filename for f in files):
        return redirect("/publish", err="Добавьте хотя бы один файл.")

    created, transcoding, errors = [], 0, []
    to_enqueue = []
    for upload in files:
        if not upload.filename:
            continue
        try:
            attrs = media.save_upload(upload)
        except media.MediaError as e:
            errors.append(f"{upload.filename}: {e}")
            continue

        mf = db.query(MediaFile).filter(
            MediaFile.sha256 == attrs["sha256"]).first()
        if mf is None:
            mf = MediaFile(**attrs)
            if mf.kind == "video" and not mf.compatible:
                mf.transcode_status = "pending"
            db.add(mf)
            db.flush()
        if mf.transcode_status == "pending":
            transcoding += 1
            to_enqueue.append(mf.id)

        poster = Poster(
            name=(upload.filename or "Афиша").rsplit(".", 1)[0],
            media_id=mf.id,
            display_seconds=max(1, display_seconds),
            starts_at=parse_dt_local(starts_at),
            expires_at=parse_dt_local(expires_at),
            daily_from=parse_daily(daily_from),
            daily_until=parse_daily(daily_until),
            weekdays_mask=weekdays_mask(wd),
            enabled=True,
            created_by=user.id,
        )
        for cid in city_ids:
            poster.targets.append(PosterTarget(city_id=cid))
        for did in device_ids:
            poster.targets.append(PosterTarget(device_id=did))
        db.add(poster)
        created.append(poster.name)

    db.commit()
    for mid in set(to_enqueue):
        worker.enqueue(mid)

    if not created:
        return redirect("/publish", err="; ".join(errors) or "Файлы не приняты.")
    msg = f"Опубликовано афиш: {len(created)}."
    if transcoding:
        msg += f" Видео в очереди на транскодирование: {transcoding}."
    err = "; ".join(errors) if errors else None
    return redirect("/posters", msg=msg, err=err)
