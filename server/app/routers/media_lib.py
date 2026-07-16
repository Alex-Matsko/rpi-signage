from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from .. import media
from ..db import get_db
from ..deps import current_user
from ..models import MediaFile, User
from ..templating import templates
from ..utils import redirect

router = APIRouter()


@router.get("/media")
def media_page(
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    files = db.query(MediaFile).order_by(MediaFile.created_at.desc()).all()
    return templates.TemplateResponse(request, "media.html", {
        "user": user,
        "files": files,
    })


@router.post("/media/{media_id}/delete")
def delete_media(
    media_id: int,
    user: User = Depends(current_user),
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
):
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
    path = media.media_path(mf.sha256)
    if not path.exists():
        raise HTTPException(status_code=404)
    return FileResponse(path, media_type=mf.mime, filename=mf.orig_name)
