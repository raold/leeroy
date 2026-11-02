from dataclasses import dataclass
from typing import Literal

from leeroy_mind.protocol import Chat
from leeroy_mind.storage.rows import ChatLineRow
from leeroy_mind.storage.store import Store

MIN_PAIR_CHARS = 40
CAST_REFRESH_S = 60


def thread_key(ev: Chat) -> str:
    ch = ev.channel
    if ch == "whisper":
        lo, hi = sorted((ev.from_guid, ev.to_guid))
        return f"whisper:{lo}:{hi}"
    if ch in ("guild", "officer") or ch.startswith("voice:"):
        return ch
    if ch in ("party", "raid", "raid_warning"):
        return "group"
    return f"say:{ev.zone}"


@dataclass(frozen=True)
class ClosedThread:
    thread_id: int
    key: str
    line_ids: tuple[int, ...]
    reason: Literal["silence", "full", "flush"]


@dataclass
class _Open:
    thread_id: int
    line_ids: list[int]
    last_ts: float


class ThreadTracker:
    """Groups chat lines into threads by channel. A thread closes after `silence_s` of quiet or
    `max_lines` lines and is left with status 'pending' for the extractor. Times are mind-clock
    seconds. Whispers count only when a persona-bearing member is on one side."""

    def __init__(self, store: Store, *, silence_s: float = 90, max_lines: int = 12):
        self._store = store
        self._silence = silence_s
        self._max = max_lines
        self._open: dict[str, _Open] = {}
        self._cast: set[int] = set()
        self._cast_at: float | None = None

    def _close(self, key: str, reason: str, ts: float) -> ClosedThread:
        t = self._open.pop(key)
        self._store.close_thread(t.thread_id, ts, "pending")
        return ClosedThread(t.thread_id, key, tuple(t.line_ids), reason)

    def _is_cast(self, guid: int) -> bool:
        now = self._store.clock.now()
        if guid in self._cast:
            return True
        if self._cast_at is not None and now - self._cast_at < CAST_REFRESH_S:
            return False  # a cached negative: the human and strangers miss constantly
        self._cast = set(self._store.persona_versions())
        self._cast_at = now
        return guid in self._cast

    def add(self, line_id: int, ev: Chat, ts: float) -> ClosedThread | None:
        """Thread a received line. Echoes of lines the bridge sent are ignored: the mind's own
        lines enter through on_line_sent."""
        if ev.via_bridge:
            return None
        return self._add(line_id, ev, ts)

    def on_line_sent(self, line_id: int, chat: Chat) -> ClosedThread | None:
        """Entry point for the mind's own lines, called by the talk engine when a line is
        acknowledged. The line must already be in the chat log."""
        rows = self._store.chat_lines([line_id])
        ts = rows[0].ts if rows else self._store.clock.now()
        return self._add(line_id, chat, ts)

    def _add(self, line_id: int, ev: Chat, ts: float) -> ClosedThread | None:
        if ev.channel == "whisper" and not (self._is_cast(ev.from_guid) or self._is_cast(ev.to_guid)):
            return None
        key = thread_key(ev)
        closed = None
        cur = self._open.get(key)
        if cur is not None and ts - cur.last_ts > self._silence:
            closed = self._close(key, "silence", ts)
            cur = None
        if cur is None:
            cur = self._open[key] = _Open(self._store.open_thread(key, ts), [], ts)
        cur.line_ids.append(line_id)
        cur.last_ts = ts
        self._store.set_thread([line_id], cur.thread_id)
        if len(cur.line_ids) >= self._max:
            return self._close(key, "full", ts)  # a fresh thread never fills on its first line
        return closed

    def expire(self, now: float) -> list[ClosedThread]:
        due = [k for k, t in self._open.items() if now - t.last_ts > self._silence]
        return [self._close(k, "silence", now) for k in due]

    def flush_all(self) -> list[ClosedThread]:
        return [self._close(k, "flush", self._store.clock.now()) for k in list(self._open)]


def worth_extracting(lines: list[ChatLineRow], human_guid: int) -> bool:
    """A human line always counts; otherwise at least two lines and 40 characters of talk."""
    if any(ln.from_guid == human_guid for ln in lines):
        return True
    return len(lines) >= 2 and sum(len(ln.text.strip()) for ln in lines) >= MIN_PAIR_CHARS
