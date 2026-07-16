"""Тесты чистых функций агента (agent.py — не пакет, грузим по пути)."""
import base64
import http.client
import importlib.util
import time
from datetime import datetime
from pathlib import Path

import pytest

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


# ------------------------------------------------ настройки и веб-панель

def test_password_hash_roundtrip():
    h = agent._hash_pw("secret123")
    assert agent._verify_pw("secret123", h)
    assert not agent._verify_pw("wrong", h)
    assert h != agent._hash_pw("secret123")  # соль случайна


def test_settings_persist(tmp_path):
    path = tmp_path / "settings.json"
    s = agent.Settings(path)
    assert s.check_password("admin", "signage")  # пароль по умолчанию
    s.set_credentials("boss", "newpass1")
    # Перечитываем с диска
    s2 = agent.Settings(path)
    assert s2.check_password("boss", "newpass1")
    assert not s2.check_password("admin", "signage")


def test_mock_backend_shapes():
    b = agent.MockBackend()
    net = b.network_status()
    assert "ip" in net and net["connections"]
    assert b.wifi_scan()[0]["ssid"]
    assert any(o["id"] == "hdmi" for o in b.audio_outputs())
    ok, _ = b.wifi_connect("X", "y")
    assert ok


@pytest.fixture
def panel(tmp_path):
    settings = agent.Settings(tmp_path / "settings.json")
    ctx = agent.WebContext(
        settings, agent.MockBackend(), state=None,
        get_status=lambda: {"Версия агента": "test"},
        actions={"restart_agent": lambda: None,
                 "reboot": lambda: (True, "")},
    )
    server = agent.LocalWebServer(ctx, port=0)
    import http.server
    server.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0),
                                                    agent._WebHandler)
    server.httpd.ctx = ctx
    import threading
    threading.Thread(target=server.httpd.serve_forever, daemon=True).start()
    port = server.httpd.server_address[1]
    yield settings, port
    server.stop()


def _req(port, method, path, auth=("admin", "signage"), body=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {}
    if auth:
        token = base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode()
        headers["Authorization"] = "Basic " + token
    if body is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    conn.request(method, path, body=body, headers=headers)
    resp = conn.getresponse()
    data = resp.read().decode()
    conn.close()
    return resp.status, data


def test_panel_requires_auth(panel):
    _settings, port = panel
    status, _ = _req(port, "GET", "/", auth=None)
    assert status == 401
    status, _ = _req(port, "GET", "/", auth=("admin", "wrong"))
    assert status == 401


def test_panel_pages(panel):
    _settings, port = panel
    for path, needle in [("/", "Обзор"), ("/network", "Wi-Fi"),
                         ("/audio", "Аудиовыход"), ("/system", "Имя устройства")]:
        status, body = _req(port, "GET", path)
        assert status == 200 and needle in body, path


def test_panel_set_audio(panel):
    settings, port = panel
    status, _ = _req(port, "POST", "/audio", body="audio_output=hdmi")
    assert status == 303
    assert settings.data["audio_output"] == "hdmi"


def test_panel_change_password(panel):
    settings, port = panel
    status, _ = _req(port, "POST", "/system/password",
                     body="user=boss&password=longpass&password2=longpass")
    assert status == 303
    assert settings.check_password("boss", "longpass")
    # Старые доступы больше не работают
    status, _ = _req(port, "GET", "/", auth=("admin", "signage"))
    assert status == 401
    status, _ = _req(port, "GET", "/", auth=("boss", "longpass"))
    assert status == 200
