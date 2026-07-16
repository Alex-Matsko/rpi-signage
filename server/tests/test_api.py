"""Интеграционные тесты: полный путь от загрузки афиши до манифеста агента."""
import io
from datetime import datetime, timedelta

from PIL import Image

from app.db import SessionLocal
from app.models import Device, Poster


def _png_bytes(color: str = "red") -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (320, 240), color).save(buf, format="PNG")
    return buf.getvalue()


def test_healthz(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_ui_requires_login(client):
    for path in ("/", "/posters", "/devices", "/media", "/users"):
        resp = client.get(path, follow_redirects=False)
        assert resp.status_code == 303, path
        assert resp.headers["location"] == "/login"


def test_login_wrong_password(client):
    resp = client.post(
        "/login", data={"username": "admin", "password": "wrong"},
        follow_redirects=False,
    )
    assert resp.status_code == 401


def test_upload_image_poster(admin):
    resp = admin.post(
        "/posters/upload",
        files={"file": ("test.png", _png_bytes(), "image/png")},
        data={"name": "Тестовая афиша", "display_seconds": "5"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "err" not in resp.headers["location"]

    page = admin.get("/posters")
    assert "Тестовая афиша" in page.text

    media_page = admin.get("/media")
    assert "test.png" in media_page.text


def test_upload_rejects_unknown_type(admin):
    resp = admin.post(
        "/posters/upload",
        files={"file": ("evil.exe", b"MZ....", "application/x-msdownload")},
        follow_redirects=False,
    )
    assert "err=" in resp.headers["location"]


def test_full_agent_flow(admin, client):
    # Афиша
    admin.post(
        "/posters/upload",
        files={"file": ("flow.png", _png_bytes("blue"), "image/png")},
        data={"name": "Афиша для агента", "display_seconds": "7"},
        follow_redirects=False,
    )
    # Плейлист с афишей
    resp = admin.post("/playlists/create", data={"name": "Основной"},
                      follow_redirects=False)
    playlist_id = int(resp.headers["location"].split("?")[0].rsplit("/", 1)[1])
    with SessionLocal() as db:
        poster = db.query(Poster).filter(Poster.name == "Афиша для агента").one()
    admin.post(f"/playlists/{playlist_id}/items/add",
               data={"poster_id": str(poster.id)}, follow_redirects=False)

    # Экран с плейлистом
    resp = admin.post(
        "/devices/create",
        data={"name": "ТВ тест", "group_id": "0", "playlist_id": str(playlist_id)},
        follow_redirects=False,
    )
    device_id = int(resp.headers["location"].split("?")[0].rsplit("/", 1)[1])
    with SessionLocal() as db:
        code = db.get(Device, device_id).pairing_code
    assert code

    # Регистрация агента по коду
    resp = client.post("/api/agent/register", json={"code": code})
    assert resp.status_code == 200
    token = resp.json()["token"]
    headers = {"Authorization": f"Bearer {token}"}

    # Повторная регистрация тем же кодом невозможна
    assert client.post("/api/agent/register", json={"code": code}).status_code == 404

    # Манифест
    manifest = client.get("/api/agent/manifest", headers=headers).json()
    assert len(manifest["items"]) == 1
    item = manifest["items"][0]
    assert item["name"] == "Афиша для агента"
    assert item["duration"] == 7

    # Скачивание контента
    resp = client.get(item["url"], headers=headers)
    assert resp.status_code == 200
    assert len(resp.content) == item["size"]

    # Heartbeat
    resp = client.post("/api/agent/status", headers=headers, json={
        "agent_version": "0.1.0",
        "uptime_sec": 120,
        "temp_c": 47.5,
        "disk_free_mb": 10000,
        "cache_done": 1,
        "cache_total": 1,
        "current": {"name": item["name"], "sha256": item["sha256"],
                    "since": "2026-07-16T12:00:00"},
    })
    assert resp.status_code == 200
    assert resp.json()["manifest_version"] == manifest["manifest_version"]

    # Статус виден в UI
    page = admin.get(f"/devices/{device_id}")
    assert "online" in page.text
    assert "Афиша для агента" in page.text

    # Истечение: афиша пропадает из манифеста
    with SessionLocal() as db:
        db_poster = db.get(Poster, poster.id)
        db_poster.expires_at = datetime.now() - timedelta(hours=1)
        db.commit()
    manifest = client.get("/api/agent/manifest", headers=headers).json()
    assert manifest["items"] == []


def test_agent_requires_token(client):
    assert client.get("/api/agent/manifest").status_code == 401
    bad = {"Authorization": "Bearer 0000"}
    assert client.get("/api/agent/manifest", headers=bad).status_code == 401


def test_users_crud(admin):
    resp = admin.post(
        "/users/create",
        data={"username": "editor", "password": "secret-pass-1"},
        follow_redirects=False,
    )
    assert "msg=" in resp.headers["location"]
    assert "editor" in admin.get("/users").text

    resp = admin.post(
        "/users/create",
        data={"username": "weak", "password": "123"},
        follow_redirects=False,
    )
    assert "err=" in resp.headers["location"]
