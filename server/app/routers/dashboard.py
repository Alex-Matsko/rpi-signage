from datetime import timedelta

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from .. import config
from ..db import get_db
from ..deps import current_user
from ..models import Device, Poster, User, now
from ..templating import templates

router = APIRouter()


@router.get("/")
def dashboard(
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    devices = db.query(Device).order_by(Device.name).all()
    t = now()
    horizon = t + timedelta(days=config.EXPIRY_WARN_DAYS)
    expiring = (
        db.query(Poster)
        .filter(Poster.enabled, Poster.expires_at > t, Poster.expires_at <= horizon)
        .order_by(Poster.expires_at)
        .all()
    )
    offline = [d for d in devices if not d.is_online(config.OFFLINE_AFTER_SEC)]
    incomplete = [
        d for d in devices
        if d.cache_total and (d.cache_done or 0) < d.cache_total
    ]
    return templates.TemplateResponse(request, "dashboard.html", {
        "user": user,
        "devices": devices,
        "expiring": expiring,
        "offline": offline,
        "incomplete": incomplete,
        "warn_days": config.EXPIRY_WARN_DAYS,
    })
