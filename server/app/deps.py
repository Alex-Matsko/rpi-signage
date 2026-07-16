"""Общие зависимости FastAPI: текущий пользователь UI и устройство агента."""
from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from . import config, security
from .db import get_db
from .models import Device, User


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


def current_device(request: Request, db: Session = Depends(get_db)) -> Device:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing device token")
    token_hash = security.hash_token(auth.removeprefix("Bearer ").strip())
    device = db.query(Device).filter(Device.token_hash == token_hash).first()
    if device is None:
        raise HTTPException(status_code=401, detail="invalid device token")
    return device
