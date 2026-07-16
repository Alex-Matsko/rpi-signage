"""Общие зависимости FastAPI: пользователь UI (с ролями) и устройство агента."""
from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from . import config, security
from .db import get_db
from .models import City, Device, User


class AuthRedirect(Exception):
    """Пользователь не авторизован — редирект на /login (обрабатывается в main)."""


def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    cookie = request.cookies.get(config.SESSION_COOKIE)
    if cookie:
        uid = security.read_session(cookie)
        if uid is not None:
            user = db.get(User, uid)
            if user is not None:
                return user
    raise AuthRedirect()


def require_admin(user: User = Depends(current_user)) -> User:
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Только для администратора")
    return user


def visible_cities(user: User, db: Session) -> list[City]:
    """Города, доступные пользователю: менеджеру — только свой."""
    q = db.query(City).order_by(City.name)
    if not user.is_admin:
        q = q.filter(City.id == user.city_id)
    return q.all()


def check_city_access(user: User, city_id: int | None) -> None:
    """403, если менеджер лезет в чужой город."""
    if user.is_admin:
        return
    if city_id is None or city_id != user.city_id:
        raise HTTPException(status_code=403, detail="Чужой город")


def check_device_access(user: User, device: Device) -> None:
    if user.is_admin:
        return
    if device.city_id != user.city_id:
        raise HTTPException(status_code=403, detail="Чужой экран")


def current_device(request: Request, db: Session = Depends(get_db)) -> Device:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing device token")
    token_hash = security.hash_token(auth.removeprefix("Bearer ").strip())
    device = db.query(Device).filter(Device.token_hash == token_hash).first()
    if device is None:
        raise HTTPException(status_code=401, detail="invalid device token")
    return device
