"""Брокер веб-терминала: мост между браузером (WebSocket) и агентом (HTTP).

RPi за NAT, поэтому агент сам подключается к серверу: держит длинный опрос за
вводом (браузер→агент) и POST-ит вывод (агент→браузер). Сессии живут в памяти
одного процесса uvicorn — этого достаточно для нашего развёртывания.
"""
import secrets
import threading
import time

SESSION_TTL = 3600
_WAIT_SLICE = 20.0  # макс. длительность одного длинного опроса, сек


class TermSession:
    def __init__(self, session_id: str, device_id: int):
        self.id = session_id
        self.device_id = device_id
        self.cond = threading.Condition()
        self.to_agent = bytearray()      # ввод от браузера к агенту
        self.to_browser = bytearray()    # вывод от агента к браузеру
        self.closed = False
        self.agent_attached = False
        self.created = time.monotonic()

    # --- сторона браузера ---
    def browser_send(self, data: bytes) -> None:
        with self.cond:
            self.to_agent += data
            self.cond.notify_all()

    def browser_wait_output(self, sent: int, timeout: float) -> tuple[bytes, bool]:
        """Ждёт новый вывод для браузера начиная с байта `sent`."""
        deadline = time.monotonic() + timeout
        with self.cond:
            while len(self.to_browser) <= sent and not self.closed:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self.cond.wait(timeout=remaining)
            return bytes(self.to_browser[sent:]), self.closed

    # --- сторона агента ---
    def agent_wait_input(self, consumed: int, timeout: float) -> tuple[bytes, bool]:
        deadline = time.monotonic() + timeout
        with self.cond:
            self.agent_attached = True
            while len(self.to_agent) <= consumed and not self.closed:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self.cond.wait(timeout=remaining)
            return bytes(self.to_agent[consumed:]), self.closed

    def agent_send(self, data: bytes) -> None:
        with self.cond:
            self.to_browser += data
            self.cond.notify_all()

    def close(self) -> None:
        with self.cond:
            self.closed = True
            self.cond.notify_all()


class TerminalBroker:
    def __init__(self):
        self._sessions: dict[str, TermSession] = {}
        self._lock = threading.Lock()

    def open(self, device_id: int) -> TermSession:
        self._gc()
        session = TermSession(secrets.token_hex(16), device_id)
        with self._lock:
            self._sessions[session.id] = session
        return session

    def get(self, session_id: str) -> TermSession | None:
        with self._lock:
            return self._sessions.get(session_id)

    def close(self, session_id: str) -> None:
        with self._lock:
            session = self._sessions.pop(session_id, None)
        if session is not None:
            session.close()

    def _gc(self) -> None:
        now = time.monotonic()
        with self._lock:
            stale = [
                sid for sid, s in self._sessions.items()
                if s.closed or now - s.created > SESSION_TTL
            ]
            for sid in stale:
                self._sessions.pop(sid, None)


broker = TerminalBroker()
