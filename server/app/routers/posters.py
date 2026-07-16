from fastapi import APIRouter, Depends, Form, Request, UploadFile
from sqlalchemy.orm import Session

from .. import media
from ..db import get_db
from ..deps import current_user
from ..models import MediaFile, Playlist, Poster, User
from ..templating import templates
from ..utils import parse_dt_local, redirect

router = APIRouter(prefix="/posters")


@router.get("")
def posters_page(
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    posters = db.query(Poster).order_by(Poster.created_at.desc()).all()
    playlists = db.query(Playlist).order_by(Playlist.name).all()
    return templates.TemplateResponse(request, "posters.html", {
        "user": user,
        "posters": posters,
        "playlists": playlists,
    })


@router.post("/upload")
def upload_poster(
    file: UploadFile,
    name: str = Form(""),
    display_seconds: int = Form(10),
    starts_at: str = Form(""),
    expires_at: str = Form(""),
    playlist_id: int = Form(0),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    try:
        attrs = media.save_upload(file)
    except media.MediaError as e:
        return redirect("/posters", err=str(e))

    mf = db.query(MediaFile).filter(MediaFile.sha256 == attrs["sha256"]).first()
    if mf is None:
        mf = MediaFile(**attrs)
        db.add(mf)
        db.flush()

    poster = Poster(
        name=name.strip() or (file.filename or "Афиша"),
        media_id=mf.id,
        display_seconds=max(1, display_seconds),
        starts_at=parse_dt_local(starts_at),
        expires_at=parse_dt_local(expires_at),
        enabled=True,
    )
    db.add(poster)
    db.flush()

    msg = f"Афиша «{poster.name}» загружена."
    if playlist_id:
        playlist = db.get(Playlist, playlist_id)
        if playlist is not None:
            from ..models import PlaylistItem
            playlist.items.append(PlaylistItem(poster_id=poster.id,
                                               position=len(playlist.items)))
            msg += f" Добавлена в плейлист «{playlist.name}»."
    db.commit()

    if mf.compat_warning:
        return redirect("/posters", msg=msg, err=mf.compat_warning)
    return redirect("/posters", msg=msg)


@router.post("/{poster_id}/update")
def update_poster(
    poster_id: int,
    name: str = Form(...),
    display_seconds: int = Form(10),
    starts_at: str = Form(""),
    expires_at: str = Form(""),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    poster = db.get(Poster, poster_id)
    if poster is None:
        return redirect("/posters", err="Афиша не найдена.")
    poster.name = name.strip() or poster.name
    poster.display_seconds = max(1, display_seconds)
    poster.starts_at = parse_dt_local(starts_at)
    poster.expires_at = parse_dt_local(expires_at)
    db.commit()
    return redirect("/posters", msg=f"Афиша «{poster.name}» обновлена.")


@router.post("/{poster_id}/toggle")
def toggle_poster(
    poster_id: int,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    poster = db.get(Poster, poster_id)
    if poster is None:
        return redirect("/posters", err="Афиша не найдена.")
    poster.enabled = not poster.enabled
    db.commit()
    state = "включена" if poster.enabled else "выключена"
    return redirect("/posters", msg=f"Афиша «{poster.name}» {state}.")


@router.post("/{poster_id}/delete")
def delete_poster(
    poster_id: int,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    poster = db.get(Poster, poster_id)
    if poster is None:
        return redirect("/posters", err="Афиша не найдена.")
    name = poster.name
    db.delete(poster)
    db.commit()
    return redirect("/posters", msg=f"Афиша «{name}» удалена.")
