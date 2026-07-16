from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import current_user
from ..models import Playlist, PlaylistItem, Poster, User
from ..templating import templates
from ..utils import redirect

router = APIRouter(prefix="/playlists")


def _normalize(playlist: Playlist) -> None:
    for i, item in enumerate(sorted(playlist.items, key=lambda x: x.position)):
        item.position = i


@router.get("")
def playlists_page(
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    playlists = db.query(Playlist).order_by(Playlist.name).all()
    return templates.TemplateResponse(request, "playlists.html", {
        "user": user,
        "playlists": playlists,
    })


@router.post("/create")
def create_playlist(
    name: str = Form(...),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    name = name.strip()
    if not name:
        return redirect("/playlists", err="Укажите название плейлиста.")
    if db.query(Playlist).filter(Playlist.name == name).first():
        return redirect("/playlists", err=f"Плейлист «{name}» уже существует.")
    playlist = Playlist(name=name)
    db.add(playlist)
    db.commit()
    return redirect(f"/playlists/{playlist.id}", msg=f"Плейлист «{name}» создан.")


@router.get("/{playlist_id}")
def playlist_page(
    playlist_id: int,
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    playlist = db.get(Playlist, playlist_id)
    if playlist is None:
        return redirect("/playlists", err="Плейлист не найден.")
    in_playlist = {item.poster_id for item in playlist.items}
    available = [
        p for p in db.query(Poster).order_by(Poster.name).all()
        if p.id not in in_playlist
    ]
    return templates.TemplateResponse(request, "playlist_edit.html", {
        "user": user,
        "playlist": playlist,
        "available": available,
    })


@router.post("/{playlist_id}/rename")
def rename_playlist(
    playlist_id: int,
    name: str = Form(...),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    playlist = db.get(Playlist, playlist_id)
    if playlist is None:
        return redirect("/playlists", err="Плейлист не найден.")
    playlist.name = name.strip() or playlist.name
    db.commit()
    return redirect(f"/playlists/{playlist_id}", msg="Название сохранено.")


@router.post("/{playlist_id}/delete")
def delete_playlist(
    playlist_id: int,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    playlist = db.get(Playlist, playlist_id)
    if playlist is None:
        return redirect("/playlists", err="Плейлист не найден.")
    from ..models import Device, DeviceGroup
    db.query(Device).filter(Device.playlist_id == playlist_id).update(
        {"playlist_id": None}
    )
    db.query(DeviceGroup).filter(DeviceGroup.playlist_id == playlist_id).update(
        {"playlist_id": None}
    )
    name = playlist.name
    db.delete(playlist)
    db.commit()
    return redirect("/playlists", msg=f"Плейлист «{name}» удалён.")


@router.post("/{playlist_id}/items/add")
def add_item(
    playlist_id: int,
    poster_id: int = Form(...),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    playlist = db.get(Playlist, playlist_id)
    poster = db.get(Poster, poster_id)
    if playlist is None or poster is None:
        return redirect("/playlists", err="Плейлист или афиша не найдены.")
    if any(item.poster_id == poster_id for item in playlist.items):
        return redirect(f"/playlists/{playlist_id}", err="Афиша уже в плейлисте.")
    playlist.items.append(
        PlaylistItem(poster_id=poster_id, position=len(playlist.items))
    )
    db.commit()
    return redirect(f"/playlists/{playlist_id}",
                    msg=f"«{poster.name}» добавлена в плейлист.")


@router.post("/{playlist_id}/items/{item_id}/move")
def move_item(
    playlist_id: int,
    item_id: int,
    direction: str = Form(...),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    playlist = db.get(Playlist, playlist_id)
    if playlist is None:
        return redirect("/playlists", err="Плейлист не найден.")
    _normalize(playlist)
    items = sorted(playlist.items, key=lambda x: x.position)
    idx = next((i for i, it in enumerate(items) if it.id == item_id), None)
    if idx is not None:
        swap = idx - 1 if direction == "up" else idx + 1
        if 0 <= swap < len(items):
            items[idx].position, items[swap].position = (
                items[swap].position, items[idx].position
            )
    db.commit()
    return redirect(f"/playlists/{playlist_id}")


@router.post("/{playlist_id}/items/{item_id}/remove")
def remove_item(
    playlist_id: int,
    item_id: int,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    playlist = db.get(Playlist, playlist_id)
    if playlist is None:
        return redirect("/playlists", err="Плейлист не найден.")
    item = db.get(PlaylistItem, item_id)
    if item is not None and item.playlist_id == playlist_id:
        playlist.items.remove(item)
        _normalize(playlist)
        db.commit()
    return redirect(f"/playlists/{playlist_id}", msg="Афиша убрана из плейлиста.")
