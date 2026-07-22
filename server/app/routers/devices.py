"""Экраны (кассы), сгруппированные по городам, и управление городами."""
import asyncio

from fastapi import (
    APIRouter, Depends, Form, HTTPException, Request, WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from .. import config, security
from ..db import SessionLocal, get_db
from ..deps import (
    check_city_access, check_device_access, current_user, require_admin,
    user_city_ids, visible_cities,
)
from ..grid import LAYOUTS
from ..models import (
    City, Device, DeviceCommand, DeviceGroupMember, Playlist, PlaylistTarget,
    PosterTarget, User, UserCity,
)
from ..routers.agent import build_manifest, bundled_agent_version
from ..routers.groups import device_groups
from ..security import read_session
from ..templating import templates
from ..terminal import broker
from ..utils import redirect

router = APIRouter()

COMMAND_LABELS = {
    "resync": "Отправить афиши сейчас",
    "screenshot": "Сделать скриншот",
    "restart_agent": "Перезапустить агент",
    "reboot": "Перезагрузить Raspberry Pi",
}


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
    if not user.is_admin and city_id not in user_city_ids(user, db):
        return redirect("/screens", err="Выберите один из ваших городов.")
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
    check_device_access(user, device, db)
    recent_commands = (
        db.query(DeviceCommand)
        .filter(DeviceCommand.device_id == device_id,
                DeviceCommand.kind != "shell")
        .order_by(DeviceCommand.id.desc())
        .limit(8)
        .all()
    )
    manifest = build_manifest(device, db)
    hidden_video_count = 0
    if device.grid_layout > 1 and device.grid_images_only:
        hidden_video_count = sum(
            1 for item in manifest["items"] if item["kind"] == "video"
        )
    return templates.TemplateResponse(request, "screen_detail.html", {
        "user": user,
        "device": device,
        "manifest": manifest,
        "hidden_video_count": hidden_video_count,
        "cities": visible_cities(user, db),
        "server_agent_version": bundled_agent_version(),
        "offline_after": config.OFFLINE_AFTER_SEC,
        "recent_commands": recent_commands,
        "command_labels": COMMAND_LABELS,
        "device_groups": device_groups(device, db),
    })


@router.post("/screens/{device_id}/command")
def send_command(
    device_id: int,
    kind: str = Form(...),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    device = db.get(Device, device_id)
    if device is None:
        return redirect("/screens", err="Экран не найден.")
    check_device_access(user, device, db)
    if kind not in COMMAND_LABELS:
        return redirect(f"/screens/{device_id}", err="Неизвестная команда.")
    if device.token_hash is None:
        return redirect(f"/screens/{device_id}",
                        err="Экран ещё не подключён.")
    db.add(DeviceCommand(device_id=device_id, kind=kind, created_by=user.id))
    db.commit()
    return redirect(f"/screens/{device_id}",
                    msg=f"Команда «{COMMAND_LABELS[kind]}» отправлена на экран.")


@router.get("/screens/{device_id}/screenshot.png")
def screenshot(
    device_id: int,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    device = db.get(Device, device_id)
    if device is None:
        raise HTTPException(status_code=404)
    check_device_access(user, device, db)
    path = config.SHOT_DIR / f"{device_id}.png"
    if not path.exists():
        raise HTTPException(status_code=404)
    return FileResponse(path, media_type="image/png",
                        headers={"Cache-Control": "no-store"})


@router.get("/screens/{device_id}/terminal")
def terminal_page(
    device_id: int,
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    device = db.get(Device, device_id)
    if device is None:
        return redirect("/screens", err="Экран не найден.")
    check_device_access(user, device, db)
    return templates.TemplateResponse(request, "terminal.html", {
        "user": user,
        "device": device,
        "online": device.is_online(config.OFFLINE_AFTER_SEC),
    })


def _ws_user(websocket: WebSocket, db: Session) -> User | None:
    cookie = websocket.cookies.get(config.SESSION_COOKIE)
    if not cookie:
        return None
    uid = read_session(cookie)
    return db.get(User, uid) if uid is not None else None


@router.websocket("/screens/{device_id}/terminal/ws")
async def terminal_ws(websocket: WebSocket, device_id: int):
    """Мост браузер⇄агент: keystrokes вниз, вывод pty вверх."""
    await websocket.accept()
    db = SessionLocal()
    try:
        user = _ws_user(websocket, db)
        device = db.get(Device, device_id)
        if user is None or device is None:
            await websocket.close(code=4401)
            return
        if not user.is_admin and (
            device.city_id is None or
            device.city_id not in user_city_ids(user, db)
        ):
            await websocket.close(code=4403)
            return
        if device.token_hash is None:
            await websocket.close(code=4404)
            return
        session = broker.open(device_id)
        db.add(DeviceCommand(device_id=device_id, kind="shell",
                             param=session.id, created_by=user.id))
        db.commit()
    finally:
        db.close()

    async def pump_to_agent():
        try:
            while True:
                text = await websocket.receive_text()
                session.browser_send(text.encode("utf-8", "replace"))
        except WebSocketDisconnect:
            pass
        finally:
            broker.close(session.id)

    async def pump_to_browser():
        sent = 0
        while True:
            data, closed = await asyncio.to_thread(
                session.browser_wait_output, sent, 20.0)
            if data:
                sent += len(data)
                await websocket.send_bytes(data)
            if closed:
                break

    reader = asyncio.create_task(pump_to_agent())
    writer = asyncio.create_task(pump_to_browser())
    try:
        await asyncio.wait({reader, writer},
                           return_when=asyncio.FIRST_COMPLETED)
    finally:
        broker.close(session.id)
        reader.cancel()
        writer.cancel()


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
    check_device_access(user, device, db)
    device.name = name.strip() or device.name
    if user.is_admin:
        device.city_id = city_id or None
    db.commit()
    return redirect(f"/screens/{device_id}", msg="Настройки экрана сохранены.")


@router.post("/screens/{device_id}/display")
def update_display(
    device_id: int,
    orientation: str = Form("landscape"),
    grid_layout: int = Form(1),
    grid_images_only: str | None = Form(None),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    device = db.get(Device, device_id)
    if device is None:
        return redirect("/screens", err="Экран не найден.")
    check_device_access(user, device, db)
    if orientation not in ("landscape", "portrait"):
        return redirect(f"/screens/{device_id}", err="Некорректная ориентация.")
    if grid_layout not in LAYOUTS:
        return redirect(f"/screens/{device_id}", err="Некорректная раскладка.")
    device.orientation = orientation
    device.grid_layout = grid_layout
    device.grid_images_only = grid_images_only is not None
    db.commit()
    return redirect(f"/screens/{device_id}", msg="Раскладка экрана сохранена.")


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
    check_device_access(user, device, db)
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
    check_device_access(user, device, db)
    db.query(PosterTarget).filter(
        PosterTarget.device_id == device_id).delete()
    db.query(PlaylistTarget).filter(
        PlaylistTarget.device_id == device_id).delete()
    db.query(DeviceGroupMember).filter(
        DeviceGroupMember.device_id == device_id).delete()
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
    if db.query(Playlist).filter(Playlist.city_id == city_id).count():
        return redirect(
            "/screens",
            err=f"В городе «{city.name}» есть плейлисты — сначала удалите их.",
        )
    db.query(PosterTarget).filter(PosterTarget.city_id == city_id).delete()
    db.query(PlaylistTarget).filter(PlaylistTarget.city_id == city_id).delete()
    db.query(UserCity).filter(UserCity.city_id == city_id).delete()
    db.query(User).filter(User.city_id == city_id).update({"city_id": None})
    name = city.name
    db.delete(city)
    db.commit()
    return redirect("/screens", msg=f"Город «{name}» удалён.")
