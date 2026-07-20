"""Общие зависимости FastAPI: пользователь UI (с ролями) и устройство агента."""
from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from . import config, security
from .db import get_db
from .models import City, Device, DeviceGroup, User, UserCity


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


def user_city_ids(user: User, db: Session) -> set[int]:
    """Множество id городов, обслуживаемых менеджером (многие-ко-многим).

    Для администратора вызывающий код всегда идёт отдельной веткой
    (user.is_admin), эта функция для него не используется.
    """
    return {
        row.city_id for row in
        db.query(UserCity).filter(UserCity.user_id == user.id).all()
    }


def visible_cities(user: User, db: Session) -> list[City]:
    """Города, доступные пользователю: менеджеру — только его список."""
    q = db.query(City).order_by(City.name)
    if not user.is_admin:
        q = q.filter(City.id.in_(user_city_ids(user, db)))
    return q.all()


def check_city_access(user: User, city_id: int | None, db: Session) -> None:
    """403, если менеджер лезет в город вне своего списка."""
    if user.is_admin:
        return
    if city_id is None or city_id not in user_city_ids(user, db):
        raise HTTPException(status_code=403, detail="Чужой город")


def check_device_access(user: User, device: Device, db: Session) -> None:
    if user.is_admin:
        return
    if device.city_id is None or device.city_id not in user_city_ids(user, db):
        raise HTTPException(status_code=403, detail="Чужой экран")


def group_in_scope(user: User, group: DeviceGroup, db: Session) -> bool:
    """Группа «в зоне видимости» менеджера, только если ВСЕ её устройства —
    из его городов (группы сами по себе глобальны, могут содержать экраны
    разных городов; менеджеру нельзя назначать в плейлисте кросс-городскую
    группу, иначе контент уедет на чужие экраны)."""
    if user.is_admin:
        return True
    allowed = user_city_ids(user, db)
    return all(d.city_id in allowed for d in group.devices)


def current_device(request: Request, db: Session = Depends(get_db)) -> Device:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing device token")
    token_hash = security.hash_token(auth.removeprefix("Bearer ").strip())
    device = db.query(Device).filter(Device.token_hash == token_hash).first()
    if device is None:
        raise HTTPException(status_code=401, detail="invalid device token")
    return device
