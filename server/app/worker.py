"""Фоновый воркер транскодирования несовместимого видео.

Очередь в памяти + один поток: для сервера с единицами загрузок в день
этого достаточно. После успешного транскодирования запись MediaFile
обновляется на новый файл (sha256 меняется), афиши следуют за ней
автоматически, манифесты устройств обновляются при следующем опросе.
"""
import logging
import queue
import threading

from . import media
from .db import SessionLocal
from .models import MediaFile

log = logging.getLogger("signage.worker")

_queue: "queue.Queue[int]" = queue.Queue()
_started = threading.Event()


def enqueue(media_id: int) -> None:
    _queue.put(media_id)


def start() -> None:
    """Запускает поток воркера (однократно) и доставляет зависшие задачи."""
    if _started.is_set():
        return
    _started.set()
    with SessionLocal() as db:
        stuck = (
            db.query(MediaFile)
            .filter(MediaFile.transcode_status.in_(["pending", "running"]))
            .all()
        )
        for mf in stuck:
            mf.transcode_status = "pending"
            _queue.put(mf.id)
        db.commit()
    threading.Thread(target=_run, daemon=True, name="transcode-worker").start()


def _run() -> None:
    while True:
        media_id = _queue.get()
        try:
            _process(media_id)
        except Exception:
            log.exception("Ошибка воркера для media_id=%s", media_id)


def _process(media_id: int) -> None:
    with SessionLocal() as db:
        mf = db.get(MediaFile, media_id)
        if mf is None or mf.kind != "video" or mf.compatible:
            return
        mf.transcode_status = "running"
        db.commit()
        old_sha = mf.sha256

    src = media.media_path(old_sha)
    tmp = src.parent / f".transcode-{old_sha}.mp4"
    log.info("Транскодирую %s (%s)", old_sha[:12], media_id)
    try:
        if not src.exists():
            raise media.MediaError("Исходный файл не найден на диске.")
        media.transcode_video(src, tmp)
        new_sha = media.file_sha256(tmp)
        attrs = media._probe_video(tmp, new_sha)
        if not attrs["compatible"]:
            raise media.MediaError(
                "Результат всё ещё несовместим: " + str(attrs["compat_warning"])
            )
        tmp.rename(media.media_path(new_sha))
    except media.MediaError as e:
        tmp.unlink(missing_ok=True)
        with SessionLocal() as db:
            mf = db.get(MediaFile, media_id)
            if mf is not None:
                mf.transcode_status = "failed"
                mf.compat_warning = (
                    (mf.compat_warning or "") + f" | Транскодирование: {e}"
                )
                db.commit()
        log.error("Транскодирование %s не удалось: %s", old_sha[:12], e)
        return

    with SessionLocal() as db:
        mf = db.get(MediaFile, media_id)
        if mf is None:
            media.delete_media_files(new_sha)
            return
        duplicate = (
            db.query(MediaFile)
            .filter(MediaFile.sha256 == new_sha, MediaFile.id != media_id)
            .first()
        )
        if duplicate is not None:
            # Такой результат уже есть (повторная загрузка того же ролика)
            mf.transcode_status = "failed"
            mf.compat_warning = (
                "Совместимая версия уже есть в медиатеке: "
                f"«{duplicate.orig_name}»"
            )
            db.commit()
            return
        mf.sha256 = new_sha
        mf.mime = "video/mp4"
        mf.size_bytes = media.media_path(new_sha).stat().st_size
        mf.width = attrs["width"]
        mf.height = attrs["height"]
        mf.duration_sec = attrs["duration_sec"]
        mf.video_codec = attrs["video_codec"]
        mf.compatible = True
        mf.compat_warning = None
        mf.transcode_status = "done"
        db.commit()
    media.delete_media_files(old_sha)
    log.info("Готово: %s -> %s", old_sha[:12], new_sha[:12])
