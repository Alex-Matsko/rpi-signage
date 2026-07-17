#!/usr/bin/env python3
"""Агент RPi Signage для Raspberry Pi 2/3/4.

Скачивает и кеширует контент с сервера, проигрывает его через mpv (KMS/DRM),
отправляет на сервер heartbeat с текущим состоянием. Работает автономно при
потере связи. Только стандартная библиотека Python 3.9+.

Использование:
  agent.py register --server https://signage.example.com --code AB12-CD34
  agent.py run [--dev]
"""
import argparse
import base64
import hashlib
import hmac
import html
import http.server
import json
import logging
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

AGENT_VERSION = "0.8.0"

log = logging.getLogger("signage")

DEFAULT_STATE_DIR = "/var/lib/signage"
DEFAULT_MPV_SOCKET = "/tmp/signage-mpv.sock"
DEFAULT_WEB_PORT = 8088
HEARTBEAT_SEC = 30
DOWNLOAD_CHUNK = 256 * 1024
MPV_ARGS = [
    "--idle=yes", "--fullscreen", "--no-terminal", "--no-osc", "--no-osd-bar",
    "--keep-open=no", "--loop-file=no", "--hwdec=auto-safe",
    "--really-quiet", "--no-input-default-bindings", "--osd-level=0",
]


# ---------------------------------------------------------------- настройки

class Settings:
    """Локальные настройки устройства (логин панели, аудио и пр.)."""

    def __init__(self, path: Path):
        self.path = path
        self.data: dict = {
            "web_user": "admin",
            "web_pass": _hash_pw("signage"),  # пароль по умолчанию
            "audio_output": "",   # id аудиовыхода системного бэкенда ("" = авто)
        }
        if path.exists():
            try:
                self.data.update(json.loads(path.read_text()))
            except (json.JSONDecodeError, OSError):
                pass

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2))
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    def check_password(self, user: str, password: str) -> bool:
        return (hmac.compare_digest(user, self.data.get("web_user", "")) and
                _verify_pw(password, self.data.get("web_pass", "")))

    def set_credentials(self, user: str, password: str) -> None:
        self.data["web_user"] = user
        self.data["web_pass"] = _hash_pw(password)
        self.save()


def _hash_pw(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 120_000)
    return "pbkdf2$" + base64.b64encode(salt).decode() + "$" + \
        base64.b64encode(dk).decode()


def _verify_pw(password: str, stored: str) -> bool:
    try:
        _, salt_b64, dk_b64 = stored.split("$")
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(dk_b64)
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 120_000)
        return hmac.compare_digest(dk, expected)
    except (ValueError, TypeError):
        return False


# ------------------------------------------------ системный бэкенд устройства

def _run(cmd: list[str], timeout: int = 20) -> tuple[int, str]:
    """Запускает системную команду, возвращает (код, stdout+stderr)."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr).strip()
    except FileNotFoundError:
        return 127, f"команда не найдена: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return 124, "превышено время ожидания"


def _audio_label(name: str) -> str:
    low = name.lower()
    if "hdmi" in low:
        return "HDMI"
    if ("headphone" in low or "analog" in low or "3.5" in low
            or "bcm2835" in low):
        return "Аналоговый (3.5 мм)"
    return name


def _parse_alsa_cards(text: str) -> list[dict]:
    """Разбирает /proc/asound/cards → [{id, label}]. Чистая функция для тестов.

    Формат строк:  ` 1 [Headphones     ]: bcm2835_headpho - bcm2835 Headphones`
    """
    cards = []
    for line in text.splitlines():
        m = re.match(r"\s*(\d+)\s+\[([^\]]+)\]:\s*(.*)", line)
        if not m:
            continue
        card_id = m.group(2).strip()
        rest = m.group(3)
        longname = rest.split(" - ", 1)[1] if " - " in rest else rest
        label = _audio_label(longname + " " + card_id)
        cards.append({"id": card_id, "label": f"{label} · {card_id}"})
    return cards


def _alsa_cards() -> list[dict]:
    try:
        return _parse_alsa_cards(Path("/proc/asound/cards").read_text())
    except OSError:
        return []


class SystemBackend:
    """Базовый бэкенд системных настроек (переопределяется под платформу)."""

    available = False

    def network_status(self) -> dict:
        return {"hostname": socket.gethostname(), "connections": [],
                "ip": local_ip_address()}

    def wifi_scan(self) -> list[dict]:
        return []

    def wifi_connect(self, ssid: str, password: str) -> tuple[bool, str]:
        return False, "Управление сетью недоступно на этом устройстве."

    def audio_outputs(self) -> list[dict]:
        return []

    def set_audio_output(self, output_id: str) -> tuple[bool, str]:
        return True, ""

    def set_hostname(self, name: str) -> tuple[bool, str]:
        return False, "Смена имени недоступна."

    def get_timezone(self) -> str:
        try:
            return time.strftime("%Z")
        except Exception:
            return "—"

    def list_timezones(self) -> list[str]:
        return []

    def set_timezone(self, tz: str) -> tuple[bool, str]:
        return False, "Смена часового пояса недоступна."


class LinuxBackend(SystemBackend):
    """RPi OS Bookworm / Debian / Ubuntu на NUC — через NetworkManager и wpctl."""

    available = True

    def network_status(self) -> dict:
        conns = []
        rc, out = _run(["nmcli", "-t", "-f", "DEVICE,TYPE,STATE,CONNECTION",
                        "device", "status"])
        if rc == 0:
            for line in out.splitlines():
                parts = line.split(":")
                if len(parts) >= 4 and parts[1] in ("ethernet", "wifi"):
                    conns.append({
                        "device": parts[0], "type": parts[1],
                        "state": parts[2], "name": parts[3],
                    })
        return {"hostname": socket.gethostname(), "connections": conns,
                "ip": local_ip_address()}

    def wifi_scan(self) -> list[dict]:
        _run(["nmcli", "device", "wifi", "rescan"], timeout=15)
        rc, out = _run(["nmcli", "-t", "-f", "SSID,SIGNAL,SECURITY",
                        "device", "wifi", "list"])
        nets, seen = [], set()
        if rc == 0:
            for line in out.splitlines():
                parts = line.split(":")
                ssid = parts[0].strip()
                if not ssid or ssid in seen:
                    continue
                seen.add(ssid)
                nets.append({
                    "ssid": ssid,
                    "signal": parts[1] if len(parts) > 1 else "",
                    "secure": bool(parts[2].strip()) if len(parts) > 2 else True,
                })
        return sorted(nets, key=lambda n: int(n["signal"] or 0), reverse=True)

    def wifi_connect(self, ssid: str, password: str) -> tuple[bool, str]:
        cmd = ["nmcli", "device", "wifi", "connect", ssid]
        if password:
            cmd += ["password", password]
        rc, out = _run(cmd, timeout=45)
        return rc == 0, out

    def audio_outputs(self) -> list[dict]:
        """Список аудиовыходов через ALSA (/proc/asound/cards).

        Работает на headless-системе без сессии PipeWire/PulseAudio —
        именно так агент запущен как systemd-сервис. id — строка устройства
        для mpv (`alsa/plughw:CARD=<id>`).
        """
        outs = []
        for card in _alsa_cards():
            outs.append({
                "id": f"alsa/plughw:CARD={card['id']}",
                "label": card["label"],
            })
        if outs:
            return outs
        # Резерв: если работает PipeWire/Pulse — берём его список
        rc, out = _run(["pactl", "list", "short", "sinks"])
        if rc == 0:
            for line in out.splitlines():
                cols = line.split("\t")
                if len(cols) >= 2:
                    outs.append({"id": f"pulse/{cols[1]}", "label": cols[1]})
        return outs

    def set_audio_output(self, output_id: str) -> tuple[bool, str]:
        # Применяется самим mpv (--audio-device); системного действия не нужно.
        return True, ""

    def set_hostname(self, name: str) -> tuple[bool, str]:
        rc, out = _run(["sudo", "-n", "hostnamectl", "set-hostname", name])
        return rc == 0, out

    def get_timezone(self) -> str:
        rc, out = _run(["timedatectl", "show", "-p", "Timezone", "--value"])
        return out if rc == 0 and out else "—"

    def list_timezones(self) -> list[str]:
        rc, out = _run(["timedatectl", "list-timezones"])
        return out.splitlines() if rc == 0 else []

    def set_timezone(self, tz: str) -> tuple[bool, str]:
        rc, out = _run(["sudo", "-n", "timedatectl", "set-timezone", tz])
        return rc == 0, out


class MockBackend(SystemBackend):
    """Dev-режим: без реальных системных вызовов, для отладки панели."""

    available = True

    def __init__(self):
        self._hostname = socket.gethostname()
        self._tz = "Europe/Moscow"

    def network_status(self) -> dict:
        return {
            "hostname": self._hostname,
            "ip": local_ip_address(),
            "connections": [
                {"device": "eth0", "type": "ethernet",
                 "state": "connected", "name": "Проводное (dev)"},
                {"device": "wlan0", "type": "wifi",
                 "state": "disconnected", "name": ""},
            ],
        }

    def wifi_scan(self) -> list[dict]:
        return [
            {"ssid": "Kassa-Office", "signal": "82", "secure": True},
            {"ssid": "Guest-WiFi", "signal": "55", "secure": False},
        ]

    def wifi_connect(self, ssid: str, password: str) -> tuple[bool, str]:
        return True, f"[dev] подключение к «{ssid}» имитировано"

    def audio_outputs(self) -> list[dict]:
        return [
            {"id": "hdmi", "label": "HDMI (dev)"},
            {"id": "analog", "label": "Аналоговый 3.5 мм (dev)"},
        ]

    def set_hostname(self, name: str) -> tuple[bool, str]:
        self._hostname = name
        return True, ""

    def get_timezone(self) -> str:
        return self._tz

    def list_timezones(self) -> list[str]:
        return ["Europe/Moscow", "Europe/Kaliningrad", "Asia/Yekaterinburg",
                "Asia/Novosibirsk", "Asia/Krasnoyarsk", "Asia/Vladivostok",
                "UTC"]

    def set_timezone(self, tz: str) -> tuple[bool, str]:
        self._tz = tz
        return True, ""


def make_backend(dev: bool) -> SystemBackend:
    if dev:
        return MockBackend()
    if sys.platform.startswith("linux"):
        # Звук (ALSA) и часовой пояс работают всегда; Wi-Fi — если есть nmcli
        return LinuxBackend()
    log.warning("Не Linux — системные настройки в панели ограничены")
    return SystemBackend()


def local_ip_address() -> str:
    """Определяет локальный IP устройства (адрес исходящего интерфейса)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


