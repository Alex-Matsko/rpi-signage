"""Плейлисты: именованные упорядоченные наборы существующих афиш,
назначаемые на город/экран/группу устройств — дополнительный, необязательный
слой поверх прямого назначения афиш (PosterTarget), которое продолжает
работать независимо."""
from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import (
    check_city_access, current_user, group_in_scope, user_city_ids,
    visible_cities,
)
from ..models import Device, DeviceGroup, Playlist, PlaylistItem, PlaylistTarget, User
from ..templating import templates
from ..utils import redirect
from .posters import visible_posters

router = APIRouter(prefix="/playlists")


def visible_playlists(user: User, db: Session) -> list[Playlist]:
    """Менеджер видит свои плейлисты (created_by) или плейлисты своих городов."""
    playlists = db.query(Playlist).order_by(Playlist.created_at.desc()).all()
    if user.is_admin:
        return playlists
    allowed = user_city_ids(user, db)
    return [
        pl for pl in playlists
        if pl.created_by == user.id or pl.city_id in allowed
    ]


def target_summary(playlist: Playlist) -> str:
    parts = [t.city.name for t in playlist.targets if t.city is not None]
    n_dev = sum(1 for t in playlist.targets if t.device_id is not None)
    if n_dev:
        parts.append(f"экраны: {n_dev}")
    n_grp = sum(1 for t in playlist.targets if t.group_id is not None)
    if n_grp:
        parts.append(f"группы: {n_grp}")
    return ", ".join(parts) if parts else "не назначен"


def _check_playlist_access(user: User, playlist: Playlist, db: Session) -> None:
    if user.is_admin or playlist.created_by == user.id:
        return
    check_city_access(user, playlist.city_id, db)


