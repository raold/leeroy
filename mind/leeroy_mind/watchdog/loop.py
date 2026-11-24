"""A C-level one-shot timer reset by the loop, so a stalled loop cannot hide itself.

faulthandler's timer is process-global. Only this service entry point owns it;
unit tests inject a fake backend, and the real timer is rehearsed in subprocesses.
The descriptor stays open until cancel_dump_traceback_later has joined its timer.
"""
import asyncio
import faulthandler
import logging
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .config import load

log = logging.getLogger("leeroy_mind.watchdog")
TIMEOUT_S = 2.0
HEARTBEAT_S = 0.25
COOLDOWN_S = 60.0
MAX_BYTES = 2 * 1024 * 1024
BACKUPS = 3
_owner = None


class LoopWatchdog:
    def __init__(self, log_dir: Path, *, backend=faulthandler, now=time.monotonic,
                 timeout=TIMEOUT_S, heartbeat=HEARTBEAT_S, cooldown=COOLDOWN_S):
        self.path = Path(log_dir) / "mind-loop-stalls.log"
        self.backend, self.now = backend, now
        self.timeout, self.heartbeat, self.cooldown = timeout, heartbeat, cooldown
        self.stream = None
        self.handle = None
        self.deadline = None
        self.not_before = 0.0
        self.closed = True

    def start(self):
        global _owner
        if _owner is not None or threading.current_thread() is not threading.main_thread():
            log.warning("loop_watchdog_unavailable reason=ownership")
            return self
        self.loop = asyncio.get_running_loop()
        _owner = self
        self.closed = False
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._open()
            self._pulse()
        except Exception:
            self._failed()
        return self

    def _open(self):
        # Only rotate our own bounded log, while no C timer can write its descriptor.
        if self.path.exists() and self.path.stat().st_size >= MAX_BYTES:
            for index in range(BACKUPS - 1, 0, -1):
                previous = self.path.with_name(self.path.name + f".{index}")
                if previous.exists():
                    previous.replace(self.path.with_name(self.path.name + f".{index + 1}"))
            self.path.replace(self.path.with_name(self.path.name + ".1"))
        self.stream = self.path.open("ab", buffering=0)
        self._note("opened", main_thread=hex(threading.get_ident()), timeout_s=self.timeout)

    def _note(self, event, **fields):
        stamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        details = " ".join(f"{key}={value}" for key, value in fields.items())
        self.stream.write(f"{stamp} loop_watchdog_{event} {details}\n".encode("ascii"))

    def _pulse(self):
        if self.closed:
            return
        try:
            now = self.now()
            if self.deadline is not None and now >= self.deadline:
                # A single timeout per stall; after recovery wait a minute before rearming.
                # Cancellation joins any in-progress dump before rotation or closing.
                self.backend.cancel_dump_traceback_later()
                delayed_ms = round((now - self.deadline + self.timeout) * 1000)
                self._note("recovered", delayed_ms=delayed_ms)
                log.warning("loop_watchdog_delayed delay_ms=%d", delayed_ms)
                self.deadline = None
                self.not_before = now + self.cooldown
                self.stream.close()
                self._open()
            if now >= self.not_before:
                self.backend.dump_traceback_later(self.timeout, repeat=False,
                                                  file=self.stream, exit=False)
                self.deadline = now + self.timeout
            self.handle = self.loop.call_later(self.heartbeat, self._pulse)
        except Exception:
            self._failed()

    def _failed(self):
        # A diagnostic failure must not stop chat, expose values or keep retrying.
        log.warning("loop_watchdog_disabled reason=diagnostic_io")
        self.close()

    def close(self):
        global _owner
        if self.closed:
            return
        self.closed = True
        if self.handle is not None:
            self.handle.cancel()
        if _owner is self:
            # Do not close/reuse the file descriptor if cancelling the timer fails.
            try:
                self.backend.cancel_dump_traceback_later()
            except Exception:
                log.warning("loop_watchdog_cancel_failed")
                return
            _owner = None
        if self.stream is not None:
            self.stream.close()


@contextmanager
def monitor(root: Path):
    if not load(Path(root) / "mind.toml").enabled:
        yield
        return
    guard = LoopWatchdog(Path(root) / "logs").start()
    try:
        yield
    finally:
        guard.close()
