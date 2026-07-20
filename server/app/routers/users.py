"""Пользователи: администраторы и менеджеры городов. Только для админа."""
from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy.orm import Session

from .. import security
from ..db import get_db
from ..deps import require_admin
from ..models import City, ROLE_ADMIN, ROLE_MANAGER, User, UserCity
from ..templating import templates
from ..utils import redirect

router = APIRouter(prefix="/users")

MIN_PASSWORD_LEN = 8


@router.get("")
def users_page(
    request: Request,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    users = db.query(User).order_by(User.username).all()
    cities = db.query(City).order_by(City.name).all()
    return templates.TemplateResponse(request, "users.html", {
        "user": user,
        "users": users,
        "cities": cities,
    })


@router.post("/create")
def create_user(
    username: str = Form(...),
    password: str = Form(...),
    role: str = Form(ROLE_ADMIN),
    city_id: list[int] = Form([]),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    username = username.strip()
    if not username:
        return redirect("/users", err="Укажите логин.")
    if len(password) < MIN_PASSWORD_LEN:
        return redirect(
            "/users", err=f"Пароль короче {MIN_PASSWORD_LEN} символов."
        )
    if db.query(User).filter(User.username == username).first():
        return redirect("/users", err=f"Пользователь «{username}» уже есть.")
    is_manager = role == ROLE_MANAGER
    valid_ids = [c for c in city_id if db.get(City, c) is not None]
    if is_manager and not valid_ids:
        return redirect("/users", err="Менеджеру нужно назначить хотя бы один город.")
    new_user = User(
        username=username,
        password_hash=security.hash_password(password),
        role=ROLE_MANAGER if is_manager else ROLE_ADMIN,
        city_id=(valid_ids[0] if is_manager else None),  # подсказка осн. города
    )
    if is_manager:
        new_user.city_links = [UserCity(city_id=c) for c in valid_ids]
    db.add(new_user)
    db.commit()
    return redirect("/users", msg=f"Пользователь «{username}» создан.")


@router.post("/{user_id}/cities")
def update_user_cities(
    user_id: int,
    city_id: list[int] = Form([]),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    target = db.get(User, user_id)
    if target is None:
        return redirect("/users", err="Пользователь не найден.")
    if target.is_admin:
        return redirect("/users", err="У администратора нет привязки к городу.")
    valid_ids = [c for c in city_id if db.get(City, c) is not None]
    if not valid_ids:
        return redirect("/users", err="Менеджеру нужен хотя бы один город.")
    target.city_links = [UserCity(city_id=c) for c in valid_ids]
    target.city_id = valid_ids[0]  # подсказка осн. города — держим в синхроне
    db.commit()
    return redirect("/users", msg=f"Города «{target.username}» обновлены.")


@router.post("/{user_id}/password")
def change_password(
    user_id: int,
    password: str = Form(...),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    target = db.get(User, user_id)
    if target is None:
        return redirect("/users", err="Пользователь не найден.")
    if len(password) < MIN_PASSWORD_LEN:
        return redirect(
            "/users", err=f"Пароль короче {MIN_PASSWORD_LEN} символов."
        )
    target.password_hash = security.hash_password(password)
    db.commit()
    return redirect("/users", msg=f"Пароль «{target.username}» изменён.")


@router.post("/{user_id}/delete")
def delete_user(
    user_id: int,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    if user_id == user.id:
        return redirect("/users", err="Нельзя удалить самого себя.")
    target = db.get(User, user_id)
    if target is None:
        return redirect("/users", err="Пользователь не найден.")
    if target.is_admin and db.query(User).filter(
        User.role == ROLE_ADMIN
    ).count() <= 1:
        return redirect("/users", err="Нельзя удалить последнего администратора.")
    name = target.username
    db.delete(target)
    db.commit()
    return redirect("/users", msg=f"Пользователь «{name}» удалён.")