@router.get("")
def playlists_page(
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    playlists = visible_playlists(user, db)
    return templates.TemplateResponse(request, "playlists.html", {
        "user": user, "playlists": playlists, "target_summary": target_summary,
        "cities": visible_cities(user, db),
    })


@router.post("/create")
def create_playlist(
    name: str = Form(...),
    city_id: int = Form(...),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    name = name.strip()
    if not name:
        return redirect("/playlists", err="Укажите название плейлиста.")
    allowed = {c.id for c in visible_cities(user, db)}
    if city_id not in allowed:
        return redirect("/playlists", err="Выберите доступный город.")
    pl = Playlist(name=name, city_id=city_id, created_by=user.id)
    db.add(pl)
    db.commit()
    return redirect(f"/playlists/{pl.id}", msg=f"Плейлист «{name}» создан.")


@router.get("/{playlist_id}")
def playlist_page(
    playlist_id: int,
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    pl = db.get(Playlist, playlist_id)
    if pl is None:
        return redirect("/playlists", err="Плейлист не найден.")
    _check_playlist_access(user, pl, db)
    cities = visible_cities(user, db)
    devices_by_city = {c.id: sorted(c.devices, key=lambda d: d.name) for c in cities}
    all_groups = db.query(DeviceGroup).order_by(DeviceGroup.name).all()
    groups = [g for g in all_groups if group_in_scope(user, g, db)]
    in_playlist = {item.poster_id for item in pl.items}
    candidate_posters = [
        p for p in visible_posters(user, db) if p.id not in in_playlist
    ]
    return templates.TemplateResponse(request, "playlist_detail.html", {
        "user": user, "pl": pl, "cities": cities, "devices_by_city": devices_by_city,
        "groups": groups, "candidate_posters": candidate_posters,
        "target_city_ids": {t.city_id for t in pl.targets if t.city_id},
        "target_device_ids": {t.device_id for t in pl.targets if t.device_id},
        "target_group_ids": {t.group_id for t in pl.targets if t.group_id},
    })


@router.post("/{playlist_id}/update")
def update_playlist(
    playlist_id: int,
    name: str = Form(...),
    city: list[int] = Form([]),
    device: list[int] = Form([]),
    group: list[int] = Form([]),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    pl = db.get(Playlist, playlist_id)
    if pl is None:
        return redirect("/playlists", err="Плейлист не найден.")
    _check_playlist_access(user, pl, db)
    pl.name = name.strip() or pl.name

    allowed = {c.id for c in visible_cities(user, db)}
    new_cities = {c for c in city if c in allowed}
    new_devices = {
        d.id for d in db.query(Device).filter(Device.id.in_(device or [])).all()
        if d.city_id in allowed
    }
    submitted_groups = db.query(DeviceGroup).filter(
        DeviceGroup.id.in_(group or [])).all()
    new_groups = {
        g.id for g in submitted_groups if group_in_scope(user, g, db)
    }

    for t in list(pl.targets):
        if t.city_id is not None:
            if t.city_id in allowed and t.city_id not in new_cities:
                pl.targets.remove(t)
            new_cities.discard(t.city_id)
        elif t.device_id is not None:
            in_scope = t.device is not None and t.device.city_id in allowed
            if in_scope and t.device_id not in new_devices:
                pl.targets.remove(t)
            new_devices.discard(t.device_id)
        elif t.group_id is not None:
            # Как и с city/device выше: трогаем только назначения, которые
            # менеджеру видны (group_in_scope) — иначе форма без невидимой
            # ему кросс-городской группы молча снесла бы чужое назначение,
            # которое админ мог настроить намеренно.
            in_scope = t.group is not None and group_in_scope(user, t.group, db)
            if in_scope and t.group_id not in new_groups:
                pl.targets.remove(t)
            new_groups.discard(t.group_id)
    for cid in new_cities:
        pl.targets.append(PlaylistTarget(city_id=cid))
    for did in new_devices:
        pl.targets.append(PlaylistTarget(device_id=did))
    for gid in new_groups:
        pl.targets.append(PlaylistTarget(group_id=gid))

    db.commit()
    return redirect(f"/playlists/{playlist_id}", msg="Плейлист сохранён.")


@router.post("/{playlist_id}/toggle")
def toggle_playlist(
    playlist_id: int,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    pl = db.get(Playlist, playlist_id)
    if pl is None:
        return redirect("/playlists", err="Плейлист не найден.")
    _check_playlist_access(user, pl, db)
    pl.enabled = not pl.enabled
    db.commit()
    state = "включён" if pl.enabled else "выключен"
    return redirect(f"/playlists/{playlist_id}", msg=f"Плейлист {state}.")


@router.post("/{playlist_id}/delete")
def delete_playlist(
    playlist_id: int,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    pl = db.get(Playlist, playlist_id)
    if pl is None:
        return redirect("/playlists", err="Плейлист не найден.")
    _check_playlist_access(user, pl, db)
    name = pl.name
    db.delete(pl)  # cascade: items + targets
    db.commit()
    return redirect("/playlists", msg=f"Плейлист «{name}» удалён.")


@router.post("/{playlist_id}/items/add")
def add_item(
    playlist_id: int,
    poster_id: int = Form(...),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    pl = db.get(Playlist, playlist_id)
    if pl is None:
        return redirect("/playlists", err="Плейлист не найден.")
    _check_playlist_access(user, pl, db)
    if poster_id not in {p.id for p in visible_posters(user, db)}:
        return redirect(f"/playlists/{playlist_id}", err="Афиша недоступна.")
    if poster_id in {item.poster_id for item in pl.items}:
        return redirect(f"/playlists/{playlist_id}", err="Афиша уже в плейлисте.")
    next_pos = max((i.position for i in pl.items), default=-1) + 1
    db.add(PlaylistItem(playlist_id=playlist_id, poster_id=poster_id,
                        position=next_pos))
    db.commit()
    return redirect(f"/playlists/{playlist_id}", msg="Афиша добавлена в плейлист.")


@router.post("/{playlist_id}/items/{item_id}/delete")
def delete_item(
    playlist_id: int,
    item_id: int,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    pl = db.get(Playlist, playlist_id)
    if pl is None:
        return redirect("/playlists", err="Плейлист не найден.")
    _check_playlist_access(user, pl, db)
    item = db.get(PlaylistItem, item_id)
    if item is None or item.playlist_id != playlist_id:
        return redirect(f"/playlists/{playlist_id}", err="Позиция не найдена.")
    db.delete(item)
    db.commit()
    return redirect(f"/playlists/{playlist_id}", msg="Афиша убрана из плейлиста.")


@router.post("/{playlist_id}/items/{item_id}/move")
def move_item(
    playlist_id: int,
    item_id: int,
    direction: str = Form(...),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    pl = db.get(Playlist, playlist_id)
    if pl is None:
        return redirect("/playlists", err="Плейлист не найден.")
    _check_playlist_access(user, pl, db)
    items = pl.items  # relationship уже упорядочен по position
    idx = next((i for i, it in enumerate(items) if it.id == item_id), None)
    if idx is None:
        return redirect(f"/playlists/{playlist_id}", err="Позиция не найдена.")
    swap_idx = idx - 1 if direction == "up" else idx + 1
    if 0 <= swap_idx < len(items):
        items[idx].position, items[swap_idx].position = (
            items[swap_idx].position, items[idx].position,
        )
        db.commit()
    return redirect(f"/playlists/{playlist_id}")
