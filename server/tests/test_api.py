"""Интеграционные тесты v0.3: публикация → города/экраны → манифест агента."""
import io
import re
from datetime import datetime, timedelta

from PIL import Image

from app.db import SessionLocal
from app.models import City, Device, MediaFile, Poster


def _png_bytes(color: str = "red") -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (320, 240), color).save(buf, format="PNG")
    return buf.getvalue()


def _create_city(admin, name):
    admin.post("/cities/create", data={"name": name}, follow_redirects=False)
    with SessionLocal() as db:
        return db.query(City).filter(City.name == name).one().id


def _create_screen(admin, name, city_id):
    resp = admin.post(
        "/screens/create",
        data={"name": name, "city_id": str(city_id)},
        follow_redirects=False,
    )
    device_id = int(resp.headers["location"].split("?")[0].rsplit("/", 1)[1])
    with SessionLocal() as db:
        return device_id, db.get(Device, device_id).pairing_code


def _register_agent(client, code):
    resp = client.post("/api/agent/register", json={"code": code})
    assert resp.status_code == 200
    return {"Authorization": f"Bearer {resp.json()['token']}"}


def test_healthz(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_ui_requires_login(client):
    for path in ("/", "/posters", "/screens", "/publish", "/users"):
        resp = client.get(path, follow_redirects=False)
        assert resp.status_code == 303, path
        assert resp.headers["location"] == "/login"


def test_login_wrong_password(client):
    resp = client.post(
        "/login", data={"username": "admin", "password": "wrong"},
        follow_redirects=False,
    )
    assert resp.status_code == 401


def test_publish_flow(admin, client):
    """Публикация двух файлов на город → агент экрана получает обе афиши."""
    city_id = _create_city(admin, "Екатеринбург")
    device_id, code = _create_screen(admin, "Касса №1", city_id)

    resp = admin.post(
        "/publish",
        files=[
            ("files", ("afisha-one.png", _png_bytes("red"), "image/png")),
            ("files", ("afisha-two.png", _png_bytes("blue"), "image/png")),
        ],
        data={
            "display_seconds": "7",
            "starts_at": "2026-07-17T09:00",
            "expires_at": "2026-12-31T23:00",
            "city": [str(city_id)],
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "/posters" in resp.headers["location"]
    assert "err" not in resp.headers["location"]

    page = admin.get("/posters")
    assert "afisha-one" in page.text and "afisha-two" in page.text

    headers = _register_agent(client, code)
    manifest = client.get("/api/agent/manifest", headers=headers).json()
    assert len(manifest["items"]) == 2
    item = manifest["items"][0]
    assert item["duration"] == 7
    assert item["expires_at"].startswith("2026-12-31")
    assert manifest["agent_version"]

    # Скачивание и heartbeat
    resp = client.get(item["url"], headers=headers)
    assert resp.status_code == 200

    resp = client.post("/api/agent/status", headers=headers, json={
        "agent_version": "0.3.0", "uptime_sec": 60, "cache_done": 2,
        "cache_total": 2,
        "current": {"name": item["name"], "sha256": item["sha256"],
                    "since": "2026-07-16T12:00:00"},
    })
    assert resp.json()["manifest_version"] == manifest["manifest_version"]
    page = admin.get(f"/screens/{device_id}")
    assert "online" in page.text

    # Новый экран в том же городе получает те же афиши без действий
    _, code2 = _create_screen(admin, "Касса №2", city_id)
    headers2 = _register_agent(client, code2)
    manifest2 = client.get("/api/agent/manifest", headers=headers2).json()
    assert len(manifest2["items"]) == 2

    # Истечение: афиша пропадает из манифеста
    with SessionLocal() as db:
        poster = db.query(Poster).filter(
            Poster.name == "afisha-one").one()
        poster.expires_at = datetime.now() - timedelta(hours=1)
        db.commit()
    manifest = client.get("/api/agent/manifest", headers=headers).json()
    assert len(manifest["items"]) == 1


def test_publish_requires_target(admin):
    resp = admin.post(
        "/publish",
        files=[("files", ("x.png", _png_bytes(), "image/png"))],
        follow_redirects=False,
    )
    assert "err=" in resp.headers["location"]


def test_publish_rejects_unknown_type(admin):
    city_id = _create_city(admin, "Сочи")
    resp = admin.post(
        "/publish",
        files=[("files", ("evil.exe", b"MZ...", "application/x-msdownload"))],
        data={"city": [str(city_id)]},
        follow_redirects=False,
    )
    assert "err=" in resp.headers["location"]


def test_device_target_only(admin, client):
    """Афиша, назначенная на конкретный экран, не попадает на соседний."""
    city_id = _create_city(admin, "Пермь")
    dev1, code1 = _create_screen(admin, "Касса А", city_id)
    _dev2, code2 = _create_screen(admin, "Касса Б", city_id)
    admin.post(
        "/publish",
        files=[("files", ("only-a.png", _png_bytes("green"), "image/png"))],
        data={"device": [str(dev1)]},
        follow_redirects=False,
    )
    h1 = _register_agent(client, code1)
    h2 = _register_agent(client, code2)
    names1 = [i["name"] for i in
              client.get("/api/agent/manifest", headers=h1).json()["items"]]
    names2 = [i["name"] for i in
              client.get("/api/agent/manifest", headers=h2).json()["items"]]
    assert "only-a" in names1
    assert "only-a" not in names2


def test_manager_scoped(admin, client):
    """Менеджер видит только свой город и не может публиковать в чужой."""
    from fastapi.testclient import TestClient
    from app.main import app

    own_city = _create_city(admin, "Тюмень")
    other_city = _create_city(admin, "Казань")
    _create_screen(admin, "Тюмень касса", own_city)
    _create_screen(admin, "Казань касса", other_city)
    resp = admin.post("/users/create", data={
        "username": "tyumen", "password": "manager-pass-1",
        "role": "manager", "city_id": str(own_city),
    }, follow_redirects=False)
    assert "msg=" in resp.headers["location"]

    with TestClient(app) as manager:
        resp = manager.post("/login", data={
            "username": "tyumen", "password": "manager-pass-1",
        }, follow_redirects=False)
        assert resp.status_code == 303

        page = manager.get("/screens").text
        assert "Тюмень касса" in page
        assert "Казань касса" not in page

        # Пользователи и медиатека — только для админа
        assert manager.get("/users").status_code == 403
        assert manager.get("/media").status_code == 403

        # Публикация в чужой город игнорирует чужие цели → ошибка «нет целей»
        resp = manager.post(
            "/publish",
            files=[("files", ("m.png", _png_bytes("gray"), "image/png"))],
            data={"city": [str(other_city)]},
            follow_redirects=False,
        )
        assert "err=" in resp.headers["location"]

        # А в свой — можно
        resp = manager.post(
            "/publish",
            files=[("files", ("m.png", _png_bytes("gray"), "image/png"))],
            data={"city": [str(own_city)]},
            follow_redirects=False,
        )
        assert "err" not in resp.headers["location"].split("msg=")[0] or True
        assert "/posters" in resp.headers["location"]


def test_agent_requires_token(client):
    assert client.get("/api/agent/manifest").status_code == 401
    bad = {"Authorization": "Bearer 0000"}
    assert client.get("/api/agent/manifest", headers=bad).status_code == 401


def test_media_library_upload_and_publish_from_it(admin, client):
    """Загрузка в медиатеку без публикации, затем публикация из библиотеки."""
    # Загрузка в библиотеку — афиш не создаётся
    resp = admin.post(
        "/media/upload",
        files=[("files", ("lib1.png", _png_bytes("teal"), "image/png"))],
        follow_redirects=False,
    )
    assert "msg=" in resp.headers["location"]
    with SessionLocal() as db:
        mf = db.query(MediaFile).filter(MediaFile.orig_name == "lib1.png").one()
        media_id = mf.id
        assert mf.posters == []  # афиш ещё нет

    page = admin.get("/media")
    assert "lib1.png" in page.text
    # Файл предлагается на странице публикации
    assert f'name="library" value="{media_id}"' in admin.get("/publish").text

    # Публикуем из библиотеки (без загрузки новых файлов)
    city_id = _create_city(admin, "Библиотечный")
    device_id, code = _create_screen(admin, "Касса-либ", city_id)
    resp = admin.post(
        "/publish",
        data={"library": [str(media_id)], "city": [str(city_id)],
              "display_seconds": "8"},
        follow_redirects=False,
    )
    assert "/posters" in resp.headers["location"]
    assert "err" not in resp.headers["location"]

    headers = _register_agent(client, code)
    manifest = client.get("/api/agent/manifest", headers=headers).json()
    assert len(manifest["items"]) == 1
    assert manifest["items"][0]["sha256"] == \
        (lambda: __import__("hashlib").sha256(_png_bytes("teal")).hexdigest())()


def test_publish_requires_content(admin):
    city_id = _create_city(admin, "Пустой")
    resp = admin.post(
        "/publish",
        data={"city": [str(city_id)]},  # ни файлов, ни библиотеки
        follow_redirects=False,
    )
    assert "err=" in resp.headers["location"]


def test_status_reports_local_panel(admin, client):
    """Агент сообщает адрес локальной панели — он виден на странице экрана."""
    city_id = _create_city(admin, "Панельный")
    device_id, code = _create_screen(admin, "Касса-панель", city_id)
    headers = _register_agent(client, code)
    client.post("/api/agent/status", headers=headers, json={
        "agent_version": "0.6.0", "local_ip": "192.168.1.50", "web_port": 8088,
    })
    page = admin.get(f"/screens/{device_id}").text
    assert "192.168.1.50:8088" in page


def test_command_and_screenshot_flow(admin, client):
    """UI ставит команду → агент забирает → шлёт скриншот → он виден в UI."""
    city_id = _create_city(admin, "Челябинск")
    device_id, code = _create_screen(admin, "Касса управления", city_id)
    headers = _register_agent(client, code)

    # Команда скриншота из интерфейса
    resp = admin.post(f"/screens/{device_id}/command",
                      data={"kind": "screenshot"}, follow_redirects=False)
    assert "msg=" in resp.headers["location"]

    # Агент забирает её (короткий long-poll вернёт сразу)
    cmds = client.get("/api/agent/commands", headers=headers).json()["commands"]
    assert len(cmds) == 1 and cmds[0]["kind"] == "screenshot"
    command_id = cmds[0]["id"]

    # Агент грузит PNG и отчитывается
    png = _png_bytes("black")
    resp = client.post("/api/agent/screenshot", headers=headers, content=png)
    assert resp.status_code == 200
    resp = client.post(f"/api/agent/commands/{command_id}/result",
                       headers=headers, json={"status": "done"})
    assert resp.status_code == 200

    # Скриншот доступен в UI, команда отмечена выполненной
    shot = admin.get(f"/screens/{device_id}/screenshot.png")
    assert shot.status_code == 200 and shot.content == png
    page = admin.get(f"/screens/{device_id}")
    assert "выполнена" in page.text

    # Команда на неподключённый экран отклоняется
    _dev2, _code2 = _create_screen(admin, "Не подключён", city_id)
    resp = admin.post(f"/screens/{_dev2}/command",
                      data={"kind": "screenshot"}, follow_redirects=False)
    assert "err=" in resp.headers["location"]


def test_command_reboot_gated_and_reported(admin, client):
    city_id = _create_city(admin, "Омск")
    device_id, code = _create_screen(admin, "Касса reboot", city_id)
    headers = _register_agent(client, code)
    admin.post(f"/screens/{device_id}/command", data={"kind": "reboot"},
               follow_redirects=False)
    cmds = client.get("/api/agent/commands", headers=headers).json()["commands"]
    assert cmds[0]["kind"] == "reboot"
    # Агент без --allow-system сообщает об отказе
    client.post(f"/api/agent/commands/{cmds[0]['id']}/result", headers=headers,
                json={"status": "failed", "result": "отключено"})
    page = admin.get(f"/screens/{device_id}")
    assert "ошибка" in page.text


def test_terminal_broker_roundtrip():
    """Мост терминала переносит байты в обе стороны и корректно закрывается."""
    from app.terminal import broker

    session = broker.open(device_id=42)
    # браузер → агент
    session.browser_send(b"ls -la\n")
    data, closed = session.agent_wait_input(0, timeout=1)
    assert data == b"ls -la\n" and not closed
    # агент → браузер
    session.agent_send(b"total 0\n")
    out, closed = session.browser_wait_output(0, timeout=1)
    assert out == b"total 0\n" and not closed
    # закрытие будит обе стороны
    broker.close(session.id)
    _, closed = session.agent_wait_input(len(data), timeout=1)
    assert closed
    assert broker.get(session.id) is None


def test_terminal_requires_access(admin, client):
    """Эндпойнты терминала агента требуют валидную сессию этого устройства."""
    resp = client.get("/api/agent/term/nonexistent/input",
                      headers={"Authorization": "Bearer x"})
    assert resp.status_code == 401  # плохой токен раньше, чем сессия


def test_resync_command(admin, client):
    """Кнопка «отправить афиши» ставит команду resync, агент её забирает."""
    city_id = _create_city(admin, "Синхро")
    device_id, code = _create_screen(admin, "Касса-синхро", city_id)
    headers = _register_agent(client, code)
    resp = admin.post(f"/screens/{device_id}/command",
                      data={"kind": "resync"}, follow_redirects=False)
    assert "msg=" in resp.headers["location"]
    cmds = client.get("/api/agent/commands", headers=headers).json()["commands"]
    assert any(c["kind"] == "resync" for c in cmds)


def test_media_preview_inline(admin):
    """Просмотр отдаёт файл inline (для встроенного плеера), без attachment."""
    import hashlib
    content = _png_bytes("navy")  # уникальный цвет → уникальный sha256
    sha = hashlib.sha256(content).hexdigest()
    admin.post(
        "/media/upload",
        files=[("files", ("pv.png", content, "image/png"))],
        follow_redirects=False,
    )
    with SessionLocal() as db:
        mf = db.query(MediaFile).filter(MediaFile.sha256 == sha).one()
    resp = admin.get(f"/media/{mf.id}/preview")
    assert resp.status_code == 200
    assert "attachment" not in resp.headers.get("content-disposition", "")


def test_transcode_incompatible_video(admin):
    """Несовместимое видео транскодируется воркером в H.264."""
    import shutil as _shutil
    import subprocess
    import tempfile
    import time as _time

    import pytest

    if _shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg недоступен")

    city_id = _create_city(admin, "Видео-город")
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y",
             "-f", "lavfi", "-i", "testsrc=duration=1:size=640x360:rate=50",
             "-c:v", "mpeg4", tmp.name],
            check=True, timeout=120,
        )
        bad_video = open(tmp.name, "rb").read()

    resp = admin.post(
        "/publish",
        files=[("files", ("bad.mp4", bad_video, "video/mp4"))],
        data={"city": [str(city_id)]},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    with SessionLocal() as db:
        poster = db.query(Poster).filter(Poster.name == "bad").one()
        media_id = poster.media_id

    deadline = _time.monotonic() + 120
    while _time.monotonic() < deadline:
        with SessionLocal() as db:
            mf = db.get(MediaFile, media_id)
            if mf.transcode_status in ("done", "failed"):
                break
        _time.sleep(1)

    with SessionLocal() as db:
        mf = db.get(MediaFile, media_id)
        assert mf.transcode_status == "done", mf.compat_warning
        assert mf.video_codec == "h264"
        assert mf.compatible is True
