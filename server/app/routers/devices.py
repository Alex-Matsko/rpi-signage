"""Экраны (кассы), сгруппированные по городам, и управление городами."""
from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy.orm import Session

from .. import config, security
from ..db import get_db
from ..deps import (
    check_city_access, check_device_access, current_user, require_admin,
    visible_cities,
)
from ..models import City, Device, PosterTarget, User
from ..routers.agent import build_manifest, bundled_agent_version
from ..templating import templates
from ..utils import redirect

router = APIRouter()


@router.get("/screens")
def screens_page(
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    cities = visible_cities(user, db)
    orphans = (
        db.query(Device).filter(Device.city_id.is_(None))
        .order_by(Device.name).all()
        if user.is_admin else []
    )
    return templates.TemplateResponse(request, "screens.html", {
        "user": user,
        "cities": cities,
        "orphans": orphans,
    })


@router.post("/screens/create")
def create_screen(
    name: str = Form(...),
    city_id: int = Form(0),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    name = name.strip()
    if not name:
        return redirect("/screens", err="Укажите название экрана.")
    if not user.is_admin:
        city_id = user.city_id or 0
    if city_id and db.get(City, city_id) is None:
        return redirect("/screens", err="Город не найден.")
    device = Device(
        name=name,
        city_id=city_id or None,
        pairing_code=security.new_pairing_code(),
    )
    db.add(device)
    db.commit()
    return redirect(
        f"/screens/{device.id}",
        msg=f"Экран «{name}» создан. Код подключения: {device.pairing_code}",
    )


@router.get("/screens/{device_id}")
def screen_page(
    device_id: int,
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    device = db.get(Device, device_id)
    if device is None:
        return redirect("/screens", err="Экран не найден.")
    check_device_access(user, device)
    return templates.TemplateResponse(request, "screen_detail.html", {
        "user": user,
        "device": device,
        "manifest": build_manifest(device, db),
        "cities": visible_cities(user, db),
        "server_agent_version": bundled_agent_version(),
        "offline_after": config.OFFLINE_AFTER_SEC,
    })


@router.post("/screens/{device_id}/update")
def update_screen(
    device_id: int,
    name: str = Form(...),
    city_id: int = Form(0),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    device = db.get(Device, device_id)
    if device is None:
        return redirect("/screens", err="Экран не найден.")
    check_device_access(user, device)
    device.name = name.strip() or device.name
    if user.is_admin:
        device.city_id = city_id or None
    db.commit()
    return redirect(f"/screens/{device_id}", msg="Настройки экрана сохранены.")


@router.post("/screens/{device_id}/repair")
def repair_screen(
    device_id: int,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    """Выпускает новый код подключения; старый токен агента отзывается."""
    device = db.get(Device, device_id)
    if device is None:
        return redirect("/screens", err="Экран не найден.")
    check_device_access(user, device)
    device.pairing_code = security.new_pairing_code()
    device.token_hash = None
    db.commit()
    return redirect(
        f"/screens/{device_id}",
        msg=f"Новый код подключения: {device.pairing_code}. "
            "Старый токен агента отозван.",
    )


@router.post("/screens/{device_id}/delete")
def delete_screen(
    device_id: int,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    device = db.get(Device, device_id)
    if device is None:
        return redirect("/screens", err="Экран не найден.")
    check_device_access(user, device)
    db.query(PosterTarget).filter(
        PosterTarget.device_id == device_id).delete()
    name = device.name
    db.delete(device)
    db.commit()
    return redirect("/screens", msg=f"Экран «{name}» удалён.")


# ---------------------------------------------------------------- города

@router.post("/cities/create")
def create_city(
    name: str = Form(...),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    name = name.strip()
    if not name:
        return redirect("/screens", err="Укажите название города.")
    if db.query(City).filter(City.name == name).first():
        return redirect("/screens", err=f"Город «{name}» уже есть.")
    db.add(City(name=name))
    db.commit()
    return redirect("/screens", msg=f"Город «{name}» добавлен.")


@router.post("/cities/{city_id}/rename")
def rename_city(
    city_id: int,
    name: str = Form(...),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    city = db.get(City, city_id)
    if city is None:
        return redirect("/screens", err="Город не найден.")
    city.name = name.strip() or city.name
    db.commit()
    return redirect("/screens", msg="Город переименован.")


@router.post("/cities/{city_id}/delete")
def delete_city(
    city_id: int,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    city = db.get(City, city_id)
    if city is None:
        return redirect("/screens", err="Город не найден.")
    if city.devices:
        return redirect(
            "/screens",
            err=f"В городе «{city.name}» есть экраны — сначала перенесите их.",
        )
    db.query(PosterTarget).filter(PosterTarget.city_id == city_id).delete()
    db.query(User).filter(User.city_id == city_id).update({"city_id": None})
    name = city.name
    db.delete(city)
    db.commit()
    return redirect("/screens", msg=f"Город «{name}» удалён.")