# ---------------------------------------------------------------- состояние

class State:
    """Разделяемое состояние: манифест, текущий элемент, кеш."""

    def __init__(self, state_dir: Path):
        self.lock = threading.Lock()
        # Будит heartbeat-цикл при смене афиши или обновлении кеша,
        # чтобы сервер видел актуальное состояние без задержки.
        self.wake = threading.Event()
        self.state_dir = state_dir
        self.cache_dir = state_dir / "cache"
        self.manifest_path = state_dir / "manifest.json"
        self.auth_path = state_dir / "auth.json"
        self.items: list[dict] = []       # элементы манифеста с локальными путями
        self.cache_total = 0
        self.cache_done = 0
        self.current: dict | None = None  # {"name","sha256","since"}
        self.started = time.monotonic()
        self.settings_path = state_dir / "settings.json"
        self.web_port = 0  # 0 = панель выключена

    def load_saved_manifest(self) -> None:
        if self.manifest_path.exists():
            try:
                data = json.loads(self.manifest_path.read_text())
                self.apply_manifest(data.get("items", []))
                log.info("Загружен сохранённый манифест: %d элементов",
                         len(self.items))
            except (json.JSONDecodeError, OSError) as e:
                log.warning("Не удалось прочитать сохранённый манифест: %s", e)

    def apply_manifest(self, items: list[dict]) -> None:
        with self.lock:
            self.cache_total = len(items)
            ready = []
            done = 0
            for item in items:
                path = self.cache_dir / item["sha256"]
                if path.exists():
                    done += 1
                    ready.append({**item, "path": str(path)})
            self.cache_done = done
            self.items = ready
        self.wake.set()

    def set_current(self, item: dict | None) -> None:
        with self.lock:
            if item is None:
                self.current = None
            else:
                self.current = {
                    "name": item["name"],
                    "sha256": item["sha256"],
                    "since": datetime.now().replace(microsecond=0).isoformat(),
                }
        self.wake.set()

    def playable_items(self) -> list[dict]:
        """Элементы, действующие прямо сейчас (по локальным часам)."""
        t = datetime.now()
        with self.lock:
            return [item for item in self.items if item_is_active(item, t)]


def item_is_active(item: dict, t: datetime) -> bool:
    """Действует ли элемент манифеста в момент t (локальные часы устройства).

    Учитывает даты начала/истечения, дни недели (битовая маска, бит 0 = пн)
    и ежедневное окно показа, в том числе через полночь (22:00–06:00).
    """
    starts = item.get("starts_at")
    if starts and datetime.fromisoformat(starts) > t:
        return False
    expires = item.get("expires_at")
    if expires and datetime.fromisoformat(expires) <= t:
        return False
    mask = item.get("weekdays")
    if mask and not (mask >> t.weekday()) & 1:
        return False
    frm, until = item.get("daily_from"), item.get("daily_until")
    if frm or until:
        cur = t.strftime("%H:%M")
        if frm and until:
            if frm <= until:
                return frm <= cur < until
            return cur >= frm or cur < until  # окно через полночь
        if frm:
            return cur >= frm
        return cur < until
    return True


# ---------------------------------------------------------------- сервер

