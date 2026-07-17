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
    state = agent.State(tmp_path / "state")
    state.cache_dir.mkdir(parents=True, exist_ok=True)
    bind_calls = []
    ctx = agent.WebContext(
        settings, agent.MockBackend(), state=state,
        get_status=lambda: {"Версия агента": "test"},
        actions={"restart_agent": lambda: None,
                 "reboot": lambda: (True, "")},
        auth_info=lambda: None,
        bind=lambda server, code: (bind_calls.append((server, code)) or
                                   (True, "привязано")),
    )
    ctx.bind_calls = bind_calls
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


# ------------------------------------------------ v0.6: сервер, хранилище, tz

def test_panel_server_and_storage_pages(panel):
    _settings, port = panel
    for path, needle in [("/server", "Привязать к серверу"),
                         ("/storage", "Хранилище")]:
        status, body = _req(port, "GET", path)
        assert status == 200 and needle in body, path


def test_panel_bind_calls_action(panel):
    settings, port = panel
    status, _ = _req(port, "POST", "/server/bind",
                     body="server=https://s.example.com&code=AB12-CD34")
    assert status == 303


def test_panel_timezone_in_system(panel):
    _settings, port = panel
    status, body = _req(port, "GET", "/system")
    assert status == 200 and "Часовой пояс" in body
    status, _ = _req(port, "POST", "/system/timezone",
                     body="timezone=Asia/Yekaterinburg")
    assert status == 303


def test_cache_report_and_delete(tmp_path):
    state = agent.State(tmp_path / "st")
    state.cache_dir.mkdir(parents=True, exist_ok=True)
    (state.cache_dir / ("a" * 64)).write_bytes(b"x" * 2048)
    sha = "b" * 64
    (state.cache_dir / sha).write_bytes(b"y" * 1024)
    state.items = [{"sha256": sha, "name": "Афиша Б"}]
    rep = agent.cache_report(state)
    assert rep["total_mb"] > 0
    assert len(rep["files"]) == 2
    by_sha = {f["sha256"]: f for f in rep["files"]}
    assert by_sha[sha]["name"] == "Афиша Б" and by_sha[sha]["in_use"]
    # Удаление одного файла
    assert agent.delete_cached(state, sha)
    assert not (state.cache_dir / sha).exists()
    # Защита от обхода пути
    assert not agent.delete_cached(state, "../evil")
    # Полная очистка
    removed = agent.clear_cache(state)
    assert removed == 1 and not any(state.cache_dir.iterdir())


# ------------------------------------------------ v0.8: ALSA-звук, редирект

def test_alsa_card_parsing():
    sample = (
        "  0 [vc4hdmi0       ]: vc4-hdmi - vc4-hdmi-0\n"
        "                      vc4-hdmi-0\n"
        "  1 [Headphones     ]: bcm2835_headpho - bcm2835 Headphones\n"
        "                      bcm2835 Headphones\n"
    )
    cards = agent._parse_alsa_cards(sample)
    assert [c["id"] for c in cards] == ["vc4hdmi0", "Headphones"]
    assert "HDMI" in cards[0]["label"]
    assert "3.5" in cards[1]["label"]


def test_alsa_empty():
    assert agent._parse_alsa_cards("") == []


def test_mpv_audio_device_arg():
    p = agent.MpvPlayer("/tmp/x.sock", [], audio_device="alsa/plughw:CARD=Headphones")
    assert p.audio_device == "alsa/plughw:CARD=Headphones"
    # смена выхода из другого потока не применяется немедленно
    p.set_audio_device("alsa/plughw:CARD=vc4hdmi0")
    assert p._desired_audio == "alsa/plughw:CARD=vc4hdmi0"
    assert p.audio_device == "alsa/plughw:CARD=Headphones"  # до перезапуска mpv


def test_panel_restart_redirect_cyrillic(panel):
    """Редирект с кириллицей в query не должен падать на latin-1 заголовке."""
    status, _ = _req(panel[1], "POST", "/system/restart-agent")
    assert status == 303  # раньше здесь был крах latin-1
    settings, port = panel
    # смена пароля тоже редиректит с кириллицей
    status, _ = _req(port, "POST", "/system/password",
                     body="user=admin&password=short&password2=short")
    assert status == 303
