"""The worker side of the loopback NDJSON transport, for the fake worker. The same rules as Dojo::Transport in C++.

One controller at a time: a second connection gets a ``busy`` error line and is closed. Reader threads only frame
lines and decode them into pure data (protocol.decode_request); the worker thread takes events with poll() and
answers with send(). Nothing is ever dropped to relieve pressure: when the worker has not taken the last
``inbox_capacity`` lines, or ``outbox_limit`` responses wait unwritten, the reader stops reading and TCP pushes back
on the controller. A line over the bound is answered with ``line_too_long`` and the connection is closed, since its
end cannot be trusted. Each connection has a serial; a response for an old serial is dropped, because its session
has ended.
"""

from __future__ import annotations

import select
import socket
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable

from .protocol import Bounds, Decoded, ErrorCode, decode_request, encode, error, is_blank_line

LOOPBACK = ("127.0.0.1", "::1", "localhost")


@dataclass(frozen=True)
class Event:
    kind: str                       # "connected", "line" or "disconnected"
    connection: int
    decoded: Decoded | None = None


@dataclass
class Stats:
    lines_in: int = 0
    lines_out: int = 0
    refused: int = 0
    dropped_sends: int = 0
    max_inbox_lines: int = 0
    max_outbox: int = 0
    read_pauses: int = 0


@dataclass
class _Connection:
    serial: int
    sock: socket.socket
    outbox: deque[bytes] = field(default_factory=deque)
    closing: bool = False
    dead: bool = False
    gone: bool = False              # disconnected was reported
    closing_since: float = 0.0


def _busy_line(message: str) -> bytes:
    return (encode(error(ErrorCode.BUSY, None, message)) + "\n").encode("utf-8")


