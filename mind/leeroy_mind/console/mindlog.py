"""mind.log as the console reads it: the newest records, followed as the file grows, and
shown without content.

The file is opened, read and closed at each poll, never held: on Windows it is opened with
FILE_SHARE_DELETE, so the mind's midnight rotation (a rename) is never blocked by the console.

A content-free record keeps its time, level and event name, its numbers and booleans, a few
fields whose values are identifiers (reason, error, loop, ...) and an exception's class name.
Any other text (messages, details, topics, paths, tracebacks) is dropped: the console never
shows what was said."""
import collections
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

SEED_BYTES = 512 * 1024  # how far back the first read goes
KEEP = 2000  # records kept in memory
IDENT_KEYS = frozenset({"reason", "error", "loop", "step", "stage", "channel", "hook", "phase",
                        "outcome", "status", "kind", "type", "factory", "role"})
_IDENT = re.compile(r"[A-Za-z][A-Za-z0-9_.:-]{0,47}\Z")
_EVENT = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_EXC = re.compile(r"^([A-Za-z_][\w.]*)(?::|$)")
LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

# Lifecycle markers: a run's start and clean end, and the records that mean it failed
STARTED = "mind_started"
CLEAN_END = "shutdown_complete"
FAILURES = frozenset({"mind_failed", "loop_stopped", "embedding_mismatch", "config_invalid"})
# A running mind that cannot reload an edited mind.toml keeps its config and says so as a
# WARNING: that is a failure only at ERROR (a start that never got going logs it so) or between
# a run's clean end and the next start
KEPT_RUNNING = frozenset({"config_invalid"})
BRIDGE = {"bridge_connected": True, "bridge_disconnected": False}


def _open_shared(path: Path):
    """A binary read handle that lets the writer rename or delete the file meanwhile."""
    if os.name != "nt":
        return open(path, "rb")
    import ctypes
    import msvcrt
    from ctypes import wintypes as w
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, w.LPVOID, w.DWORD, w.DWORD,
                                   w.HANDLE]
    kernel.CreateFileW.restype = w.HANDLE
    GENERIC_READ, SHARE_ALL, OPEN_EXISTING = 0x80000000, 0x7, 3
    handle = kernel.CreateFileW(str(path), GENERIC_READ, SHARE_ALL, None, OPEN_EXISTING, 0x80,
                                None)
    if handle in (None, w.HANDLE(-1).value):
        err = ctypes.get_last_error()
        if err in (2, 3):
            raise FileNotFoundError(str(path))
        raise OSError(f"cannot open the log (Windows error {err})")
    try:
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY)
    except OSError:
        kernel.CloseHandle(handle)
        raise
    return os.fdopen(fd, "rb")


def content_free(record: dict) -> dict:
    """{ts, level, event, fields, exc?}: nothing of what anyone said."""
    level = record.get("level")
    event = record.get("event")
    out = {"ts": record.get("ts") if isinstance(record.get("ts"), str) else "",
           "level": level if level in LEVELS else "INFO",
           "event": event if isinstance(event, str) and _EVENT.match(event) else "log"}
    fields = {}
    for key, value in record.items():
        if key in ("ts", "level", "event", "exc", "msg", "detail") or not _EVENT.match(key):
            continue
        if type(value) in (int, float, bool):
            fields[key] = value
        elif key in IDENT_KEYS and isinstance(value, str) and _IDENT.match(value):
            fields[key] = value
    out["fields"] = fields
    exc = record.get("exc")
    if isinstance(exc, str) and exc.strip():
        last = exc.strip().splitlines()[-1]
        m = _EXC.match(last)
        if m:
            out["exc"] = m.group(1)[-64:]
    return out


@dataclass
class LogView:
    """What the newest records say. `run_failed`: the latest run logged a failure (and so, when
    its process is gone, it crashed); `failure`: that record, content-free. `bridge`: the
    latest bridge record since the run started (None: none seen)."""
    started: str | None = None
    run_failed: bool = False
    failure: dict | None = None
    bridge: bool | None = None
    errors: list = field(default_factory=list)


def scan(records) -> LogView:
    view = LogView()
    failed = None
    # Between a clean end and the next start (False), inside a run (True), or not known: the
    # records begin inside a run more often than not (a long run, a midnight rotation)
    running = None
    for r in records:
        event = r.get("event")
        if event == STARTED:
            view.started = r.get("ts") if isinstance(r.get("ts"), str) else ""
            failed, view.bridge = None, None
            running = True
        elif event == CLEAN_END:
            running = False
        elif event in FAILURES:
            if (event not in KEPT_RUNNING or running is False
                    or r.get("level") in ("ERROR", "CRITICAL")):
                failed = r
        elif event in BRIDGE:
            view.bridge = BRIDGE[event]
        if r.get("level") in ("WARNING", "ERROR", "CRITICAL"):
            view.errors.append(r)
    view.run_failed = failed is not None
    view.failure = content_free(failed) if failed is not None else None
    view.errors = [content_free(r) for r in view.errors[-10:]]
    return view


class LogFollower:
    """Follows mind-data/logs/mind.log. poll() reads what was appended (or starts again from
    the last SEED_BYTES when the file was replaced or shrank) and returns whether anything
    new arrived."""

    def __init__(self, path: Path, *, seed_bytes: int = SEED_BYTES, keep: int = KEEP,
                 opener=_open_shared):
        self.path = Path(path)
        self.seed_bytes = seed_bytes
        self.records: collections.deque = collections.deque(maxlen=keep)
        self._opener = opener
        self._identity = None
        self._offset = 0
        self._partial = b""

    def _reset(self) -> None:
        self.records.clear()
        self._identity, self._offset, self._partial = None, 0, b""

    def poll(self) -> bool:
        try:
            fh = self._opener(self.path)
        except FileNotFoundError:
            changed = bool(self.records)
            self._reset()
            return changed
        except OSError:
            return False
        with fh:
            st = os.fstat(fh.fileno())
            identity = (st.st_dev, st.st_ino) if st.st_ino else None
            if (identity != self._identity and self._identity is not None) or st.st_size < self._offset:
                self._reset()
            self._identity = identity
            if st.st_size == self._offset:
                return False
            skip_first = False
            if self._offset == 0 and st.st_size > self.seed_bytes:
                self._offset = st.st_size - self.seed_bytes
                skip_first = True  # it starts inside a line
            fh.seek(self._offset)
            data = fh.read(st.st_size - self._offset)
        self._offset += len(data)
        data = self._partial + data
        lines = data.split(b"\n")
        self._partial = lines.pop()
        if skip_first and lines:
            lines.pop(0)
        added = False
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except (ValueError, RecursionError):
                continue
            if isinstance(record, dict):
                self.records.append(record)
                added = True
        return added

    def view(self) -> LogView:
        return scan(self.records)

    def tail(self, n: int = 60) -> list[dict]:
        return [content_free(r) for r in list(self.records)[-n:]]
