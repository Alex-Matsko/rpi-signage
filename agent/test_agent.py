"""Тесты чистых функций агента (agent.py — не пакет, грузим по пути)."""
import importlib.util
from datetime import datetime
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "signage_agent", Path(__file__).parent / "agent.py"
)
agent = importlib.util.module_from_spec(spec)
spec.loader.exec_module(agent)

# Понедельник 14 июля 2026, 15:00
MON_DAY = datetime(2026, 7, 13, 15, 0)
SAT_NIGHT = datetime(2026, 7, 18, 23, 30)
SUN_EARLY = datetime(2026, 7, 19, 1, 30)


def active(item, t):
    return agent.item_is_active(item, t)


def test_no_restrictions():
    assert active({}, MON_DAY)


def test_dates():
    assert not active({"starts_at": "2026-07-14T00:00:00"}, MON_DAY)
    assert active({"starts_at": "2026-07-13T00:00:00"}, MON_DAY)
    assert not active({"expires_at": "2026-07-13T15:00:00"}, MON_DAY)
    assert active({"expires_at": "2026-07-13T15:01:00"}, MON_DAY)


def test_weekdays_mask():
    monday_only = {"weekdays": 1}          # бит 0 = понедельник
    weekend = {"weekdays": (1 << 5) | (1 << 6)}
    assert active(monday_only, MON_DAY)
    assert not active(weekend, MON_DAY)
    assert active(weekend, SAT_NIGHT)


def test_daily_window():
    work_hours = {"daily_from": "09:00", "daily_until": "18:00"}
    assert active(work_hours, MON_DAY)
    assert not active(work_hours, SAT_NIGHT)


def test_overnight_window():
    night = {"daily_from": "22:00", "daily_until": "06:00"}
    assert active(night, SAT_NIGHT)   # 23:30 — внутри
    assert active(night, SUN_EARLY)   # 01:30 — внутри (после полуночи)
    assert not active(night, MON_DAY)  # 15:00 — снаружи


def test_open_ended_window():
    assert active({"daily_from": "12:00"}, MON_DAY)
    assert not active({"daily_from": "16:00"}, MON_DAY)
    assert active({"daily_until": "16:00"}, MON_DAY)
    assert not active({"daily_until": "15:00"}, MON_DAY)


def test_combined():
    item = {
        "weekdays": 1,
        "daily_from": "14:00",
        "daily_until": "16:00",
        "expires_at": "2027-01-01T00:00:00",
    }
    assert active(item, MON_DAY)
    assert not active(item, SAT_NIGHT)
