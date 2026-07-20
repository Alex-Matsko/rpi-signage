"""Афиши: карточки со статусами, страница афиши, назначения на города/экраны."""
from datetime import timedelta

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy.orm import Session

from .. import config
from ..db import get_db
from ..deps import check_city_access, current_user, user_city_ids, visible_cities
from ..models import Device, PlaylistItem, Poster, PosterTarget, User, now
from ..templating import templates
from ..utils import parse_dt_local, redirect

router = APIRouter(prefix="/posters")


def visible_posters(user: User, db: Session) -> list[Poster]:
    """Менеджер видит афиши, касающиеся его городов (или созданные им)."""
    posters = (
        db.query(Poster).order_by(Poster.created_at.desc()).all()
    )
    if user.is_admin:
        return posters
    allowed = user_city_ids(user, db)
    result = []
    for p in posters:
        if p.created_by == user.id:
            result.append(p)
            continue
        for t in p.targets:
            if t.city_id in allowed or (
                t.device is not None and t.device.city_id in allowed
            ):
                result.append(p)
                break
    return result


def target_summary(poster: Poster) -> str:
    parts = [t.city.name for t in poster.targets if t.city is not None]
    n_devices = sum(1 for t in poster.targets if t.device_id is not None)
    if n_devices:
        parts.append(f"экраны: {n_devices}")
    return ", ".join(parts) if parts else "не назначена"


@router.get("")
def posters_page(
    request: Request,
    f: str = "all",
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    posters = visible_posters(user, db)
    horizon = now() + timedelta(days=config.EXPIRY_WARN_DAYS)
    counts = {
        "all": len(posters),
        "active": sum(1 for p in posters if p.status == "active"),
        "expiring": sum(
            1 for p in posters
            if p.enabled and p.expires_at and now() < p.expires_at <= horizon
        ),
        "disabled": sum(1 for p in posters if p.status == "disabled"),
    }
    if f == "active":
        posters = [p for p in posters if p.status == "active"]
    elif f == "expiring":
        posters = [
            p for p in posters
            if p.enabled and p.expires_at and now() < p.expires_at <= horizon
        ]
    elif f == "disabled":
        posters = [p for p in posters if p.status == "disabled"]
    return templates.TemplateResponse(request, "posters.html", {
        "user": user,
        "posters": posters,
        "filter": f,
        "counts": counts,
        "target_summary": target_summary,
    })


@router.get("/{poster_id}")
def poster_page(
    poster_id: int,
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    poster = db.get(Poster, poster_id)
    if poster is None:
        return redirect("/posters", err="Афиша не найдена.")
    cities = visible_cities(user, db)
    devices_by_city = {
        c.id: sorted(c.devices, key=lambda d: d.name) for c in cities
    }
    return templates.TemplateResponse(request, "poster_detail.html", {
        "user": user,
        "p": poster,
        "cities": cities,
        "devices_by_city": devices_by_city,
        "target_city_ids": {t.city_id for t in poster.targets if t.city_id},
        "target_device_ids": {t.device_id for t in poster.targets if t.device_id},
    })


@router.post("/{poster_id}/update")
def update_poster(
    poster_id: int,
    name: str = Form(...),
    display_seconds: int = Form(10),
    starts_at: str = Form(""),
    expires_at: str = Form(""),
    city: list[int] = Form([]),
    device: list[int] = Form([]),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    poster = db.get(Poster, poster_id)
    if poster is None:
        return redirect("/posters", err="Афиша не найдена.")
    poster.name = name.strip() or poster.name
    poster.display_seconds = max(1, display_seconds)
    poster.starts_at = parse_dt_local(starts_at)
    poster.expires_at = parse_dt_local(expires_at)

    # Назначения: менеджер меняет только в пределах своего города
    allowed = {c.id for c in visible_cities(user, db)}
    new_cities = {c for c in city if c in allowed}
    new_devices = {
        d.id for d in db.query(Device).filter(Device.id.in_(device or [])).all()
        if d.city_id in allowed
    }
    for t in list(poster.targets):
        if t.city_id is not None:
            if t.city_id in allowed and t.city_id not in new_cities:
                poster.targets.remove(t)
            new_cities.discard(t.city_id)
        elif t.device_id is not None:
            in_scope = t.device is not None and t.device.city_id in allowed
            if in_scope and t.device_id not in new_devices:
                poster.targets.remove(t)
            new_devices.discard(t.device_id)
    for cid in new_cities:
        poster.targets.append(PosterTarget(city_id=cid))
    for did in new_devices:
        poster.targets.append(PosterTarget(device_id=did))

    db.commit()
    return redirect(f"/posters/{poster_id}", msg="Афиша сохранена.")


@router.post("/{poster_id}/toggle")
def toggle_poster(
    poster_id: int,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    poster = db.get(Poster, poster_id)
    if poster is None:
        return redirect("/posters", err="Афиша не найдена.")
    poster.enabled = not poster.enabled
    db.commit()
    state = "включена" if poster.enabled else "выключена"
    return redirect(f"/posters/{poster_id}", msg=f"Афиша {state}.")


@router.post("/{poster_id}/delete")
def delete_poster(
    poster_id: int,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    poster = db.get(Poster, poster_id)
    if poster is None:
        return redirect("/posters", err="Афиша не найдена.")
    if not user.is_admin and poster.created_by != user.id:
        check_city_access(user, None, db)  # 403
    db.query(PlaylistItem).filter(
        PlaylistItem.poster_id == poster_id).delete()
    name = poster.name
    db.delete(poster)
    db.commit()
    return redirect("/posters", msg=f"Афиша «{name}» удалена.")
