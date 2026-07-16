from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import current_user
from ..models import Device, DeviceGroup, Playlist, User
from ..templating import templates
from ..utils import redirect

router = APIRouter(prefix="/groups")


@router.get("")
def groups_page(
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    groups = db.query(DeviceGroup).order_by(DeviceGroup.name).all()
    playlists = db.query(Playlist).order_by(Playlist.name).all()
    return templates.TemplateResponse(request, "groups.html", {
        "user": user,
        "groups": groups,
        "playlists": playlists,
    })


@router.post("/create")
def create_group(
    name: str = Form(...),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    name = name.strip()
    if not name:
        return redirect("/groups", err="Укажите название группы.")
    if db.query(DeviceGroup).filter(DeviceGroup.name == name).first():
        return redirect("/groups", err=f"Группа «{name}» уже существует.")
    db.add(DeviceGroup(name=name))
    db.commit()
    return redirect("/groups", msg=f"Группа «{name}» создана.")


@router.post("/{group_id}/update")
def update_group(
    group_id: int,
    name: str = Form(...),
    playlist_id: int = Form(0),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    group = db.get(DeviceGroup, group_id)
    if group is None:
        return redirect("/groups", err="Группа не найдена.")
    group.name = name.strip() or group.name
    group.playlist_id = playlist_id or None
    db.commit()
    return redirect("/groups", msg=f"Группа «{group.name}» обновлена.")


@router.post("/{group_id}/delete")
def delete_group(
    group_id: int,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    group = db.get(DeviceGroup, group_id)
    if group is None:
        return redirect("/groups", err="Группа не найдена.")
    db.query(Device).filter(Device.group_id == group_id).update({"group_id": None})
    name = group.name
    db.delete(group)
    db.commit()
    return redirect("/groups", msg=f"Группа «{name}» удалена.")