class ServerClient:
    def __init__(self, server: str, token: str | None):
        self.server = server.rstrip("/")
        self.token = token

    def _request(self, method: str, path: str, payload: dict | None = None,
                 timeout: int = 30):
        req = urllib.request.Request(self.server + path, method=method)
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        data = None
        if payload is not None:
            data = json.dumps(payload).encode()
            req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, data=data, timeout=timeout) as resp:
            return json.loads(resp.read())

    def _raw(self, method: str, path: str, body: bytes | None = None,
             timeout: int = 30) -> bytes:
        req = urllib.request.Request(self.server + path, method=method,
                                     data=body)
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        if body is not None:
            req.add_header("Content-Type", "application/octet-stream")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()

    def register(self, code: str) -> dict:
        return self._request("POST", "/api/agent/register", {"code": code})

    def manifest(self) -> dict:
        return self._request("GET", "/api/agent/manifest")

    def send_status(self, payload: dict) -> dict:
        return self._request("POST", "/api/agent/status", payload, timeout=15)

    def get_commands(self) -> list[dict]:
        return self._request("GET", "/api/agent/commands", timeout=30).get(
            "commands", [])

    def command_result(self, command_id: int, status: str,
                       result: str = "") -> None:
        self._request("POST", f"/api/agent/commands/{command_id}/result",
                      {"status": status, "result": result}, timeout=15)

    def upload_screenshot(self, png: bytes) -> None:
        self._raw("POST", "/api/agent/screenshot", png, timeout=30)

    def term_input(self, session_id: str, after: int) -> dict:
        return self._request(
            "GET", f"/api/agent/term/{session_id}/input?after={after}",
            timeout=30)

    def term_output(self, session_id: str, data: bytes) -> None:
        self._raw("POST", f"/api/agent/term/{session_id}/output", data,
                  timeout=15)

    def term_close(self, session_id: str) -> None:
        try:
            self._request("POST", f"/api/agent/term/{session_id}/close",
                          {}, timeout=10)
        except Exception:
            pass

    def download(self, url_path: str, dest: Path) -> None:
        """Скачивает файл с докачкой (.part + Range) и переименовывает."""
        part = dest.with_suffix(".part")
        offset = part.stat().st_size if part.exists() else 0
        req = urllib.request.Request(self.server + url_path)
        req.add_header("Authorization", f"Bearer {self.token}")
        if offset:
            req.add_header("Range", f"bytes={offset}-")
        mode = "ab"
        try:
            resp = urllib.request.urlopen(req, timeout=60)
        except urllib.error.HTTPError as e:
            if e.code == 416:  # диапазон вне файла — начать заново
                part.unlink(missing_ok=True)
                offset = 0
                resp = urllib.request.urlopen(
                    urllib.request.Request(
                        self.server + url_path,
                        headers={"Authorization": f"Bearer {self.token}"},
                    ), timeout=60)
            else:
                raise
        if offset and resp.status != 206:
            mode = "wb"  # сервер не поддержал Range — качаем целиком
        with resp, open(part, mode) as f:
            while chunk := resp.read(DOWNLOAD_CHUNK):
                f.write(chunk)
        # Проверка целостности: имя файла = его sha256
        sha = hashlib.sha256()
        with open(part, "rb") as f:
            while chunk := f.read(1024 * 1024):
                sha.update(chunk)
        if sha.hexdigest() != dest.name:
            part.unlink(missing_ok=True)
            raise IOError(f"Хеш не совпал для {dest.name}")
        part.rename(dest)


# ---------------------------------------------------------------- плееры

class MockPlayer:
    """Dev-режим: пишет в лог, что «играет», без реального вывода."""

    def play(self, item: dict, stop: threading.Event) -> None:
        duration = item.get("duration") or 10
        log.info("[mock] ▶ %s (%s, %s сек)", item["name"], item["kind"], duration)
        stop.wait(timeout=duration)

    def play_idle(self, stop: threading.Event, placeholder: Path | None) -> None:
        log.info("[mock] ▶ нет контента (заставка)")
        stop.wait(timeout=10)

    def screenshot(self) -> bytes:
        """Заглушка-PNG (1×1) — чтобы протокол скриншотов работал в dev-режиме."""
        return _tiny_png()

    def set_audio_device(self, device: str) -> None:
        log.info("[mock] аудиовыход → %s", device or "авто")

    def shutdown(self) -> None:
        pass


# Минимальный валидный PNG 1×1 (серый пиксель) — без внешних зависимостей
_TINY_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108020000009077"
    "53de0000000c49444154789c6360606000000004000160f0a2b40000000049"
    "454e44ae426082"
)


def _tiny_png() -> bytes:
    return _TINY_PNG


class MpvPlayer:
    """Управляет одним долгоживущим процессом mpv через JSON IPC.

    Элементы сменяются командой loadfile — без перезапуска процесса,
    поэтому между афишами нет чёрного экрана.
    """

    def __init__(self, socket_path: str, extra_args: list[str],
                 audio_device: str = ""):
        self.socket_path = socket_path
        self.extra_args = extra_args
        self.audio_device = audio_device      # применённое устройство
        self._desired_audio = audio_device    # выбранное в панели (др. поток)
        self.proc: subprocess.Popen | None = None
        self.sock: socket.socket | None = None
        self._req_id = 0

    def set_audio_device(self, device: str) -> None:
        """Смена аудиовыхода из веб-панели (перечитается плеером-потоком)."""
        self._desired_audio = device or ""

    def _ensure_running(self) -> None:
        # Аудиовыход сменили в панели — перезапустить mpv с новым устройством
        if self._desired_audio != self.audio_device:
            self.audio_device = self._desired_audio
            self._close()
        if self.proc is not None and self.proc.poll() is None and self.sock:
            return
        self._close()
        Path(self.socket_path).unlink(missing_ok=True)
        audio_args = ([f"--audio-device={self.audio_device}"]
                      if self.audio_device else [])
        cmd = ["mpv", f"--input-ipc-server={self.socket_path}",
               *MPV_ARGS, *audio_args, *self.extra_args]
        log.info("Запускаю mpv: %s", " ".join(cmd))
        self.proc = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(50):  # ждём сокет до 5 секунд
            if Path(self.socket_path).exists():
                break
            if self.proc.poll() is not None:
                raise RuntimeError("mpv завершился сразу после запуска")
            time.sleep(0.1)
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.connect(self.socket_path)
        self.sock.settimeout(1.0)
        self._buf = b""

    def _send(self, command: list) -> None:
        self._req_id += 1
        msg = json.dumps({"command": command, "request_id": self._req_id})
        assert self.sock is not None
        self.sock.sendall(msg.encode() + b"\n")

    def _events(self, timeout: float):
        """Итератор событий mpv до истечения timeout."""
        deadline = time.monotonic() + timeout
        assert self.sock is not None
        while time.monotonic() < deadline:
            try:
                data = self.sock.recv(4096)
            except socket.timeout:
                yield None
                continue
            if not data:
                raise ConnectionError("mpv закрыл IPC-сокет")
            self._buf += data
            while b"\n" in self._buf:
                line, self._buf = self._buf.split(b"\n", 1)
                if line.strip():
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError:
                        pass

    def _play_file(self, path: str, image_duration: float | None,
                   timeout: float, stop: threading.Event) -> None:
        self._ensure_running()
        if image_duration is not None:
            self._send(["set_property", "image-display-duration",
                        image_duration])
        self._send(["loadfile", path, "replace"])
        for event in self._events(timeout):
            if stop.is_set():
                return
            if event and event.get("event") == "end-file":
                reason = event.get("reason", "")
                if reason != "redirect":
                    return

    def play(self, item: dict, stop: threading.Event) -> None:
        duration = item.get("duration") or 10
        image_duration = duration if item["kind"] == "image" else None
        try:
            self._play_file(item["path"], image_duration,
                            timeout=duration + 30, stop=stop)
        except (OSError, RuntimeError, ConnectionError) as e:
            log.error("Ошибка mpv (%s), перезапуск: %s", item["name"], e)
            self._close()
            stop.wait(timeout=3)

    def play_idle(self, stop: threading.Event, placeholder: Path | None) -> None:
        try:
            if placeholder and placeholder.exists():
                self._play_file(str(placeholder), image_duration=15,
                                timeout=20, stop=stop)
            else:
                self._ensure_running()
                self._send(["stop"])
                stop.wait(timeout=15)
        except (OSError, RuntimeError, ConnectionError) as e:
            log.error("Ошибка mpv в режиме ожидания: %s", e)
            self._close()
            stop.wait(timeout=5)

    def screenshot(self) -> bytes:
        """Кадр текущего экрана через отдельное IPC-подключение к mpv."""
        if self.proc is None or self.proc.poll() is not None:
            raise RuntimeError("mpv не запущен")
        tmp = Path(f"/tmp/signage-shot-{os.getpid()}.png")
        s = socket.socket(socket.AF_UNIX)
        s.settimeout(5.0)
        s.connect(self.socket_path)
        try:
            s.sendall(json.dumps(
                {"command": ["screenshot-to-file", str(tmp), "video"]}
            ).encode() + b"\n")
            buf = b""
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                try:
                    data = s.recv(4096)
                except socket.timeout:
                    break
                if not data:
                    break
                buf += data
                if b'"error"' in buf:
                    break
        finally:
            s.close()
        if not tmp.exists():
            raise RuntimeError("mpv не создал скриншот")
        png = tmp.read_bytes()
        tmp.unlink(missing_ok=True)
        return png

    def _close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None

    def shutdown(self) -> None:
        self._close()


