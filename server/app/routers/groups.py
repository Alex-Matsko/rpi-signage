"""Группы устройств: операционные метки экранов (например, «только статика»,
«видео-кассы»), не привязанные к городу. CRUD и состав группы — только для
администратора: группа может объединять экраны разных городов, и менеджеру
нельзя доверить редактирование состава, иначе он мог бы случайно расширить
доступ плейлиста на чужие экраны (см. deps.group_in_scope)."""
from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import require_admin
from ..models import City, Device, DeviceGroup, DeviceGroupMember, PlaylistTarget, User
from ..templating import templates
from ..utils import redirect

router = APIRouter(prefix="/groups")


def device_groups(device: Device, db: Session) -> list[DeviceGroup]:
    """Список групп устройства — для отображения на странице экрана."""
    return (
        db.query(DeviceGroup)
        .join(DeviceGroupMember, DeviceGroupMember.group_id == DeviceGroup.id)
        .filter(DeviceGroupMember.device_id == device.id)
        .order_by(DeviceGroup.name)
        .all()
    )


@router.get("")
def groups_page(
    request: Request,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    groups = db.query(DeviceGroup).order_by(DeviceGroup.name).all()
    return templates.TemplateResponse(request, "groups.html", {
        "user": user, "groups": groups,
    })


@router.post("/create")
def create_group(
    name: str = Form(...),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    name = name.strip()
    if not name:
        return redirect("/groups", err="Укажите название группы.")
    if db.query(DeviceGroup).filter(DeviceGroup.name == name).first():
        return redirect("/groups", err=f"Группа «{name}» уже есть.")
    g = DeviceGroup(name=name)
    db.add(g)
    db.commit()
    return redirect(f"/groups/{g.id}", msg=f"Группа «{name}» создана.")


@router.post("/{group_id}/rename")
def rename_group(
    group_id: int,
    name: str = Form(...),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    g = db.get(DeviceGroup, group_id)
    if g is None:
        return redirect("/groups", err="Группа не найдена.")
    name = name.strip()
    if not name:
        return redirect("/groups", err="Укажите название группы.")
    g.name = name
    db.commit()
    return redirect("/groups", msg="Группа переименована.")


@router.post("/{group_id}/delete")
def delete_group(
    group_id: int,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    g = db.get(DeviceGroup, group_id)
    if g is None:
        return redirect("/groups", err="Группа не найдена.")
    db.query(PlaylistTarget).filter(PlaylistTarget.group_id == group_id).delete()
    name = g.name
    db.delete(g)  # cascade: DeviceGroupMember
    db.commit()
    return redirect("/groups", msg=f"Группа «{name}» удалена.")


@router.get("/{group_id}")
def group_page(
    group_id: int,
    request: Request,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    g = db.get(DeviceGroup, group_id)
    if g is None:
        return redirect("/groups", err="Группа не найдена.")
    cities = db.query(City).order_by(City.name).all()
    devices_by_city = {c.id: sorted(c.devices, key=lambda d: d.name) for c in cities}
    orphans = (
        db.query(Device).filter(Device.city_id.is_(None))
        .order_by(Device.name).all()
    )
    member_device_ids = {m.device_id for m in g.members}
    return templates.TemplateResponse(request, "group_detail.html", {
        "user": user, "g": g, "cities": cities, "devices_by_city": devices_by_city,
        "orphans": orphans, "member_device_ids": member_device_ids,
    })


@router.post("/{group_id}/members")
def update_members(
    group_id: int,
    device: list[int] = Form([]),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    g = db.get(DeviceGroup, group_id)
    if g is None:
        return redirect("/groups", err="Группа не найдена.")
    new_ids = {
        d.id for d in db.query(Device).filter(Device.id.in_(device or [])).all()
    }
    existing = {m.device_id: m for m in g.members}
    for did, m in existing.items():
        if did not in new_ids:
            g.members.remove(m)
    for did in new_ids - existing.keys():
        g.members.append(DeviceGroupMember(device_id=did))
    db.commit()
    return redirect(f"/groups/{group_id}", msg="Состав группы сохранён.")
