"""Bounded, content-free reads of the retained native and mind logs for the ledger's signatures.

Newest files first, through shared read handles (a rotation is never blocked), with a byte budget
per source, a file and match cap and a deadline; symlinks and junctions are refused. Each source
is scanned for its signatures' literal tokens in large blocks, so the many unrelated lines cost no
per-line work. A line a signature matches is reduced at once to its time and, when the signature
names one, a code: nothing of its text is kept or returned.

Counts are lower bounds. Budgets, rotation and retention cut older evidence off; `coverage` says
for each source how far back it was read and whether it was read whole."""
import json
import os
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from leeroy_mind.console.mindlog import _open_shared

# Native rotations are "<name>.log.YYYY-MM-DD HH-MM-SS" (AppenderFile), the mind's "mind.log.YYYY-MM-DD"
SOURCES = {
    "server": ("native", re.compile(r"Server\.log(?:\.\d{4}-\d\d-\d\d \d\d-\d\d-\d\d)?\Z")),
    "playerbots": ("native", re.compile(r"Playerbots\.log(?:\.\d{4}-\d\d-\d\d \d\d-\d\d-\d\d)?\Z")),
    "mind": ("mind", re.compile(r"mind\.log(?:\.\d{4}-\d\d-\d\d)?\Z")),
}
MIB = 1024 * 1024
BUDGETS = {"server": 64 * MIB, "playerbots": 256 * MIB, "mind": 64 * MIB}
CHUNK = 4 * MIB
MAX_LINE = 16384
MAX_FILES = 64
MAX_ENTRIES = 4096
MAX_LINES = 400_000  # matched lines per source
CODE = re.compile(r"[A-Za-z][A-Za-z0-9_.:-]{0,63}\Z")
_STAMP = re.compile(rb"(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) ")
_MIND_SKIP = frozenset(("ts", "level", "event", "exc"))
# Issues that leave lines of a source unread; the others drop single lines only.
PARTIAL = frozenset(("deadline", "directory_budget", "file_limit", "linked_path", "no_directory", "unavailable",
                     "read_budget", "truncated_file", "match_limit", "changed_file", "unreadable_file"))


@dataclass(frozen=True)
class Signature:
    key: str  # "<feature>.<evidence>"
    source: str  # server, playerbots or mind
    token: bytes  # a literal every matching line carries
    pattern: re.Pattern
    since: float | None = None  # evidence before this is ignored


@dataclass
class Tally:
    count: int = 0
    first: float | None = None
    last: float | None = None
    codes: Counter = field(default_factory=Counter)

    def add(self, at: float, code: str | None = None) -> None:
        self.count += 1
        self.first = at if self.first is None else min(self.first, at)
        self.last = at if self.last is None else max(self.last, at)
        if code is not None:
            self.codes[code] += 1


def _linked(path: Path) -> bool:
    return any(p.is_symlink() or p.is_junction() for p in (path, *path.parents))


