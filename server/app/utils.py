from datetime import datetime
from urllib.parse import quote

from fastapi.responses import RedirectResponse


def parse_dt_local(value: str | None) -> datetime | None:
    """Разбирает значение input type=datetime-local ('' -> None)."""
    if not value:
        return None
    return datetime.strptime(value, "%Y-%m-%dT%H:%M")


def redirect(url: str, msg: str | None = None, err: str | None = None):
    if msg:
        url += ("&" if "?" in url else "?") + "msg=" + quote(msg)
    if err:
        url += ("&" if "?" in url else "?") + "err=" + quote(err)
    return RedirectResponse(url, status_code=303)
