from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy.orm import Session

from .. import config, security
from ..db import get_db
from ..deps import current_user
from ..models import Device, DeviceGroup, Playlist, User
from ..routers.agent import build_manifest
from ..templating import templates
from ..utils import redirect

router = APIRouter(prefix="/devices")


def _common(db: Session) -> dict:
    return {
        "groups": db.query(DeviceGroup).order_by(DeviceGroup.name).all(),
        "playlists": db.query(Playlist).order_by(Playlist.name).all(),
        "offline_after": config.OFFLINE_AFTER_SEC,
    }


@router.get("")
def devices_page(
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    devices = db.query(Device).order_by(Device.name).all()
    return templates.TemplateResponse(request, "devices.html", {
        "user": user,
        "devices": devices,
        **_common(db),
    })


@router.post("/create")
def create_device(
    name: str = Form(...),
    group_id: int = Form(0),
    playlist_id: int = Form(0),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    name = name.strip()
    if not name:
        return redirect("/devices", err="Укажите название экрана.")
    device = Device(
        name=name,
        group_id=group_id or None,
        playlist_id=playlist_id or None,
        pairing_code=security.new_pairing_code(),
    )
    db.add(device)
    db.commit()
    return redirect(
        f"/devices/{device.id}",
        msg=f"Экран «{name}» создан. Код подключения: {device.pairing_code}",
    )


@router.get("/{device_id}")
def device_page(
    device_id: int,
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    device = db.get(Device, device_id)
    if device is None:
        return redirect("/devices", err="Экран не найден.")
    return templates.TemplateResponse(request, "device_detail.html", {
        "user": user,
        "device": device,
        "manifest": build_manifest(device),
        **_common(db),
    })


@router.post("/{device_id}/update")
def update_device(
    device_id: int,
    name: str = Form(...),
    group_id: int = Form(0),
    playlist_id: int = Form(0),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    device = db.get(Device, device_id)
    if device is None:
        return redirect("/devices", err="Экран не найден.")
    device.name = name.strip() or device.name
    device.group_id = group_id or None
    device.playlist_id = playlist_id or None
    db.commit()
    return redirect(f"/devices/{device_id}", msg="Настройки экрана сохранены.")


@router.post("/{device_id}/repair")
def repair_device(
    device_id: int,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    """Выпускает новый код подключения; старый токен агента отзывается."""
    device = db.get(Device, device_id)
    if device is None:
        return redirect("/devices", err="Экран не найден.")
    device.pairing_code = security.new_pairing_code()
    device.token_hash = None
    db.commit()
    return redirect(
        f"/devices/{device_id}",
        msg=f"Новый код подключения: {device.pairing_code}. "
            "Старый токен агента отозван.",
    )


@router.post("/{device_id}/delete")
def delete_device(
    device_id: int,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    device = db.get(Device, device_id)
    if device is None:
        return redirect("/devices", err="Экран не найден.")
    name = device.name
    db.delete(device)
    db.commit()
    return redirect("/devices", msg=f"Экран «{name}» удалён.")