class LoopbackServer:
    def __init__(self, bounds: Bounds | None = None, *, inbox_capacity: int = 16, outbox_limit: int = 64,
                 linger_s: float = 1.0, control_backlog: int = 8, log: Callable[[str], None] | None = None):
        self.bounds = bounds or Bounds()
        self.inbox_capacity = max(inbox_capacity, 1)
        self.outbox_limit = max(outbox_limit, 1)
        self.linger_s = linger_s
        self.control_backlog = control_backlog
        self.stats = Stats()
        self._log = log or (lambda message: None)
        self._lock = threading.Condition()
        self._events: deque[Event] = deque()
        self._lines_queued = 0
        self._controls_queued = 0
        self._active: _Connection | None = None
        self._serial = 0
        self._listener: socket.socket | None = None
        self._stopping = False
        self._threads: list[threading.Thread] = []
        self.host = ""
        self.port = 0

    # --- lifecycle ---

    def start(self, host: str = "127.0.0.1", port: int = 0) -> int:
        if host not in LOOPBACK:
            raise ValueError(f"{host} is not a loopback address")
        address = "127.0.0.1" if host == "localhost" else host
        family = socket.AF_INET6 if ":" in address else socket.AF_INET
        listener = socket.socket(family, socket.SOCK_STREAM)
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        listener.bind((address, port))
        listener.listen(4)
        listener.settimeout(0.1)
        self._listener = listener
        self.host = address
        self.port = listener.getsockname()[1]
        self._spawn(self._accept_loop, "dojo-accept")
        return self.port

    def stop(self) -> None:
        with self._lock:
            self._stopping = True
            conn = self._active
            self._lock.notify_all()
        if conn is not None:
            self._kill(conn)
        if self._listener is not None:
            self._listener.close()
        for thread in self._threads:
            thread.join(timeout=5)
        self._threads.clear()

    @property
    def connection(self) -> int:
        with self._lock:
            return self._active.serial if self._active and not self._active.gone else 0

    # --- the worker thread's side ---

    def poll(self, timeout: float | None = None) -> Event | None:
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._lock:
            while not self._events:
                left = None if deadline is None else deadline - time.monotonic()
                if self._stopping or (left is not None and left <= 0):
                    return None
                self._lock.wait(left)
            event = self._events.popleft()
            if event.kind == "line":
                self._lines_queued -= 1
            else:
                self._controls_queued -= 1
            self._lock.notify_all()
            return event

    def send(self, connection: int, message: dict[str, Any] | str) -> bool:
        line = (message if isinstance(message, str) else encode(message)) + "\n"
        with self._lock:
            conn = self._active
            if conn is None or conn.serial != connection or conn.closing or conn.dead:
                self.stats.dropped_sends += 1
                return False
            conn.outbox.append(line.encode("utf-8"))
            self.stats.max_outbox = max(self.stats.max_outbox, len(conn.outbox))
            self._lock.notify_all()
            return True

    def close(self, connection: int) -> None:
        """Closes the connection once the responses already queued for it are written."""
        with self._lock:
            conn = self._active
            if conn is not None and conn.serial == connection and not conn.closing:
                conn.closing = True
                conn.closing_since = time.monotonic()
                self._lock.notify_all()

    # --- threads ---

    def _spawn(self, target: Callable[..., None], name: str, *args: Any) -> None:
        thread = threading.Thread(target=target, args=args, name=name, daemon=True)
        self._threads = [t for t in self._threads if t.is_alive()]   # finished connections are forgotten
        self._threads.append(thread)
        thread.start()

    def _accept_loop(self) -> None:
        while True:
            with self._lock:
                if self._stopping:
                    return
            try:
                sock, _ = self._listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            with self._lock:
                refuse = self._active is not None or self._controls_queued >= self.control_backlog
                if not refuse:
                    self._serial += 1
                    conn = _Connection(self._serial, sock)
                    self._active = conn
                    self._push(Event("connected", conn.serial))
                else:
                    self.stats.refused += 1
            if refuse:
                self._log("refused a second controller")
                self._spawn(self._refuse, "dojo-refuse", sock)
                continue
            self._spawn(self._read_loop, f"dojo-read-{conn.serial}", conn)
            self._spawn(self._write_loop, f"dojo-write-{conn.serial}", conn)

    def _refuse(self, sock: socket.socket) -> None:
        """Answers busy and drains until EOF, for at most linger_s in all however slowly the peer trickles."""
        deadline = time.monotonic() + self.linger_s
        try:
            sock.settimeout(self.linger_s)
            sock.sendall(_busy_line("another controller is connected"))
            sock.shutdown(socket.SHUT_WR)
            while (left := deadline - time.monotonic()) > 0:
                sock.settimeout(left)
                if not sock.recv(4096):
                    break
        except OSError:
            pass
        finally:
            sock.close()

    def _read_loop(self, conn: _Connection) -> None:
        limit = self.bounds.max_line_bytes
        buffer = bytearray()
        conn.sock.settimeout(None)   # blocking: a socket timeout would also cut the writer's sendall short
        try:
            while True:
                with self._lock:
                    while not (self._stopping or conn.dead or conn.closing) and (
                            len(conn.outbox) >= self.outbox_limit):
                        self.stats.read_pauses += 1
                        self._lock.wait(0.1)
                    if self._stopping or conn.dead:
                        return
                    if conn.closing and time.monotonic() - conn.closing_since > self.linger_s:
                        return
                try:
                    readable, _, _ = select.select([conn.sock], [], [], 0.05)
                    if not readable:
                        continue
                    chunk = conn.sock.recv(65536)
                except (OSError, ValueError):
                    return
                if not chunk:
                    return
                if conn.closing:
                    continue   # draining after a final line, so the peer reads it instead of a reset
                buffer += chunk
                while True:
                    end = buffer.find(b"\n")
                    if end < 0 and len(buffer) <= limit:
                        break
                    if end < 0 or end > limit:
                        self._too_long(conn)
                        buffer.clear()
                        break
                    line = bytes(buffer[:end])
                    del buffer[:end + 1]
                    if line.endswith(b"\r"):
                        line = line[:-1]
                    if is_blank_line(line):
                        continue
                    if not self._enqueue(conn, decode_request(line, self.bounds)):
                        return
        finally:
            self._kill(conn)

    def _too_long(self, conn: _Connection) -> None:
        self._log(f"controller sent a line over {self.bounds.max_line_bytes} bytes, closing")
        with self._lock:
            conn.outbox.append((encode(error(ErrorCode.LINE_TOO_LONG, None,
                                             f"line over {self.bounds.max_line_bytes} bytes")) + "\n").encode())
            conn.closing = True
            conn.closing_since = time.monotonic()
            self._lock.notify_all()

    def _enqueue(self, conn: _Connection, decoded: Decoded) -> bool:
        """Waits for room in the inbox, and for the outbox to drain, before each line, as the C++ transport does: a
        buffered burst cannot pile up answers the controller is not reading."""
        with self._lock:
            while self._lines_queued >= self.inbox_capacity or len(conn.outbox) >= self.outbox_limit:
                if self._stopping or conn.dead:
                    return False
                self.stats.read_pauses += 1
                self._lock.wait(0.1)
            self._lines_queued += 1
            self.stats.lines_in += 1
            self.stats.max_inbox_lines = max(self.stats.max_inbox_lines, self._lines_queued)
            self._events.append(Event("line", conn.serial, decoded))
            self._lock.notify_all()
            return True

    def _write_loop(self, conn: _Connection) -> None:
        try:
            while True:
                with self._lock:
                    while not conn.outbox and not conn.closing and not conn.dead and not self._stopping:
                        self._lock.wait(0.1)
                    if conn.dead or self._stopping:
                        return
                    if not conn.outbox:
                        conn.sock.shutdown(socket.SHUT_WR)   # closing, everything written
                        return
                    line = conn.outbox[0]
                conn.sock.sendall(line)
                with self._lock:
                    conn.outbox.popleft()
                    self.stats.lines_out += 1
                    self._lock.notify_all()
        except OSError:
            self._kill(conn)

    def _kill(self, conn: _Connection) -> None:
        with self._lock:
            if conn.gone:
                return
            conn.gone = True
            conn.dead = True
            if self._active is conn:
                self._active = None
            self._push(Event("disconnected", conn.serial))
        try:
            conn.sock.shutdown(socket.SHUT_RDWR)   # wakes a writer blocked in sendall
        except OSError:
            pass
        try:
            conn.sock.close()
        except OSError:
            pass

    def _push(self, event: Event) -> None:
        """Callers hold the lock."""
        self._controls_queued += 1
        self._events.append(event)
        self._lock.notify_all()
