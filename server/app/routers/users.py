from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy.orm import Session

from .. import security
from ..db import get_db
from ..deps import current_user
from ..models import User
from ..templating import templates
from ..utils import redirect

router = APIRouter(prefix="/users")

MIN_PASSWORD_LEN = 8


@router.get("")
def users_page(
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    users = db.query(User).order_by(User.username).all()
    return templates.TemplateResponse(request, "users.html", {
        "user": user,
        "users": users,
    })


@router.post("/create")
def create_user(
    username: str = Form(...),
    password: str = Form(...),
    user: User = Depends(current_user),
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
    db.add(User(username=username,
                password_hash=security.hash_password(password)))
    db.commit()
    return redirect("/users", msg=f"Пользователь «{username}» создан.")


@router.post("/{user_id}/password")
def change_password(
    user_id: int,
    password: str = Form(...),
    user: User = Depends(current_user),
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
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    if user_id == user.id:
        return redirect("/users", err="Нельзя удалить самого себя.")
    if db.query(User).count() <= 1:
        return redirect("/users", err="Нельзя удалить последнего пользователя.")
    target = db.get(User, user_id)
    if target is None:
        return redirect("/users", err="Пользователь не найден.")
    name = target.username
    db.delete(target)
    db.commit()
    return redirect("/users", msg=f"Пользователь «{name}» удалён.")