# ---------------------------------------------------------------- команды

def run_terminal(client: "ServerClient", session_id: str,
                 stop_root: threading.Event) -> None:
    """Запускает интерактивный shell в pty и связывает его с сервером."""
    import pty

    pid, fd = pty.fork()
    if pid == 0:  # дочерний процесс: становится shell
        os.environ["TERM"] = "xterm-256color"
        shell = os.environ.get("SHELL", "/bin/bash")
        try:
            os.execvp(shell, [shell, "-i"])
        except OSError:
            os.execvp("/bin/sh", ["/bin/sh", "-i"])
        os._exit(1)

    log.info("Терминал %s открыт (pid %d)", session_id[:8], pid)
    closed = threading.Event()

    def reader():
        try:
            while not closed.is_set():
                try:
                    data = os.read(fd, 4096)
                except OSError:
                    break
                if not data:
                    break
                try:
                    client.term_output(session_id, data)
                except Exception:
                    break
        finally:
            closed.set()

    threading.Thread(target=reader, daemon=True).start()

    consumed = 0
    try:
        while not closed.is_set() and not stop_root.is_set():
            try:
                resp = client.term_input(session_id, consumed)
            except Exception:
                time.sleep(1)
                continue
            data = resp.get("data", "").encode("latin1")
            if data:
                try:
                    os.write(fd, data)
                except OSError:
                    break
                consumed = resp.get("consumed", consumed + len(data))
            if resp.get("closed"):
                break
    finally:
        closed.set()
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
        except OSError:
            pass
        client.term_close(session_id)
        log.info("Терминал %s закрыт", session_id[:8])


def handle_command(client: "ServerClient", player, cmd: dict,
                   allow_system: bool, stop: threading.Event) -> None:
    kind = cmd["kind"]
    cid = cmd["id"]
    try:
        if kind == "screenshot":
            png = player.screenshot()
            client.upload_screenshot(png)
            client.command_result(cid, "done", "Скриншот загружен")

        elif kind == "restart_agent":
            client.command_result(cid, "done", "Агент перезапускается")
            log.info("Команда: перезапуск агента")
            stop.set()
            os._exit(0)  # systemd поднимет заново

        elif kind == "reboot":
            if not allow_system:
                client.command_result(
                    cid, "failed",
                    "Перезагрузка отключена (агент запущен без --allow-system)")
                return
            client.command_result(cid, "done", "Raspberry Pi перезагружается")
            log.info("Команда: перезагрузка устройства")
            subprocess.run(["sudo", "-n", "reboot"], timeout=15)

        elif kind == "shell":
            session_id = cmd.get("param")
            if not session_id:
                client.command_result(cid, "failed", "Нет id сессии")
                return
            client.command_result(cid, "done", "Сессия терминала открыта")
            threading.Thread(
                target=run_terminal, args=(client, session_id, stop),
                daemon=True).start()
        else:
            client.command_result(cid, "failed", f"Неизвестная команда: {kind}")
    except Exception as e:
        log.error("Команда %s (%s) не выполнена: %s", kind, cid, e)
        try:
            client.command_result(cid, "failed", str(e))
        except Exception:
            pass


def command_loop(client: "ServerClient", player, allow_system: bool,
                 stop: threading.Event) -> None:
    """Долгий опрос команд управления экраном."""
    while not stop.is_set():
        try:
            cmds = client.get_commands()
        except Exception as e:
            log.debug("Опрос команд не удался: %s", e)
            stop.wait(timeout=5)
            continue
        for cmd in cmds:
            handle_command(client, player, cmd, allow_system, stop)


# ---------------------------------------------------------------- метрики

def read_temp_c() -> float | None:
    try:
        raw = Path("/sys/class/thermal/thermal_zone0/temp").read_text().strip()
        return int(raw) / 1000.0
    except (OSError, ValueError):
        return None


def read_uptime_sec(state: State) -> int:
    try:
        return int(float(Path("/proc/uptime").read_text().split()[0]))
    except (OSError, ValueError, IndexError):
        return int(time.monotonic() - state.started)


def disk_free_mb(path: Path) -> int | None:
    try:
        return shutil.disk_usage(path).free // (1024 * 1024)
    except OSError:
        return None


# ---------------------------------------------------------------- self-update

def self_update(client: ServerClient, server_version: str) -> None:
    """Заменяет собственный файл версией с сервера и перезапускает процесс.

    Вызывается только с флагом --self-update (ставится install.sh):
    в dev-режиме и при запуске из репозитория обновление выключено.
    """
    own_path = Path(__file__).resolve()
    log.info("Обновление агента %s -> %s", AGENT_VERSION, server_version)
    req = urllib.request.Request(client.server + "/agent.py")
    with urllib.request.urlopen(req, timeout=60) as resp:
        source = resp.read()
    # Проверяем, что скачался корректный Python с ожидаемой версией
    compile(source, str(own_path), "exec")
    if f'AGENT_VERSION = "{server_version}"'.encode() not in source:
        raise ValueError("версия в скачанном agent.py не совпадает с манифестом")
    tmp = own_path.with_suffix(".new")
    tmp.write_bytes(source)
    tmp.chmod(0o755)
    tmp.rename(own_path)
    log.info("Агент обновлён, выходим — systemd перезапустит новую версию "
             "и корректно завершит mpv")
    os._exit(0)


# ------------------------------------------------ хранилище (локальный кеш)

def cache_report(state: State) -> dict:
    """Сводка по кешу: свободное место и список закешированных файлов."""
    try:
        usage = shutil.disk_usage(state.cache_dir)
        total_mb, free_mb = usage.total // (1024 * 1024), usage.free // (1024 * 1024)
    except OSError:
        total_mb = free_mb = 0
    names = {}
    with state.lock:
        for it in state.items:
            names[it["sha256"]] = it["name"]
    files = []
    used = 0
    if state.cache_dir.exists():
        for f in sorted(state.cache_dir.iterdir()):
            if f.is_file() and f.suffix != ".part":
                size = f.stat().st_size
                used += size
                files.append({
                    "sha256": f.name,
                    "name": names.get(f.name, "(нет в текущем плейлисте)"),
                    "size_mb": round(size / (1024 * 1024), 1),
                    "in_use": f.name in names,
                })
    return {"total_mb": total_mb, "free_mb": free_mb,
            "used_mb": round(used / (1024 * 1024), 1), "files": files}


def delete_cached(state: State, sha256: str) -> bool:
    """Удаляет один файл из кеша (безопасно: только внутри cache_dir)."""
    if "/" in sha256 or "\\" in sha256 or ".." in sha256:
        return False
    target = state.cache_dir / sha256
    if target.exists() and target.parent == state.cache_dir:
        target.unlink()
        with state.lock:
            state.items = [i for i in state.items if i["sha256"] != sha256]
            state.cache_done = len(state.items)
        return True
    return False