def native_time(stamp: bytes, tz=None, _cache={}) -> float:
    """Host wall time with whole seconds; an ambiguous DST hour reads as its first fold."""
    key = (stamp, tz)
    at = _cache.get(key)
    if at is None:
        if len(_cache) > 65536:
            _cache.clear()
        at = _cache[key] = datetime.strptime(stamp.decode("ascii"), "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=tz).timestamp()
    return at


def mind_text(record) -> str | None:
    """The record as its message read: "event k=v ..." for a structured event, the message itself
    for one kept whole. Only ever matched against, never returned."""
    if not isinstance(record, dict):
        return None
    event = record.get("event")
    if event == "log":
        msg = record.get("msg")
        return msg if isinstance(msg, str) else None
    if not isinstance(event, str):
        return None
    parts = [event]
    for key, value in record.items():
        if key not in _MIND_SKIP:
            parts.append(f"{key}={value if isinstance(value, str) else json.dumps(value)}")
    return " ".join(parts)


def _read_line(kind: str, line: bytes, tz):
    """(time, text) of a log line, or None when it carries no readable time."""
    if kind == "native":
        stamp = _STAMP.match(line)
        if stamp is None:
            return None
        return native_time(stamp.group(1), tz), line[stamp.end():].decode("utf-8", "replace")
    record = json.loads(line)
    text = mind_text(record)
    at = datetime.fromisoformat(record["ts"]).timestamp()
    return (at, text) if text is not None else None


def _line_time(kind: str, line: bytes, tz) -> float | None:
    try:
        if kind == "native":
            stamp = _STAMP.match(line)
            return native_time(stamp.group(1), tz) if stamp else None
        return datetime.fromisoformat(json.loads(line)["ts"]).timestamp()
    except (ValueError, TypeError, KeyError, OverflowError, OSError, RecursionError, AttributeError):
        return None


def _files(directory: Path, pattern, deadline, issues):
    if _linked(directory):
        issues["linked_path"] += 1
        return []
    found = []
    try:
        with os.scandir(directory) as entries:
            for n, entry in enumerate(entries):
                if time.monotonic() >= deadline:
                    issues["deadline"] += 1
                    break
                if n >= MAX_ENTRIES:
                    issues["directory_budget"] += 1
                    break
                if not pattern.fullmatch(entry.name):
                    continue
                path = Path(entry.path)
                if _linked(path) or not entry.is_file(follow_symlinks=False):
                    issues["linked_or_nonfile"] += 1
                    continue
                stat = path.stat(follow_symlinks=False)
                found.append((stat.st_mtime, entry.name, path, stat))
    except FileNotFoundError:
        issues["no_directory"] += 1
        return []
    except OSError:
        issues["unavailable"] += 1
        return []
    found.sort(key=lambda row: (row[0], row[1]), reverse=True)
    if len(found) > MAX_FILES:
        issues["file_limit"] += len(found) - MAX_FILES
    return found[:MAX_FILES]


class _Source:
    def __init__(self, name, kind, signatures, tallies, tz, deadline):
        self.name, self.kind, self.tz, self.deadline = name, kind, tz, deadline
        self.signatures, self.tallies = signatures, tallies
        self.tokens = sorted({s.token for s in signatures})
        self.issues = Counter()
        self.lines = 0
        self.earliest = None
        self.stopped = False

    def note_start(self, block: bytes) -> None:
        """The first readable time of a read region: how far back this source was read."""
        for line in block.split(b"\n", 64)[:64]:
            at = _line_time(self.kind, line[line.rfind(b"\0") + 1:].rstrip(b"\r"), self.tz)
            if at is not None:
                self.earliest = at if self.earliest is None else min(self.earliest, at)
                return

    def block(self, block: bytes) -> None:
        spans = {}
        for token in self.tokens:
            pos = block.find(token)
            while pos != -1:
                start = block.rfind(b"\n", 0, pos) + 1
                end = spans.get(start)
                if end is None:
                    end = block.find(b"\n", pos)
                    spans[start] = end = len(block) if end == -1 else end
                pos = block.find(token, end)
        for start in sorted(spans, reverse=True):  # newest first, as the blocks come
            if self.lines >= MAX_LINES:
                self.issues["match_limit"] += 1
                self.stopped = True
                return
            if not self.lines % 4096 and time.monotonic() >= self.deadline:
                self.issues["deadline"] += 1
                self.stopped = True
                return
            self.lines += 1
            line = block[start:spans[start]].rstrip(b"\r")
            if b"\0" in line:  # NUL padding a crash left before the line
                line = line[line.rfind(b"\0") + 1:]
            if len(line) > MAX_LINE:
                self.issues["oversized_line"] += 1
                continue
            try:
                read = _read_line(self.kind, line, self.tz)
            except (ValueError, TypeError, KeyError, OverflowError, OSError, RecursionError, AttributeError):
                self.issues["invalid_record"] += 1
                continue
            if read is None:
                continue
            at, text = read
            for sig in self.signatures:
                if sig.token not in line or (sig.since is not None and at < sig.since):
                    continue
                found = sig.pattern.search(text)
                if found is None:
                    continue
                code = found.groupdict().get("code")
                self.tallies[sig.key].add(at, code if isinstance(code, str) and CODE.fullmatch(code) else None)

    def read_region(self, stream, lo: int, hi: int) -> int:
        """Whole lines of [lo, hi) to block(), the newest chunk first, so a match cap or the
        deadline drops the oldest evidence, never the newest. A partial line at lo (when lo > 0),
        the line still being written at hi and an oversized line are left out; NUL padding a crash
        left before a line is not part of it. Returns the bytes read."""
        pos, carry, read, skip = hi, b"", 0, True  # skip: drop bytes back to the previous newline
        while pos > lo and not self.stopped:
            if time.monotonic() >= self.deadline:
                self.issues["deadline"] += 1
                self.stopped = True
                break
            start = max(lo, pos - CHUNK)
            stream.seek(start)
            chunk = stream.read(pos - start)
            if not chunk:
                break
            read += len(chunk)
            pos = start
            buf, carry = chunk + carry, b""
            if skip:
                last = buf.rfind(b"\n")
                if last < 0:
                    continue
                buf, skip = buf[:last + 1], False
            if start == 0:
                body = buf
            else:
                # the bytes up to the first newline end a line that began further back
                cut = buf.find(b"\n") + 1
                head, body = (buf[:cut], buf[cut:]) if cut else (buf, b"")
                if start > lo:
                    carry = head
            if len(carry) > MAX_LINE:
                nul = carry.rfind(b"\0")
                line = carry[nul + 1:] if nul >= 0 else carry
                if len(line) <= MAX_LINE:
                    body = line + body  # a whole line after NUL padding
                elif nul < 0 or line.strip(b"\r\n"):
                    self.issues["oversized_line"] += 1
                carry, skip = b"", True
            if body:
                self.note_start(body)
                self.block(body)
        return read


def scan(directories: dict[str, Path], signatures, *, deadline: float, budgets=None, tz=None):
    """({signature key: Tally}, {source: coverage}). Only the sources a signature names are read."""
    budgets = {**BUDGETS, **(budgets or {})}
    tallies = {s.key: Tally() for s in signatures}
    coverage = {}
    for name, (kind, pattern) in SOURCES.items():
        mine = [s for s in signatures if s.source == name]
        if not mine or name not in directories:
            continue
        source = _Source(name, kind, mine, tallies, tz, deadline)
        files = _files(Path(directories[name]), pattern, deadline, source.issues)
        remaining, read_bytes, scanned, whole = budgets[name], 0, 0, True
        seen = set()
        for _, _, path, before in files:
            if source.stopped:
                whole = False
                break
            if remaining <= 0 or time.monotonic() >= deadline:
                source.issues["read_budget" if remaining <= 0 else "deadline"] += 1
                whole = False
                break
            identity = (before.st_dev, before.st_ino)
            if identity in seen:
                continue
            seen.add(identity)
            try:
                with _open_shared(path) as stream:
                    opened = os.fstat(stream.fileno())
                    if _linked(path) or (opened.st_dev, opened.st_ino) != identity:
                        source.issues["changed_file"] += 1
                        whole = False
                        continue
                    take = min(opened.st_size, remaining)
                    lo = opened.st_size - take
                    scanned += 1
                    if lo:
                        whole = False
                        source.issues["truncated_file"] += 1
                    read_bytes += source.read_region(stream, lo, opened.st_size)
                    remaining -= take
            except FileNotFoundError:
                continue  # rotated away meanwhile
            except OSError:
                source.issues["unreadable_file"] += 1
                whole = False
        coverage[name] = {"files": scanned, "bytes": read_bytes, "from": source.earliest,
                          "complete": whole and not source.stopped and not source.issues.keys() & PARTIAL,
                          "issues": dict(sorted(source.issues.items()))}
    return tallies, coverage
