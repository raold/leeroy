"""Training interfaces and bounded event collection; no live adapter is installed."""

import asyncio
import math
from collections import OrderedDict, deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Protocol

from leeroy_mind.guild.protocol import Job
from leeroy_mind.human import HumanIdentity
from leeroy_mind.memory.writer import MemoryCandidate
from leeroy_mind.protocol import Ack, Member, event_type
from leeroy_mind.talk.turns import TurnKind

from .config import TrainingConfig
from .protocol import TG_EVENT_TYPES


@dataclass(frozen=True, slots=True)
class RaidRow:
    raid_id: int
    offline: bool
    map_id: int
    size: int
    heroic: bool
    lead_guid: int
    # Optional bounds support precise historical lookup without a live RaidRun.
    started: float = 0.0
    ended: float | None = None


class TrainingPorts(Protocol):
    clock: object
    conn: object
    cfg: TrainingConfig
    human_guid: int

    def human(self) -> HumanIdentity: ...
    def human_online(self) -> bool: ...
    def cast(self) -> dict[int, Member]: ...
    def persona(self, guid): ...
    def persona_state(self, guid): ...
    def drunk(self, guid) -> bool: ...
    def flask_skips(self) -> set[int]: ...
    def mentor_of(self, guid) -> int | None: ...
    def affinity(self, a, b) -> int: ...
    def on_comms(self, guid) -> bool: ...
    def away(self) -> set[int]: ...
    async def send(self, cmd) -> Ack: ...
    async def wait_job(self, cmd_id: str, timeout_s: float) -> Job | None: ...
    async def request(self, cmd, event_type: str, *, until: Callable[[object], bool],
                      timeout_s: float) -> list[object]: ...
    def propose(self, guid, channel, instruction, *, kind=TurnKind.ASIDE): ...
    async def say(self, guid, channel, text, *, kind=TurnKind.CALLOUT, to_guid=0) -> bool: ...
    async def remember(self, c: MemoryCandidate): ...
    def standing_lead(self) -> int | None: ...
    def boss_mechanics(self, boss: str, instance: str | None) -> list[str]: ...
    def claim(self, owner: str, guids: Iterable[int], *, until: float) -> set[int]: ...
    def release(self, owner: str, guids: Iterable[int] | None = None) -> None: ...
    def holder(self, guid) -> str | None: ...
    def benchmark_allowed(self, map_id, boss_id, size, heroic) -> bool | None: ...
    def raid_at(self, ms: int) -> RaidRow | None: ...


@dataclass
class _Stream:
    rows: deque = field(default_factory=deque)
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    waiting: bool = False
    overflow: bool = False


class EventWaiter:
    """Collect only a command's matching event type through its terminal marker.

    Early responses are retained for send-then-wait callers. Storage is bounded
    even without a waiter; overflow fails explicitly, never returning a suffix
    as though it were complete. Command IDs must be unique per request. Timeout
    returns its partial rows; cancellation propagates and releases the stream.
    All calls belong to the event-loop thread.
    """

    MAX_STREAMS = 128
    MAX_EVENTS = 512

    def __init__(self):
        self._streams: OrderedDict[tuple[str, str], _Stream] = OrderedDict()
        self._closed: OrderedDict[tuple[str, str], None] = OrderedDict()

    def _stream(self, key):
        if key in self._streams:
            return self._streams[key]
        if len(self._streams) >= self.MAX_STREAMS:
            discard = next((k for k, v in self._streams.items() if not v.waiting), None)
            if discard is None:
                raise BufferError("training event wait capacity reached")
            self._streams.pop(discard)
            self._finish(discard)
        stream = self._streams[key] = _Stream()
        return stream

    def _finish(self, key):
        self._streams.pop(key, None)
        self._closed[key] = None
        while len(self._closed) > self.MAX_STREAMS:
            self._closed.popitem(last=False)

    def feed(self, ev):
        kind, ident = event_type(ev), getattr(ev, "id", None)
        if kind not in TG_EVENT_TYPES or type(ident) is not str or not ident:
            return
        key = (ident, kind)
        if key in self._closed:
            return
        try:
            stream = self._stream(key)
        except BufferError:
            return  # Active requests are retained; unsolicited streams cannot evict them.
        if len(stream.rows) >= self.MAX_EVENTS:
            stream.overflow = True
        else:
            stream.rows.append(ev)
        stream.changed.set()

    async def wait(self, cmd_id, event_type, *, until, timeout_s) -> list[object]:
        if (type(cmd_id) is not str or not cmd_id or event_type not in TG_EVENT_TYPES
                or type(timeout_s) not in (int, float) or not math.isfinite(timeout_s) or timeout_s < 0):
            raise ValueError("invalid training event wait")
        key = (cmd_id, event_type)
        if key in self._closed:
            raise ValueError("training request ID already completed or evicted")
        stream = self._stream(key)
        if stream.waiting:
            raise ValueError("training request already has a waiter")
        stream.waiting = True
        result = []
        try:
            async with asyncio.timeout(timeout_s):
                while True:
                    if stream.overflow:
                        raise BufferError("training event response exceeded capacity")
                    while stream.rows:
                        ev = stream.rows.popleft()
                        result.append(ev)
                        if until(ev):
                            return result
                        if len(result) >= self.MAX_EVENTS:
                            raise BufferError("training event response exceeded capacity")
                    stream.changed.clear()
                    await stream.changed.wait()
        except TimeoutError:
            return result
        finally:
            self._finish(key)