def clear_cache(state: State) -> int:
    """Полностью очищает кеш; действующий контент докачается при следующем опросе."""
    removed = 0
    if state.cache_dir.exists():
        for f in state.cache_dir.iterdir():
            if f.is_file():
                f.unlink()
                removed += 1
    with state.lock:
        state.items = []
        state.cache_done = 0
    return removed


# ---------------------------------------------------------------- циклы

def sync_loop(client: ServerClient, state: State, poll_interval: int,
              stop: threading.Event, allow_self_update: bool = False) -> None:
    """Опрос манифеста, докачка кеша, очистка сирот."""
    last_version = None
    while not stop.is_set():
        try:
            manifest = client.manifest()
            server_agent = manifest.get("agent_version")
            if (allow_self_update and server_agent
                    and server_agent != AGENT_VERSION):
                try:
                    self_update(client, server_agent)  # не возвращается
                except Exception as e:
                    log.error("Self-update не удался: %s", e)
            version = manifest.get("manifest_version")
            poll_interval = manifest.get("poll_interval", poll_interval)
            items = manifest.get("items", [])
            state.apply_manifest(items)  # сразу учесть удалённые элементы

            wanted = {i["sha256"] for i in items}
            for item in items:
                if stop.is_set():
                    break
                dest = state.cache_dir / item["sha256"]
                if dest.exists():
                    continue
                log.info("Скачиваю %s (%s)", item["name"], item["sha256"][:12])
                try:
                    client.download(item["url"], dest)
                    state.apply_manifest(items)
                except Exception as e:
                    log.error("Не удалось скачать %s: %s", item["name"], e)

            # Сироты: файлы кеша, которых больше нет в манифесте
            for f in state.cache_dir.iterdir():
                if f.suffix == ".part":
                    continue
                if f.name not in wanted:
                    log.info("Удаляю из кеша: %s", f.name[:12])
                    f.unlink(missing_ok=True)

            if version != last_version:
                log.info("Манифест обновлён (%s): %d элементов",
                         version, len(items))
                last_version = version
            state.manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False))
        except Exception as e:
            log.warning("Сервер недоступен (%s) — работаю из кеша", e)
        stop.wait(timeout=poll_interval)


def heartbeat_loop(client: ServerClient, state: State,
                   stop: threading.Event) -> None:
    while not stop.is_set():
        with state.lock:
            current = dict(state.current) if state.current else None
            payload = {
                "agent_version": AGENT_VERSION,
                "uptime_sec": read_uptime_sec(state),
                "temp_c": read_temp_c(),
                "disk_free_mb": disk_free_mb(state.cache_dir),
                "cache_done": state.cache_done,
                "cache_total": state.cache_total,
                "current": current,
                "local_ip": local_ip_address(),
                "web_port": state.web_port,
            }
        try:
            client.send_status(payload)
        except Exception as e:
            log.debug("Heartbeat не доставлен: %s", e)
        state.wake.clear()
        state.wake.wait(timeout=HEARTBEAT_SEC)


def playback_loop(player, state: State, placeholder: Path | None,
                  stop: threading.Event) -> None:
    """Крутит по кругу действующие элементы манифеста."""
    index = 0
    while not stop.is_set():
        items = state.playable_items()
        if not items:
            state.set_current(None)
            player.play_idle(stop, placeholder)
            continue
        index = index % len(items)
        item = items[index]
        state.set_current(item)
        player.play(item, stop)
        index += 1
    player.shutdown()


# ------------------------------------------------ локальная веб-панель устройства

_PAGE_CSS = """
*{box-sizing:border-box}body{margin:0;font:15px/1.5 system-ui,sans-serif;
background:#0f141d;color:#e6ebf4}header{background:#171f2b;
border-bottom:1px solid #2b3648;padding:12px 20px;display:flex;gap:16px;
align-items:center;flex-wrap:wrap}header b{font-size:16px}
header a{color:#90a0b6;text-decoration:none}header a:hover{color:#e6ebf4}
main{max-width:760px;margin:0 auto;padding:22px 16px 60px}
h1{font-size:20px}h2{font-size:16px;margin-top:26px}
.card{background:#171f2b;border:1px solid #2b3648;border-radius:10px;
padding:16px;margin-bottom:14px}
label{display:block;font-size:13px;color:#90a0b6;margin:10px 0 3px}
input,select{width:100%;padding:9px 11px;border:1px solid #2b3648;border-radius:7px;
background:#0f141d;color:#e6ebf4;font:inherit}
button{margin-top:12px;padding:9px 16px;border:0;border-radius:7px;
background:#3452c8;color:#fff;font:inherit;font-weight:600;cursor:pointer}
button.sec{background:#2b3648}button.danger{background:#bb3737}
table{width:100%;border-collapse:collapse}td,th{text-align:left;padding:6px 8px;
border-bottom:1px solid #2b3648;font-size:14px}
.flash{padding:10px 14px;border-radius:8px;margin-bottom:14px}
.flash.ok{background:#143526;color:#4cc287}.flash.err{background:#3d1c1c;color:#e57373}
.muted{color:#90a0b6}.kv{display:flex;justify-content:space-between;
padding:5px 0;border-bottom:1px solid #232d3d;font-size:14px}
"""

_NAV = (
    '<header><b>📺 Signage</b>'
    '<a href="/">Обзор</a><a href="/server">Сервер</a>'
    '<a href="/network">Сеть</a><a href="/audio">Звук</a>'
    '<a href="/storage">Хранилище</a><a href="/system">Система</a></header>'
)


class WebContext:
    def __init__(self, settings, backend, state, get_status, actions,
                 auth_info, bind):
        self.settings = settings
        self.backend = backend
        self.state = state
        self.get_status = get_status   # () -> dict со сводкой агента
        self.actions = actions         # {"restart_agent":fn, "reboot":fn}
        self.auth_info = auth_info      # () -> dict|None (server, device_id)
        self.bind = bind                # (server_url, code) -> (ok, msg)


def _page(title: str, body: str, flash: str = "", flash_cls: str = "ok") -> bytes:
    flash_html = f'<div class="flash {flash_cls}">{html.escape(flash)}</div>' if flash else ""
    return (
        f"<!doctype html><html lang=ru><head><meta charset=utf-8>"
        f"<meta name=viewport content='width=device-width,initial-scale=1'>"
        f"<title>{html.escape(title)} — Signage</title><style>{_PAGE_CSS}</style>"
        f"</head><body>{_NAV}<main><h1>{html.escape(title)}</h1>"
        f"{flash_html}{body}</main></body></html>"
    ).encode()


