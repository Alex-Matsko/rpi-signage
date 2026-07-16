"""Конфигурация сервера. Все параметры берутся из переменных окружения."""
import os
import secrets
from pathlib import Path

DATA_DIR = Path(os.environ.get("SIGNAGE_DATA_DIR", "./data")).resolve()
DB_DIR = DATA_DIR / "db"
MEDIA_DIR = DATA_DIR / "media"
THUMB_DIR = DATA_DIR / "thumbs"
DB_PATH = DB_DIR / "signage.db"
SECRET_KEY_FILE = DATA_DIR / "secret_key"

MAX_UPLOAD_MB = int(os.environ.get("SIGNAGE_MAX_UPLOAD_MB", "1024"))
POLL_INTERVAL = int(os.environ.get("SIGNAGE_POLL_INTERVAL", "60"))
# Через сколько секунд без heartbeat устройство считается офлайн
OFFLINE_AFTER_SEC = int(os.environ.get("SIGNAGE_OFFLINE_AFTER_SEC", "90"))
# За сколько дней предупреждать об истечении афиши
EXPIRY_WARN_DAYS = int(os.environ.get("SIGNAGE_EXPIRY_WARN_DAYS", "7"))

ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")

SESSION_COOKIE = "signage_session"
SESSION_MAX_AGE = 14 * 24 * 3600

_secret_key: str | None = None


def secret_key() -> str:
    """Ключ подписи сессий: генерируется один раз и хранится на томе данных."""
    global _secret_key
    if _secret_key is None:
        if SECRET_KEY_FILE.exists():
            _secret_key = SECRET_KEY_FILE.read_text().strip()
        else:
            SECRET_KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
            _secret_key = secrets.token_hex(32)
            SECRET_KEY_FILE.write_text(_secret_key)
            SECRET_KEY_FILE.chmod(0o600)
    return _secret_key


def ensure_dirs() -> None:
    for d in (DB_DIR, MEDIA_DIR, THUMB_DIR):
        d.mkdir(parents=True, exist_ok=True)
