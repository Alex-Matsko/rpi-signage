"""Пароли, сессии, токены устройств, rate-limit входа."""
import hashlib
import secrets
import string
import time

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from itsdangerous import BadSignature, URLSafeTimedSerializer

from . import config

_ph = PasswordHasher()


def hash_password(password: str) -> str:
    return _ph.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _ph.verify(password_hash, password)
    except VerifyMismatchError:
        return False
    except Exception:
        return False


# --- Сессии (подписанные cookie) ---

def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(config.secret_key(), salt="signage-session")


def create_session(user_id: int) -> str:
    return _serializer().dumps({"uid": user_id})


def read_session(cookie: str) -> int | None:
    try:
        data = _serializer().loads(cookie, max_age=config.SESSION_MAX_AGE)
        return int(data["uid"])
    except (BadSignature, KeyError, ValueError):
        return None


# --- Токены устройств ---

def new_device_token() -> tuple[str, str]:
    """Возвращает (токен, sha256-хеш токена для хранения в БД)."""
    token = secrets.token_hex(32)
    return token, hash_token(token)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_pairing_code() -> str:
    alphabet = string.ascii_uppercase + string.digits
    raw = "".join(secrets.choice(alphabet) for _ in range(8))
    return f"{raw[:4]}-{raw[4:]}"


# --- Rate-limit входа: не более 5 неудач за 5 минут с одного IP ---

_WINDOW_SEC = 300
_MAX_FAILURES = 5
_failures: dict[str, list[float]] = {}


def login_blocked(ip: str) -> bool:
    cutoff = time.monotonic() - _WINDOW_SEC
    attempts = [t for t in _failures.get(ip, []) if t > cutoff]
    _failures[ip] = attempts
    return len(attempts) >= _MAX_FAILURES


def register_login_failure(ip: str) -> None:
    _failures.setdefault(ip, []).append(time.monotonic())


def reset_login_failures(ip: str) -> None:
    _failures.pop(ip, None)