class _WebHandler(http.server.BaseHTTPRequestHandler):
    server_version = "SignageAgent"

    @property
    def ctx(self) -> WebContext:
        return self.server.ctx  # type: ignore[attr-defined]

    def log_message(self, *_args):
        pass  # не засорять журнал агента

    # --- аутентификация ---
    def _authed(self) -> bool:
        header = self.headers.get("Authorization", "")
        if header.startswith("Basic "):
            try:
                raw = base64.b64decode(header[6:]).decode()
                user, _, password = raw.partition(":")
            except (ValueError, UnicodeDecodeError):
                return False
            return self.ctx.settings.check_password(user, password)
        return False

    def _require_auth(self) -> bool:
        if self._authed():
            return True
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Signage device"')
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write("Требуется вход".encode())
        return False

    def _form(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode() if length else ""
        return {k: v[0] for k, v in urllib.parse.parse_qs(body).items()}

    def _send(self, data: bytes, code: int = 200,
              ctype: str = "text/html; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, location: str):
        # Заголовок Location кодируется latin-1, поэтому кириллицу в query
        # (например, текст сообщения) процент-кодируем. `%` в safe — чтобы
        # уже закодированные значения не кодировались повторно.
        safe = urllib.parse.quote(location, safe="/?:&=+%#,")
        self.send_response(303)
        self.send_header("Location", safe)
        self.end_headers()

    # --- маршрутизация ---
    def do_GET(self):
        if not self._require_auth():
            return
        path = urllib.parse.urlparse(self.path).path
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        flash = (query.get("msg", [""])[0], "ok") if "msg" in query else \
                (query.get("err", [""])[0], "err") if "err" in query else ("", "ok")
        routes = {
            "/": self._page_overview,
            "/server": self._page_server,
            "/network": self._page_network,
            "/audio": self._page_audio,
            "/storage": self._page_storage,
            "/system": self._page_system,
        }
        handler = routes.get(path)
        if handler is None:
            self._send(_page("Не найдено", "<p>Страница не найдена.</p>"), 404)
            return
        self._send(handler(flash[0], flash[1]))

    def do_POST(self):
        if not self._require_auth():
            return
        path = urllib.parse.urlparse(self.path).path
        form = self._form()
        try:
            if path == "/network/wifi":
                ok, out = self.ctx.backend.wifi_connect(
                    form.get("ssid", ""), form.get("password", ""))
                self._redirect("/network?" + urllib.parse.urlencode(
                    {"msg": "Подключение выполнено. " + out} if ok
                    else {"err": "Не удалось подключиться. " + out}))
            elif path == "/audio":
                out_id = form.get("audio_output", "")
                ok, out = self.ctx.backend.set_audio_output(out_id)
                if ok:
                    self.ctx.settings.data["audio_output"] = out_id
                    self.ctx.settings.save()
                    setter = self.ctx.actions.get("set_audio")
                    if setter:
                        setter(out_id)  # mpv переключит выход
                self._redirect("/audio?" + urllib.parse.urlencode(
                    {"msg": "Аудиовыход сохранён."} if ok
                    else {"err": "Не удалось переключить звук. " + out}))
            elif path == "/system/hostname":
                ok, out = self.ctx.backend.set_hostname(
                    form.get("hostname", "").strip())
                self._redirect("/system?" + urllib.parse.urlencode(
                    {"msg": "Имя устройства изменено."} if ok
                    else {"err": "Не удалось изменить имя. " + out}))
            elif path == "/system/password":
                user = form.get("user", "").strip() or "admin"
                pw1, pw2 = form.get("password", ""), form.get("password2", "")
                if len(pw1) < 6:
                    self._redirect("/system?err=Пароль+короче+6+символов")
                elif pw1 != pw2:
                    self._redirect("/system?err=Пароли+не+совпадают")
                else:
                    self.ctx.settings.set_credentials(user, pw1)
                    self._redirect("/system?msg=Логин+и+пароль+обновлены")
            elif path == "/system/restart-agent":
                self._redirect("/system?msg=Агент+перезапускается")
                threading.Thread(
                    target=lambda: (time.sleep(0.5),
                                    self.ctx.actions["restart_agent"]()),
                    daemon=True).start()
            elif path == "/system/reboot":
                ok, out = self.ctx.actions["reboot"]()
                self._redirect("/system?" + urllib.parse.urlencode(
                    {"msg": "Устройство перезагружается."} if ok
                    else {"err": out}))
            elif path == "/system/timezone":
                ok, out = self.ctx.backend.set_timezone(
                    form.get("timezone", "").strip())
                self._redirect("/system?" + urllib.parse.urlencode(
                    {"msg": "Часовой пояс изменён."} if ok
                    else {"err": "Не удалось изменить часовой пояс. " + out}))
            elif path == "/server/bind":
                ok, out = self.ctx.bind(form.get("server", "").strip(),
                                        form.get("code", "").strip())
                self._redirect("/server?" + urllib.parse.urlencode(
                    {"msg": out} if ok else {"err": out}))
            elif path == "/storage/delete":
                delete_cached(self.ctx.state, form.get("sha256", ""))
                self._redirect("/storage?msg=Файл+удалён+из+кеша")
            elif path == "/storage/clear":
                n = clear_cache(self.ctx.state)
                self._redirect("/storage?" + urllib.parse.urlencode(
                    {"msg": f"Кеш очищен, удалено файлов: {n}"}))
            else:
                self._send(_page("Не найдено", "<p>Нет такого действия.</p>"), 404)
        except Exception as e:  # noqa: BLE001 — панель не должна падать
            log.error("Ошибка веб-панели: %s", e)
            self._redirect("/system?err=" + urllib.parse.quote(str(e)))

    # --- страницы ---
    def _page_overview(self, flash, cls):
        s = self.ctx.get_status()
        net = self.ctx.backend.network_status()
        rows = "".join(
            f'<div class="kv"><span class="muted">{html.escape(k)}</span>'
            f'<span>{html.escape(str(v))}</span></div>'
            for k, v in s.items())
        return _page("Обзор устройства",
                     f'<div class="card">{rows}</div>'
                     f'<div class="card"><h2 style="margin-top:0">Сеть</h2>'
                     f'<div class="kv"><span class="muted">IP-адрес</span>'
                     f'<span>{html.escape(net["ip"])}</span></div>'
                     f'<div class="kv"><span class="muted">Имя устройства</span>'
                     f'<span>{html.escape(net["hostname"])}</span></div></div>',
                     flash, cls)

    def _page_server(self, flash, cls):
        auth = self.ctx.auth_info()
        if auth:
            body = (
                '<div class="card">'
                f'<div class="kv"><span class="muted">Сервер</span>'
                f'<span>{html.escape(auth.get("server", "—"))}</span></div>'
                f'<div class="kv"><span class="muted">ID устройства</span>'
                f'<span>{html.escape(str(auth.get("device_id", "—")))}</span></div>'
                '<p class="muted">Устройство привязано к серверу. Афиши и '
                'команды приходят автоматически.</p></div>'
                '<div class="card"><h2 style="margin-top:0">Сменить сервер</h2>'
                '<form method=post action="/server/bind">'
                '<label>Адрес сервера</label>'
                '<input name=server placeholder="https://signage.example.com">'
                '<label>Код подключения (новый экран на сервере)</label>'
                '<input name=code placeholder="AB12-CD34">'
                '<button type=submit>Перепривязать</button></form></div>')
        else:
            body = (
                '<div class="card"><p>Устройство ещё не привязано к серверу. '
                'Создайте экран в панели сервера, получите код подключения и '
                'введите его здесь.</p>'
                '<form method=post action="/server/bind">'
                '<label>Адрес сервера</label>'
                '<input name=server placeholder="https://signage.example.com" '
                'autofocus>'
                '<label>Код подключения</label>'
                '<input name=code placeholder="AB12-CD34">'
                '<button type=submit>Привязать к серверу</button>'
                '</form></div>')
        return _page("Сервер", body, flash, cls)

    def _page_storage(self, flash, cls):
        rep = cache_report(self.ctx.state)
        pct = int(100 * (rep["total_mb"] - rep["free_mb"]) /
                  rep["total_mb"]) if rep["total_mb"] else 0
        rows = "".join(
            f'<tr><td>{html.escape(f["name"])}'
            f'{" " if f["in_use"] else " <span class=muted>(не в плейлисте)</span>"}'
            f'</td><td>{f["size_mb"]} МБ</td>'
            f'<td><form method=post action="/storage/delete" '
            f'style="margin:0"><input type=hidden name=sha256 '
            f'value="{html.escape(f["sha256"])}">'
            f'<button class=sec type=submit>Удалить</button></form></td></tr>'
            for f in rep["files"]) or \
            '<tr><td colspan=3 class=muted>Кеш пуст</td></tr>'
        return _page(
            "Хранилище",
            f'<div class="card">'
            f'<div class="kv"><span class="muted">Всего на диске</span>'
            f'<span>{rep["total_mb"]} МБ</span></div>'
            f'<div class="kv"><span class="muted">Свободно</span>'
            f'<span>{rep["free_mb"]} МБ</span></div>'
            f'<div class="kv"><span class="muted">Занято кешем афиш</span>'
            f'<span>{rep["used_mb"]} МБ ({len(rep["files"])} файлов)</span></div>'
            f'<div class="kv"><span class="muted">Заполнение диска</span>'
            f'<span>{pct}%</span></div></div>'
            f'<div class="card"><h2 style="margin-top:0">Кешированный контент</h2>'
            f'<table><tr><th>Афиша</th><th>Размер</th><th></th></tr>{rows}</table>'
            f'<form method=post action="/storage/clear" '
            f'onsubmit="return confirm(\'Очистить весь кеш? Действующие афиши '
            f'докачаются заново.\')">'
            f'<button class=danger type=submit>Очистить весь кеш</button></form>'
            f'<p class="muted" style="margin-bottom:0">Удаление действующей '
            f'афиши временно освободит место — она докачается при следующем '
            f'обновлении плейлиста.</p></div>',
            flash, cls)

    def _page_network(self, flash, cls):
        net = self.ctx.backend.network_status()
        conn_rows = "".join(
            f"<tr><td>{html.escape(c['device'])}</td>"
            f"<td>{'Wi-Fi' if c['type']=='wifi' else 'Провод'}</td>"
            f"<td>{html.escape(c['state'])}</td>"
            f"<td>{html.escape(c['name'] or '—')}</td></tr>"
            for c in net["connections"]) or \
            "<tr><td colspan=4 class=muted>Интерфейсы не найдены</td></tr>"
        nets = self.ctx.backend.wifi_scan()
        options = "".join(
            f'<option value="{html.escape(n["ssid"])}">{html.escape(n["ssid"])}'
            f' ({html.escape(n["signal"])}%){" 🔒" if n["secure"] else ""}</option>'
            for n in nets)
        wifi_form = (
            '<div class="card"><h2 style="margin-top:0">Подключиться к Wi-Fi</h2>'
            '<form method=post action="/network/wifi">'
            f'<label>Сеть</label><select name=ssid>{options}</select>'
            '<label>Пароль</label><input type=password name=password '
            'autocomplete=off>'
            '<button type=submit>Подключиться</button></form></div>'
        ) if nets else '<p class="muted">Wi-Fi-адаптер не обнаружен или сети не найдены.</p>'
        return _page(
            "Сеть",
            f'<div class="card"><div class="kv"><span class="muted">Текущий IP</span>'
            f'<span>{html.escape(net["ip"])}</span></div>'
            f'<table><tr><th>Интерфейс</th><th>Тип</th><th>Состояние</th>'
            f'<th>Соединение</th></tr>{conn_rows}</table></div>{wifi_form}',
            flash, cls)

    def _page_audio(self, flash, cls):
        outs = self.ctx.backend.audio_outputs()
        current = self.ctx.settings.data.get("audio_output", "")
        if outs:
            options = "".join(
                f'<option value="{html.escape(o["id"])}"'
                f'{" selected" if o["id"]==current else ""}>'
                f'{html.escape(o["label"])}</option>' for o in outs)
            body = (
                '<div class="card"><form method=post action="/audio">'
                '<label>Аудиовыход</label>'
                f'<select name=audio_output>{options}</select>'
                '<button type=submit>Сохранить</button></form>'
                '<p class="muted" style="margin-bottom:0">Выберите HDMI или '
                'аналоговый выход 3.5&nbsp;мм. Звук идёт через ALSA напрямую '
                '(без PipeWire); изменение применится в течение одной ротации '
                'афиш.</p></div>')
        else:
            body = ('<div class="card muted">Аудиокарты не обнаружены '
                    '(/proc/asound/cards пуст). Проверьте, что в системе есть '
                    'звук и пользователь агента состоит в группе <b>audio</b>.'
                    '</div>')
        return _page("Звук", body, flash, cls)

    def _page_system(self, flash, cls):
        net = self.ctx.backend.network_status()
        cur_tz = self.ctx.backend.get_timezone()
        tzs = self.ctx.backend.list_timezones()
        if tzs:
            tz_opts = "".join(
                f'<option{" selected" if z == cur_tz else ""}>{html.escape(z)}</option>'
                for z in tzs)
            tz_input = f'<select name=timezone>{tz_opts}</select>'
        else:
            tz_input = (f'<input name=timezone value="{html.escape(cur_tz)}" '
                        f'placeholder="Europe/Moscow">')
        return _page(
            "Система",
            '<div class="card"><h2 style="margin-top:0">Часовой пояс</h2>'
            f'<p class="muted" style="margin-top:0">Текущий: '
            f'{html.escape(cur_tz)}</p>'
            '<form method=post action="/system/timezone">'
            f'{tz_input}<button type=submit>Сохранить</button></form></div>'
            '<div class="card"><h2 style="margin-top:0">Имя устройства</h2>'
            '<form method=post action="/system/hostname">'
            f'<input name=hostname value="{html.escape(net["hostname"])}">'
            '<button type=submit>Сохранить</button></form></div>'
            '<div class="card"><h2 style="margin-top:0">Доступ к панели</h2>'
            '<form method=post action="/system/password" autocomplete=off>'
            f'<label>Логин</label><input name=user '
            f'value="{html.escape(self.ctx.settings.data.get("web_user","admin"))}">'
            '<label>Новый пароль</label>'
            '<input type=password name=password autocomplete=new-password>'
            '<label>Повторите пароль</label>'
            '<input type=password name=password2 autocomplete=new-password>'
            '<button type=submit>Изменить</button></form></div>'
            '<div class="card"><h2 style="margin-top:0">Питание</h2>'
            '<form method=post action="/system/restart-agent" '
            'style="display:inline">'
            '<button type=submit class=sec>Перезапустить агент</button></form> '
            '<form method=post action="/system/reboot" style="display:inline" '
            'onsubmit="return confirm(\'Перезагрузить устройство?\')">'
            '<button type=submit class=danger>Перезагрузить устройство</button>'
            '</form></div>',
            flash, cls)


class LocalWebServer:
    def __init__(self, ctx: WebContext, port: int):
        self.ctx = ctx
        self.port = port
        self.httpd: http.server.ThreadingHTTPServer | None = None

    def start(self) -> None:
        try:
            self.httpd = http.server.ThreadingHTTPServer(
                ("0.0.0.0", self.port), _WebHandler)
        except OSError as e:
            log.error("Не удалось запустить веб-панель на порту %d: %s",
                      self.port, e)
            return
        self.httpd.ctx = self.ctx  # type: ignore[attr-defined]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        log.info("Веб-панель устройства: http://%s:%d (логин %s)",
                 local_ip_address(), self.port,
                 self.ctx.settings.data.get("web_user", "admin"))

    def stop(self) -> None:
        if self.httpd is not None:
            self.httpd.shutdown()


# ---------------------------------------------------------------- запуск

def load_auth(state: State) -> dict | None:
    if state.auth_path.exists():
        try:
            return json.loads(state.auth_path.read_text())
        except (json.JSONDecodeError, OSError):
            return None
    return None


def cmd_register(args, state: State) -> int:
    client = ServerClient(args.server, token=None)
    try:
        result = client.register(args.code)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            print("Ошибка: неверный или уже использованный код подключения.",
                  file=sys.stderr)
        else:
            print(f"Ошибка сервера: HTTP {e.code}", file=sys.stderr)
        return 1
    except urllib.error.URLError as e:
        print(f"Сервер недоступен: {e.reason}", file=sys.stderr)
        return 1
    state.auth_path.parent.mkdir(parents=True, exist_ok=True)
    state.auth_path.write_text(json.dumps({
        "server": args.server.rstrip("/"),
        "token": result["token"],
        "device_id": result["device_id"],
    }))
    state.auth_path.chmod(0o600)
    print(f"Устройство зарегистрировано: «{result['name']}» "
          f"(id={result['device_id']}).")
    return 0


def cmd_run(args, state: State) -> int:
    state.cache_dir.mkdir(parents=True, exist_ok=True)
    state.load_saved_manifest()

    settings = Settings(state.settings_path)
    audio_device = settings.data.get("audio_output", "")
    player = (MockPlayer() if args.dev
              else MpvPlayer(args.mpv_socket, args.mpv_arg or [],
                             audio_device=audio_device))
    placeholder = Path(args.placeholder) if args.placeholder else None
    stop = threading.Event()

    def _terminate(_sig, _frm):
        log.info("Получен сигнал остановки")
        stop.set()
        state.wake.set()

    signal.signal(signal.SIGTERM, _terminate)
    signal.signal(signal.SIGINT, _terminate)

    # Клиентские циклы (опрос сервера) запускаются один раз при наличии
    # регистрации — либо сразу, либо после привязки через веб-панель.
    auth_box: dict = {"auth": load_auth(state)}
    loops_started = threading.Event()

    def start_client_loops(auth: dict) -> None:
        if loops_started.is_set():
            return
        loops_started.set()
        client = ServerClient(auth["server"], auth["token"])
        for target, targs in (
            (sync_loop, (client, state, args.poll_interval, stop,
                         args.self_update and not args.dev)),
            (heartbeat_loop, (client, state, stop)),
            (command_loop, (client, player, args.allow_system, stop)),
        ):
            threading.Thread(target=target, args=targs, daemon=True).start()
        log.info("Подключение к серверу %s активно", auth["server"])

    def bind_action(server: str, code: str):
        server = server.rstrip("/")
        if not server or not code:
            return False, "Укажите адрес сервера и код подключения."
        try:
            result = ServerClient(server, None).register(code)
        except urllib.error.HTTPError as e:
            return False, ("Неверный или использованный код."
                           if e.code == 404 else f"Ошибка сервера: HTTP {e.code}")
        except urllib.error.URLError as e:
            return False, f"Сервер недоступен: {e.reason}"
        auth = {"server": server, "token": result["token"],
                "device_id": result["device_id"]}
        state.auth_path.parent.mkdir(parents=True, exist_ok=True)
        state.auth_path.write_text(json.dumps(auth))
        try:
            state.auth_path.chmod(0o600)
        except OSError:
            pass
        auth_box["auth"] = auth
        start_client_loops(auth)
        return True, f"Привязано к серверу как «{result['name']}»."

    # Локальная веб-панель устройства (работает и до привязки к серверу)
    web = None
    if args.web_port and not args.no_web:
        state.web_port = args.web_port
        backend = make_backend(args.dev)

        def _status_summary() -> dict:
            a = auth_box["auth"]
            return {
                "Версия агента": AGENT_VERSION,
                "Сервер": a["server"] if a else "не привязан",
                "ID устройства": a.get("device_id", "?") if a else "—",
                "Аптайм": f"{read_uptime_sec(state) // 3600} ч",
                "Температура CPU": (f"{read_temp_c():.0f} °C"
                                    if read_temp_c() is not None else "—"),
                "Свободно на диске": f"{disk_free_mb(state.cache_dir) or 0} МБ",
                "Афиш в кеше": f"{state.cache_done}/{state.cache_total}",
            }

        def _reboot_action():
            if not args.allow_system:
                return False, "Перезагрузка отключена (--allow-system не задан)."
            threading.Thread(
                target=lambda: (time.sleep(0.5),
                                subprocess.run(["sudo", "-n", "reboot"])),
                daemon=True).start()
            return True, ""

        ctx = WebContext(
            settings, backend, state, _status_summary,
            {"restart_agent": lambda: os._exit(0), "reboot": _reboot_action,
             "set_audio": player.set_audio_device},
            auth_info=lambda: auth_box["auth"],
            bind=bind_action,
        )
        web = LocalWebServer(ctx, args.web_port)
        web.start()

    if auth_box["auth"]:
        start_client_loops(auth_box["auth"])
    else:
        log.warning("Устройство не привязано к серверу. Откройте веб-панель "
                    "(порт %d) → «Сервер» и введите адрес и код подключения.",
                    args.web_port)

    log.info("Агент %s запущен (кеш: %s)", AGENT_VERSION, state.cache_dir)
    playback_loop(player, state, placeholder, stop)
    if web is not None:
        web.stop()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Агент RPi Signage")
    parser.add_argument("--state-dir", default=os.environ.get(
        "SIGNAGE_STATE_DIR", DEFAULT_STATE_DIR))
    parser.add_argument("--verbose", "-v", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_reg = sub.add_parser("register", help="привязать устройство к серверу")
    p_reg.add_argument("--server", required=True)
    p_reg.add_argument("--code", required=True)

    p_run = sub.add_parser("run", help="основной цикл воспроизведения")
    p_run.add_argument("--dev", action="store_true",
                       help="dev-режим: mock-плеер вместо mpv")
    p_run.add_argument("--poll-interval", type=int, default=60)
    p_run.add_argument("--mpv-socket", default=DEFAULT_MPV_SOCKET)
    p_run.add_argument("--mpv-arg", action="append",
                       help="дополнительный аргумент mpv (можно несколько)")
    p_run.add_argument("--placeholder",
                       default=os.environ.get("SIGNAGE_PLACEHOLDER", ""),
                       help="изображение-заставка, когда контента нет")
    p_run.add_argument("--self-update", action="store_true",
                       help="обновлять agent.py с сервера при смене версии")
    p_run.add_argument("--allow-system", action="store_true",
                       help="разрешить команду перезагрузки RPi (нужен sudo)")
    p_run.add_argument("--web-port", type=int, default=DEFAULT_WEB_PORT,
                       help="порт локальной веб-панели устройства")
    p_run.add_argument("--no-web", action="store_true",
                       help="не запускать локальную веб-панель")

    p_pw = sub.add_parser("set-password",
                          help="задать логин/пароль локальной веб-панели")
    p_pw.add_argument("--user", default="admin")
    p_pw.add_argument("--password", required=True)

    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    state = State(Path(args.state_dir))
    if args.cmd == "register":
        return cmd_register(args, state)
    if args.cmd == "set-password":
        settings = Settings(state.settings_path)
        settings.set_credentials(args.user, args.password)
        print(f"Логин панели: {args.user} (пароль обновлён).")
        return 0
    return cmd_run(args, state)


if __name__ == "__main__":
    sys.exit(main())
