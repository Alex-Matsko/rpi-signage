from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from .. import media, worker
from ..db import get_db
from ..deps import current_user, require_admin
from ..models import MediaFile, User
from ..templating import templates
from ..utils import redirect
from .playlists import visible_playlists
from .posters import visible_posters

router = APIRouter()


def visible_media(user: User, db: Session) -> list[MediaFile]:
    """Менеджер видит: файлы, загруженные им самим (uploaded_by), плюс файлы,
    использованные в его афишах/плейлистах. У MediaFile сознательно нет
    city_id — видимость строится через uploaded_by и join по владению
    Poster/Playlist, без схемных изменений в самой MediaFile."""
    files = db.query(MediaFile).order_by(MediaFile.created_at.desc()).all()
    if user.is_admin:
        return files
    ids = {mf.id for mf in files if mf.uploaded_by == user.id}
    ids |= {p.media_id for p in visible_posters(user, db)}
    for pl in visible_playlists(user, db):
        ids |= {item.poster.media_id for item in pl.items}
    return [m for m in files if m.id in ids]


def _require_visible(user: User, mf: MediaFile, db: Session) -> None:
    if user.is_admin:
        return
    if mf.id not in {m.id for m in visible_media(user, db)}:
        raise HTTPException(status_code=403, detail="Файл недоступен")


@router.post("/media/upload")
def upload_to_library(
    files: list[UploadFile],
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    """Загрузка файлов в медиатеку без создания афиш (просто в библиотеку)."""
    added, errors, transcoding = 0, [], 0
    to_enqueue = []
    for upload in files:
        if not upload.filename:
            continue
        try:
            attrs = media.save_upload(upload)
        except media.MediaError as e:
            errors.append(f"{upload.filename}: {e}")
            continue
        mf = db.query(MediaFile).filter(
            MediaFile.sha256 == attrs["sha256"]).first()
        if mf is None:
            mf = MediaFile(**attrs, uploaded_by=user.id)
            if mf.kind == "video" and not mf.compatible:
                mf.transcode_status = "pending"
            db.add(mf)
            db.flush()
            added += 1
            if mf.transcode_status == "pending":
                transcoding += 1
                to_enqueue.append(mf.id)
    db.commit()
    for mid in to_enqueue:
        worker.enqueue(mid)
    if not added and errors:
        return redirect("/media", err="; ".join(errors))
    msg = f"В медиатеку добавлено файлов: {added}."
    if transcoding:
        msg += f" В очереди на транскодирование: {transcoding}."
    return redirect("/media", msg=msg, err="; ".join(errors) or None)


@router.post("/media/{media_id}/transcode")
def retry_transcode(
    media_id: int,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    """Повторная постановка несовместимого видео в очередь транскодирования."""
    mf = db.get(MediaFile, media_id)
    if mf is None:
        return redirect("/media", err="Файл не найден.")
    _require_visible(user, mf, db)
    if mf.kind != "video" or mf.compatible:
        return redirect("/media", err="Файл не требует транскодирования.")
    mf.transcode_status = "pending"
    db.commit()
    worker.enqueue(mf.id)
    return redirect("/media", msg=f"«{mf.orig_name}» поставлен в очередь.")


@router.get("/media")
def media_page(
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    return templates.TemplateResponse(request, "media.html", {
        "user": user,
        "files": visible_media(user, db),
    })


@router.post("/media/{media_id}/delete")
def delete_media(
    media_id: int,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    mf = db.get(MediaFile, media_id)
    if mf is None:
        return redirect("/media", err="Файл не найден.")
    if mf.posters:
        return redirect(
            "/media",
            err=f"Файл «{mf.orig_name}» используется афишами "
                f"({len(mf.posters)}) — сначала удалите их.",
        )
    sha = mf.sha256
    name = mf.orig_name
    db.delete(mf)
    db.commit()
    media.delete_media_files(sha)
    return redirect("/media", msg=f"Файл «{name}» удалён.")


@router.get("/thumbs/{sha256}.jpg")
def thumbnail(
    sha256: str,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    mf = db.query(MediaFile).filter(MediaFile.sha256 == sha256).first()
    if mf is None:
        raise HTTPException(status_code=404)
    _require_visible(user, mf, db)
    path = media.thumb_path(sha256)
    if not path.exists():
        raise HTTPException(status_code=404)
    return FileResponse(path, media_type="image/jpeg")


@router.get("/media/{media_id}/download")
def download(
    media_id: int,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    mf = db.get(MediaFile, media_id)
    if mf is None:
        raise HTTPException(status_code=404)
    _require_visible(user, mf, db)
    path = media.media_path(mf.sha256)
    if not path.exists():
        raise HTTPException(status_code=404)
    return FileResponse(path, media_type=mf.mime, filename=mf.orig_name)


@router.get("/media/{media_id}/preview")
def preview(
    media_id: int,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    """Отдаёт файл inline для встроенного плеера в браузере (не скачивание)."""
    mf = db.get(MediaFile, media_id)
    if mf is None:
        raise HTTPException(status_code=404)
    _require_visible(user, mf, db)
    path = media.media_path(mf.sha256)
    if not path.exists():
        raise HTTPException(status_code=404)
    # Без filename → Content-Disposition inline: браузер проигрывает, не качает
    return FileResponse(path, media_type=mf.mime)
