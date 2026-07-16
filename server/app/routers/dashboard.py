from datetime import timedelta

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from .. import config
from ..db import get_db
from ..deps import current_user, visible_cities
from ..models import Device, Poster, User, now
from ..templating import templates

router = APIRouter()


@router.get("/")
def dashboard(
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    cities = visible_cities(user, db)
    city_ids = {c.id for c in cities}
    devices = db.query(Device).order_by(Device.name).all()
    if not user.is_admin:
        devices = [d for d in devices if d.city_id in city_ids]
    orphans = [d for d in devices if d.city_id is None] if user.is_admin else []

    t = now()
    horizon = t + timedelta(days=config.EXPIRY_WARN_DAYS)
    expiring = (
        db.query(Poster)
        .filter(Poster.enabled, Poster.expires_at > t,
                Poster.expires_at <= horizon)
        .order_by(Poster.expires_at)
        .all()
    )
    online_count = sum(
        1 for d in devices if d.is_online(config.OFFLINE_AFTER_SEC)
    )
    offline = [
        d for d in devices
        if d.token_hash is not None
        and not d.is_online(config.OFFLINE_AFTER_SEC)
    ]
    active_posters = sum(
        1 for p in db.query(Poster).all() if p.status == "active"
    )
    return templates.TemplateResponse(request, "dashboard.html", {
        "user": user,
        "cities": cities,
        "devices": devices,
        "orphans": orphans,
        "online_count": online_count,
        "offline": offline,
        "expiring": expiring,
        "active_posters": active_posters,
        "warn_days": config.EXPIRY_WARN_DAYS,
    })
