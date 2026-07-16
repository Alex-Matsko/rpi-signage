"""Приём и обработка медиафайлов: хеширование, ffprobe, превью, совместимость.

Требование совместимости с RPi 2/3/4: видео H.264 (AVC), не выше 1080p, до 30 fps.
"""
import hashlib
import json
import subprocess
import tempfile
from pathlib import Path

from fastapi import UploadFile
from PIL import Image

from . import config

ALLOWED_IMAGE_MIME = {"image/jpeg": "image", "image/png": "image"}
ALLOWED_VIDEO_MIME = {"video/mp4": "video"}
THUMB_MAX = 480


class MediaError(Exception):
    pass


def media_path(sha256: str) -> Path:
    return config.MEDIA_DIR / sha256


def thumb_path(sha256: str) -> Path:
    return config.THUMB_DIR / f"{sha256}.jpg"


def save_upload(upload: UploadFile) -> dict:
    """Сохраняет загруженный файл, возвращает атрибуты для MediaFile."""
    mime = upload.content_type or ""
    if mime in ALLOWED_IMAGE_MIME:
        kind = "image"
    elif mime in ALLOWED_VIDEO_MIME:
        kind = "video"
    else:
        raise MediaError(
            f"Недопустимый тип файла: {mime or 'неизвестен'}. "
            "Поддерживаются JPEG, PNG и MP4."
        )

    max_bytes = config.MAX_UPLOAD_MB * 1024 * 1024
    sha = hashlib.sha256()
    size = 0
    with tempfile.NamedTemporaryFile(dir=config.MEDIA_DIR, delete=False) as tmp:
        tmp_path = Path(tmp.name)
        try:
            while chunk := upload.file.read(1024 * 1024):
                size += len(chunk)
                if size > max_bytes:
                    raise MediaError(
                        f"Файл больше лимита {config.MAX_UPLOAD_MB} МБ."
                    )
                sha.update(chunk)
                tmp.write(chunk)
        except MediaError:
            tmp_path.unlink(missing_ok=True)
            raise

    if size == 0:
        tmp_path.unlink(missing_ok=True)
        raise MediaError("Пустой файл.")

    sha256 = sha.hexdigest()
    dest = media_path(sha256)
    if dest.exists():
        tmp_path.unlink(missing_ok=True)  # такой файл уже загружен
    else:
        tmp_path.rename(dest)

    attrs: dict = {
        "sha256": sha256,
        "orig_name": upload.filename or sha256,
        "kind": kind,
        "mime": mime,
        "size_bytes": size,
        "compatible": True,
        "compat_warning": None,
    }
    try:
        if kind == "image":
            attrs.update(_probe_image(dest, sha256))
        else:
            attrs.update(_probe_video(dest, sha256))
    except MediaError:
        if not _file_in_use_elsewhere(dest):
            dest.unlink(missing_ok=True)
        raise
    return attrs


def _file_in_use_elsewhere(_path: Path) -> bool:
    # Файл с тем же хешем мог быть загружен ранее; удаление решает вызывающий код.
    return False


def _probe_image(path: Path, sha256: str) -> dict:
    try:
        with Image.open(path) as img:
            img.load()
            width, height = img.size
            thumb = img.convert("RGB")
            thumb.thumbnail((THUMB_MAX, THUMB_MAX))
            thumb.save(thumb_path(sha256), "JPEG", quality=85)
    except Exception as e:
        raise MediaError(f"Не удалось прочитать изображение: {e}") from e
    return {"width": width, "height": height}


def _probe_video(path: Path, sha256: str) -> dict:
    try:
        out = subprocess.run(
            [
                "ffprobe", "-v", "error", "-print_format", "json",
                "-show_streams", "-show_format", str(path),
            ],
            capture_output=True, text=True, timeout=60, check=True,
        ).stdout
        info = json.loads(out)
    except FileNotFoundError as e:
        raise MediaError("ffprobe не найден на сервере.") from e
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        raise MediaError("Файл не распознан как корректное видео.") from e

    video = next(
        (s for s in info.get("streams", []) if s.get("codec_type") == "video"),
        None,
    )
    if video is None:
        raise MediaError("В файле нет видеопотока.")

    codec = video.get("codec_name", "?")
    width = int(video.get("width") or 0)
    height = int(video.get("height") or 0)
    duration = float(info.get("format", {}).get("duration") or 0) or None
    fps = _parse_fps(video.get("avg_frame_rate") or video.get("r_frame_rate"))

    warnings = []
    if codec != "h264":
        warnings.append(
            f"кодек {codec} — аппаратно декодируется только H.264 (AVC)"
        )
    if height > 1080 or width > 1920:
        warnings.append(f"разрешение {width}x{height} — максимум 1920x1080")
    if fps and fps > 31:
        warnings.append(f"{fps:.0f} fps — максимум 30 fps")

    _video_thumbnail(path, sha256)

    return {
        "width": width,
        "height": height,
        "duration_sec": duration,
        "video_codec": codec,
        "compatible": not warnings,
        "compat_warning": (
            "Может не воспроизводиться на RPi 2/3/4: " + "; ".join(warnings)
            if warnings else None
        ),
    }


def _parse_fps(rate: str | None) -> float | None:
    if not rate:
        return None
    try:
        num, _, den = rate.partition("/")
        return float(num) / float(den or 1)
    except (ValueError, ZeroDivisionError):
        return None


def _video_thumbnail(path: Path, sha256: str) -> None:
    try:
        subprocess.run(
            [
                "ffmpeg", "-v", "error", "-ss", "1", "-i", str(path),
                "-frames:v", "1", "-vf", f"scale={THUMB_MAX}:-2",
                "-y", str(thumb_path(sha256)),
            ],
            capture_output=True, timeout=60, check=True,
        )
    except Exception:
        # Превью не критично: возможно, ролик короче 1 секунды
        try:
            subprocess.run(
                [
                    "ffmpeg", "-v", "error", "-i", str(path),
                    "-frames:v", "1", "-vf", f"scale={THUMB_MAX}:-2",
                    "-y", str(thumb_path(sha256)),
                ],
                capture_output=True, timeout=60, check=True,
            )
        except Exception:
            pass


def delete_media_files(sha256: str) -> None:
    media_path(sha256).unlink(missing_ok=True)
    thumb_path(sha256).unlink(missing_ok=True)
