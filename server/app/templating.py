from datetime import datetime
from pathlib import Path

from fastapi.templating import Jinja2Templates

from . import config

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _fmt_dt(v: datetime | None) -> str:
    return v.strftime("%d.%m.%Y %H:%M") if v else "—"


def _fmt_size(n: int | None) -> str:
    if n is None:
        return "—"
    size = float(n)
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if size < 1024 or unit == "ГБ":
            return f"{size:.0f} {unit}" if unit in ("Б", "КБ") else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} Б"


def _fmt_duration(sec: float | None) -> str:
    if not sec:
        return "—"
    m, s = divmod(int(sec), 60)
    return f"{m}:{s:02d}"


def _fmt_uptime(sec: int | None) -> str:
    if sec is None:
        return "—"
    d, rem = divmod(int(sec), 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return f"{d} д {h} ч"
    if h:
        return f"{h} ч {m} мин"
    return f"{m} мин"


templates.env.filters["dt"] = _fmt_dt
templates.env.filters["uptime"] = _fmt_uptime
templates.env.filters["filesize"] = _fmt_size
templates.env.filters["duration"] = _fmt_duration
templates.env.globals["offline_after"] = config.OFFLINE_AFTER_SEC

POSTER_STATUS = {
    "active": ("Активна", "ok"),
    "disabled": ("Выключена", "muted"),
    "expired": ("Истекла", "err"),
    "scheduled": ("Ожидает начала", "warn"),
}
templates.env.globals["POSTER_STATUS"] = POSTER_STATUS
