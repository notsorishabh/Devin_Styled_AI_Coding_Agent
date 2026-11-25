"""In-memory persistent shell sessions (Step 3).

Not stored in Postgres: shells die with the process and with sandbox destroy.
A ring buffer of recent chunks supports SSE reconnect via Last-Event-ID.
"""

from __future__ import annotations

import asyncio
import errno
import logging
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.config import settings
from app.sandbox.provider import ShellNotFound

logger = logging.getLogger(__name__)

FLUSH_SECONDS = 0.05
RECV_SIZE = 4096


def _raw_socket(sock: Any) -> Any:
    current = sock
    for _ in range(4):
        nxt = getattr(current, "_sock", None)
        if nxt is None or nxt is current:
            break
        current = nxt
    return current


@dataclass
class ShellSession:
    id: str
    sandbox_id: str
    sock: Any
    status: str = "open"
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    closed_at: datetime | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)
    buffer: deque[tuple[int, str]] = field(default_factory=deque)
    next_event_id: int = 0
    listeners: list[tuple[asyncio.AbstractEventLoop, asyncio.Queue]] = field(
        default_factory=list
    )
    stop: threading.Event = field(default_factory=threading.Event)
    reader: threading.Thread | None = None


class ShellRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[str, ShellSession] = {}

    def get(self, shell_session_id: str) -> ShellSession:
        with self._lock:
            session = self._sessions.get(shell_session_id)
        if session is None or session.status != "open":
            raise ShellNotFound(shell_session_id)
        return session

    def latest_open(self, sandbox_id: str) -> ShellSession:
        with self._lock:
            matches = [
                s
                for s in self._sessions.values()
                if s.sandbox_id == sandbox_id and s.status == "open"
            ]
        if not matches:
            raise ShellNotFound(sandbox_id)
        matches.sort(key=lambda s: s.created_at)
        return matches[-1]

    def add(self, session: ShellSession) -> None:
        with self._lock:
            self._sessions[session.id] = session

    def close(self, shell_session_id: str) -> None:
        with self._lock:
            session = self._sessions.get(shell_session_id)
        if session is None:
            return
        self._shutdown(session)

    def close_for_sandbox(self, sandbox_id: str) -> None:
        with self._lock:
            ids = [
                s.id for s in self._sessions.values() if s.sandbox_id == sandbox_id
            ]
        for sid in ids:
            self.close(sid)

    def _shutdown(self, session: ShellSession) -> None:
        with session.lock:
            if session.status == "closed":
                return
            session.status = "closed"
            session.closed_at = datetime.now(UTC)
            session.stop.set()
            listeners = list(session.listeners)
        raw = _raw_socket(session.sock)
        try:
            raw.shutdown(2)
        except Exception:
            pass
        try:
            raw.close()
        except Exception:
            pass
        try:
            session.sock.close()
        except Exception:
            pass
        if session.reader is not None and session.reader is not threading.current_thread():
            session.reader.join(timeout=2)
        for loop, queue in listeners:
            loop.call_soon_threadsafe(queue.put_nowait, None)
        logger.info("shell closed id=%s sandbox=%s", session.id, session.sandbox_id)


def start_reader(session: ShellSession, registry: ShellRegistry) -> None:
    session.buffer = deque(maxlen=settings.shell_buffer_max_events)
    thread = threading.Thread(
        target=_read_loop,
        args=(session, registry),
        name=f"shell-reader-{session.id[:8]}",
        daemon=True,
    )
    session.reader = thread
    thread.start()


def emit(session: ShellSession, text: str) -> None:
    if not text:
        return
    with session.lock:
        if session.status != "open":
            return
        session.next_event_id += 1
        event_id = session.next_event_id
        session.buffer.append((event_id, text))
        listeners = list(session.listeners)
    for loop, queue in listeners:
        try:
            loop.call_soon_threadsafe(queue.put_nowait, (event_id, text))
        except RuntimeError:
            pass


def _read_loop(session: ShellSession, registry: ShellRegistry) -> None:
    raw = _raw_socket(session.sock)
    pending = b""
    last_data = time.monotonic()
    try:
        if hasattr(raw, "settimeout"):
            raw.settimeout(FLUSH_SECONDS)
        while not session.stop.is_set():
            try:
                if hasattr(raw, "recv"):
                    chunk = raw.recv(RECV_SIZE)
                elif hasattr(raw, "read"):
                    chunk = raw.read(RECV_SIZE)
                else:
                    break
            except TimeoutError:
                chunk = None
            except OSError as exc:
                winerr = getattr(exc, "winerror", None)
                if exc.errno in {
                    errno.ETIMEDOUT,
                    errno.EAGAIN,
                    errno.EWOULDBLOCK,
                    errno.EINTR,
                } or winerr in {10035, 10060}:
                    chunk = None
                else:
                    break
            except Exception:
                logger.exception("shell recv failed id=%s", session.id)
                break
            now = time.monotonic()
            if chunk:
                pending += chunk
                last_data = now
                pending = _flush_lines(session, pending)
            elif chunk == b"":
                break
            elif pending and now - last_data >= FLUSH_SECONDS:
                emit(session, pending.decode("utf-8", errors="replace"))
                pending = b""
            elif chunk is None and pending and now - last_data >= FLUSH_SECONDS:
                emit(session, pending.decode("utf-8", errors="replace"))
                pending = b""
        if pending:
            emit(session, pending.decode("utf-8", errors="replace"))
    finally:
        registry._shutdown(session)


def _flush_lines(session: ShellSession, pending: bytes) -> bytes:
    while b"\n" in pending:
        line, pending = pending.split(b"\n", 1)
        emit(session, line.decode("utf-8", errors="replace") + "\n")
    return pending


def open_session(sandbox_id: str, sock: Any, registry: ShellRegistry) -> ShellSession:
    session = ShellSession(
        id=str(uuid.uuid4()),
        sandbox_id=sandbox_id,
        sock=sock,
    )
    registry.add(session)
    start_reader(session, registry)
    logger.info("shell opened id=%s sandbox=%s", session.id, sandbox_id)
    return session
