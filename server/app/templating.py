from datetime import datetime
from pathlib import Path

from fastapi.templating import Jinja2Templates

from . import config
from .grid import LAYOUTS, grid_dims

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


_WEEKDAYS = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")


def _fmt_weekdays(mask: int | None) -> str:
    if not mask or mask >= 127:
        return "ежедневно"
    return ", ".join(d for i, d in enumerate(_WEEKDAYS) if (mask >> i) & 1)


def _fmt_schedule(poster) -> str:
    """Краткое описание окна показа афиши, '' если ограничений нет."""
    parts = []
    if poster.weekdays_mask:
        parts.append(_fmt_weekdays(poster.weekdays_mask))
    if poster.daily_from or poster.daily_until:
        parts.append(f"{poster.daily_from or '00:00'}–{poster.daily_until or '24:00'}")
    return " · ".join(parts)


templates.env.filters["dt"] = _fmt_dt
templates.env.filters["uptime"] = _fmt_uptime
templates.env.filters["filesize"] = _fmt_size
templates.env.filters["duration"] = _fmt_duration
templates.env.filters["weekdays"] = _fmt_weekdays
templates.env.filters["schedule"] = _fmt_schedule
templates.env.globals["offline_after"] = config.OFFLINE_AFTER_SEC
templates.env.globals["WEEKDAY_NAMES"] = _WEEKDAYS
templates.env.globals["GRID_LAYOUTS"] = LAYOUTS
templates.env.globals["grid_dims"] = grid_dims

POSTER_STATUS = {
    "active": ("Активна", "ok"),
    "disabled": ("Выключена", "muted"),
    "expired": ("Истекла", "err"),
    "scheduled": ("Ожидает начала", "warn"),
}
templates.env.globals["POSTER_STATUS"] = POSTER_STATUS

TRANSCODE_STATUS = {
    "pending": ("в очереди на транскодирование", "warn"),
    "running": ("транскодируется…", "warn"),
    "failed": ("транскодирование не удалось", "err"),
}
templates.env.globals["TRANSCODE_STATUS"] = TRANSCODE_STATUS

COMMAND_STATUS = {
    "pending": ("отправлена", "warn"),
    "done": ("выполнена", "ok"),
    "failed": ("ошибка", "err"),
}
templates.env.globals["COMMAND_STATUS"] = COMMAND_STATUS

COMMAND_KINDS = {
    "resync": "Отправка афиш",
    "screenshot": "Скриншот",
    "restart_agent": "Перезапуск агента",
    "reboot": "Перезагрузка RPi",
}
templates.env.globals["COMMAND_KINDS"] = COMMAND_KINDS
