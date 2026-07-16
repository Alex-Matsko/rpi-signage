from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from .. import config, security
from ..db import get_db
from ..models import User
from ..templating import templates

router = APIRouter()


@router.get("/login")
def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html", {"error": None})


@router.post("/login")
def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    ip = request.client.host if request.client else "?"
    if security.login_blocked(ip):
        return templates.TemplateResponse(
            request, "login.html",
            {"error": "Слишком много попыток входа. Подождите 5 минут."},
            status_code=429,
        )
    user = db.query(User).filter(User.username == username.strip()).first()
    if user is None or not security.verify_password(user.password_hash, password):
        security.register_login_failure(ip)
        return templates.TemplateResponse(
            request, "login.html",
            {"error": "Неверный логин или пароль."},
            status_code=401,
        )
    security.reset_login_failures(ip)
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(
        config.SESSION_COOKIE,
        security.create_session(user.id),
        max_age=config.SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
    )
    return response


@router.get("/logout")
def logout():
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(config.SESSION_COOKIE)
    return response
