"""Группы устройств: операционные метки экранов (например, «только статика»,
«видео-кассы»). Группа не привязана к городу и в принципе может объединять
экраны разных городов, но менеджер видит и редактирует только группы,
целиком состоящие из устройств ЕГО городов (см. deps.group_in_scope) —
и может добавлять в такую группу только свои же устройства. Админ работает
без ограничений."""
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import current_user, group_in_scope, user_city_ids, visible_cities
from ..models import Device, DeviceGroup, DeviceGroupMember, PlaylistTarget, User
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


def visible_groups(user: User, db: Session) -> list[DeviceGroup]:
    """Админ видит все группы; менеджер — только те, что целиком состоят
    из устройств его городов."""
    groups = db.query(DeviceGroup).order_by(DeviceGroup.name).all()
    if user.is_admin:
        return groups
    return [g for g in groups if group_in_scope(user, g, db)]


def _check_group_access(user: User, group: DeviceGroup, db: Session) -> None:
    if user.is_admin:
        return
    if not group_in_scope(user, group, db):
        raise HTTPException(status_code=403, detail="Группа вне ваших городов")


@router.get("")
def groups_page(
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    groups = visible_groups(user, db)
    return templates.TemplateResponse(request, "groups.html", {
        "user": user, "groups": groups,
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
        return redirect("/groups", err=f"Группа «{name}» уже есть.")
    g = DeviceGroup(name=name)
    db.add(g)
    db.commit()
    return redirect(f"/groups/{g.id}", msg=f"Группа «{name}» создана.")


@router.post("/{group_id}/rename")
def rename_group(
    group_id: int,
    name: str = Form(...),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    g = db.get(DeviceGroup, group_id)
    if g is None:
        return redirect("/groups", err="Группа не найдена.")
    _check_group_access(user, g, db)
    name = name.strip()
    if not name:
        return redirect("/groups", err="Укажите название группы.")
    g.name = name
    db.commit()
    return redirect("/groups", msg="Группа переименована.")


@router.post("/{group_id}/delete")
def delete_group(
    group_id: int,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    g = db.get(DeviceGroup, group_id)
    if g is None:
        return redirect("/groups", err="Группа не найдена.")
    _check_group_access(user, g, db)
    db.query(PlaylistTarget).filter(PlaylistTarget.group_id == group_id).delete()
    name = g.name
    db.delete(g)  # cascade: DeviceGroupMember
    db.commit()
    return redirect("/groups", msg=f"Группа «{name}» удалена.")


@router.get("/{group_id}")
def group_page(
    group_id: int,
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    g = db.get(DeviceGroup, group_id)
    if g is None:
        return redirect("/groups", err="Группа не найдена.")
    _check_group_access(user, g, db)
    cities = visible_cities(user, db)
    devices_by_city = {c.id: sorted(c.devices, key=lambda d: d.name) for c in cities}
    orphans = (
        db.query(Device).filter(Device.city_id.is_(None))
        .order_by(Device.name).all()
        if user.is_admin else []
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
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    g = db.get(DeviceGroup, group_id)
    if g is None:
        return redirect("/groups", err="Группа не найдена.")
    _check_group_access(user, g, db)
    candidates = db.query(Device).filter(Device.id.in_(device or [])).all()
    if user.is_admin:
        new_ids = {d.id for d in candidates}
    else:
        # Менеджер может добавлять в группу только устройства своих городов;
        # доступ к самой группе уже проверен выше (_check_group_access), так
        # что все СУЩЕСТВУЮЩИЕ участники и так гарантированно в его зоне.
        allowed = user_city_ids(user, db)
        new_ids = {d.id for d in candidates if d.city_id in allowed}
    existing = {m.device_id: m for m in g.members}
    for did, m in existing.items():
        if did not in new_ids:
            g.members.remove(m)
    for did in new_ids - existing.keys():
        g.members.append(DeviceGroupMember(device_id=did))
    db.commit()
    return redirect(f"/groups/{group_id}", msg="Состав группы сохранён.")
