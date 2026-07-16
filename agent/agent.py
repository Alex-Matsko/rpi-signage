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
import hashlib
import json
import logging
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

AGENT_VERSION = "0.1.0"

log = logging.getLogger("signage")

DEFAULT_STATE_DIR = "/var/lib/signage"
DEFAULT_MPV_SOCKET = "/tmp/signage-mpv.sock"
HEARTBEAT_SEC = 30
DOWNLOAD_CHUNK = 256 * 1024
MPV_ARGS = [
    "--idle=yes", "--fullscreen", "--no-terminal", "--no-osc", "--no-osd-bar",
    "--keep-open=no", "--loop-file=no", "--hwdec=auto-safe",
    "--really-quiet", "--no-input-default-bindings", "--osd-level=0",
]


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
        result = []
        with self.lock:
            for item in self.items:
                starts = item.get("starts_at")
                expires = item.get("expires_at")
                if starts and datetime.fromisoformat(starts) > t:
                    continue
                if expires and datetime.fromisoformat(expires) <= t:
                    continue
                result.append(item)
        return result


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

    def register(self, code: str) -> dict:
        return self._request("POST", "/api/agent/register", {"code": code})

    def manifest(self) -> dict:
        return self._request("GET", "/api/agent/manifest")

    def send_status(self, payload: dict) -> dict:
        return self._request("POST", "/api/agent/status", payload, timeout=15)

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

    def shutdown(self) -> None:
        pass


class MpvPlayer:
    """Управляет одним долгоживущим процессом mpv через JSON IPC.

    Элементы сменяются командой loadfile — без перезапуска процесса,
    поэтому между афишами нет чёрного экрана.
    """

    def __init__(self, socket_path: str, extra_args: list[str]):
        self.socket_path = socket_path
        self.extra_args = extra_args
        self.proc: subprocess.Popen | None = None
        self.sock: socket.socket | None = None
        self._req_id = 0

    def _ensure_running(self) -> None:
        if self.proc is not None and self.proc.poll() is None and self.sock:
            return
        self._close()
        Path(self.socket_path).unlink(missing_ok=True)
        cmd = ["mpv", f"--input-ipc-server={self.socket_path}",
               *MPV_ARGS, *self.extra_args]
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


# ---------------------------------------------------------------- циклы

def sync_loop(client: ServerClient, state: State, poll_interval: int,
              stop: threading.Event) -> None:
    """Опрос манифеста, докачка кеша, очистка сирот."""
    last_version = None
    while not stop.is_set():
        try:
            manifest = client.manifest()
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
    auth = load_auth(state)
    if auth is None:
        print("Агент не зарегистрирован. Сначала выполните:\n"
              f"  {sys.argv[0]} register --server URL --code КОД",
              file=sys.stderr)
        return 1

    state.cache_dir.mkdir(parents=True, exist_ok=True)
    state.load_saved_manifest()
    client = ServerClient(auth["server"], auth["token"])

    if args.dev:
        player = MockPlayer()
    else:
        player = MpvPlayer(args.mpv_socket, args.mpv_arg or [])

    placeholder = Path(args.placeholder) if args.placeholder else None
    stop = threading.Event()

    def _terminate(_sig, _frm):
        log.info("Получен сигнал остановки")
        stop.set()
        state.wake.set()

    signal.signal(signal.SIGTERM, _terminate)
    signal.signal(signal.SIGINT, _terminate)

    threads = [
        threading.Thread(target=sync_loop,
                         args=(client, state, args.poll_interval, stop),
                         daemon=True),
        threading.Thread(target=heartbeat_loop, args=(client, state, stop),
                         daemon=True),
    ]
    for t in threads:
        t.start()

    log.info("Агент %s запущен (сервер: %s, кеш: %s)",
             AGENT_VERSION, auth["server"], state.cache_dir)
    playback_loop(player, state, placeholder, stop)
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

    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    state = State(Path(args.state_dir))
    if args.cmd == "register":
        return cmd_register(args, state)
    return cmd_run(args, state)


if __name__ == "__main__":
    sys.exit(main())
