"""Training interfaces, bounded response collection, and the lazy live boundary."""

import asyncio
import math
import sqlite3
from uuid import uuid4
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
    reserved: bool = False


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
            discard = next((k for k, v in self._streams.items() if not v.waiting and not v.reserved), None)
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

    def reserve(self, cmd_id, event_type):
        """Pin a response stream before sending; release it even if sending fails.

        Reserved streams consume the same bounded capacity as active waiters.
        Their prefix and overflow flag survive unsolicited traffic while an ACK
        is pending. A reservation permits exactly one subsequent wait.
        """
        if type(cmd_id) is not str or not cmd_id or event_type not in TG_EVENT_TYPES:
            raise ValueError("invalid training event reservation")
        key = (cmd_id, event_type)
        if key in self._closed:
            raise ValueError("training request ID already completed or evicted")
        stream = self._stream(key)
        if stream.waiting or stream.reserved:
            raise ValueError("training request already reserved or waiting")
        stream.reserved = True

    def release(self, cmd_id, event_type):
        """Close a reservation, including failed/cancelled send-before-wait."""
        self._finish((cmd_id, event_type))

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


class LivePorts:
    def __init__(self, app, *, cfg=None):
        from leeroy_mind.guild.ports import LivePorts as GuildPorts
        from .config import load_training_config
        self.app, self.clock, self.conn = app, app.clock, app.store.conn
        self.cfg = cfg or load_training_config(app.data_dir / "mind.toml")
        guild = getattr(app, "guild", None)
        self.guild = guild.ports if guild is not None else GuildPorts(app)
        self.waiter = EventWaiter()
        self._requests = set()
        self._generation = 0
        self._prefix, self._sequence = uuid4().hex, 0

    def __getattr__(self, name):
        return getattr(self.guild, name)

    def feed(self, ev):
        self.waiter.feed(ev)

    @property
    def connection_epoch(self):
        """Local observed transport/hello generation; never proof of remote state."""
        return self._generation

    async def disconnect(self):
        self._generation += 1
        tasks = tuple(self._requests)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.waiter = EventWaiter()

    async def request(self, cmd, event_type, *, until, timeout_s):
        from dataclasses import replace
        self._sequence += 1
        ident = cmd.id
        if not isinstance(ident, str) or not ident:
            raise ValueError("request requires a unique command ID")
        # Wire IDs are unique even after mailbox eviction and caller-ID reuse.
        # Translate only responses correlated to this wire request back to the
        # caller's immutable command ID (also used by its terminal predicate).
        wire = replace(cmd, id=f"{self._prefix}_{self._sequence}")
        generation, waiter = self._generation, self.waiter
        task = asyncio.current_task()
        if len(self._requests) >= EventWaiter.MAX_STREAMS:
            raise BufferError("training request capacity reached")
        waiter.reserve(wire.id, event_type)
        self._requests.add(task)
        try:
            async with asyncio.timeout(timeout_s):
                ack = await self.send(wire)
                if (generation != self._generation or not isinstance(ack, Ack)
                        or ack.id != wire.id or ack.ok is not True or ack.error):
                    return []
                rows = await waiter.wait(wire.id, event_type,
                    until=lambda ev: until(replace(ev, id=ident)), timeout_s=timeout_s)
                return [replace(ev, id=ident) for ev in rows] if generation == self._generation else []
        except TimeoutError:
            return []
        finally:
            waiter.release(wire.id, event_type)
            self._requests.discard(task)

    def drunk(self, guid):
        return self.app.store.get_state(guid).altered_state == "drunk"

    def flask_skips(self):
        habits = getattr(getattr(self.app, "guild", None), "habits", None)
        return set(getattr(habits, "flask_skips", ()))

    def mentor_of(self, guid):
        try:
            row = self.conn.execute("SELECT mentor FROM mentor WHERE mentee=? AND ended IS NULL "
                "AND started<=? AND ends>?", (guid, self.clock.now(), self.clock.now())).fetchone()
            return row[0] if row else None
        except sqlite3.OperationalError as error:
            if "no such table" not in str(error):
                raise
            return None

    def affinity(self, a, b):
        return self.app.store.get_relationship(a, b).affinity

    def on_comms(self, guid):
        comms = getattr(getattr(self.app, "voice", None), "comms", None)
        return comms is not None and comms.room_of(guid) is not None

    def away(self):
        try:
            return self.guild.away()
        except (ImportError, sqlite3.OperationalError):
            return set()

    async def say(self, guid, channel, text, *, kind=TurnKind.CALLOUT, to_guid=0):
        return await self.guild.say(guid, channel, text, kind=kind, to_guid=to_guid)

    def standing_lead(self):
        return self.app.raid.lead()

    def boss_mechanics(self, boss, instance):
        from leeroy_mind.talk.facts import boss_mechanics
        library = getattr(self.app, "library", None)
        return boss_mechanics(library.strategy_for(boss, instance)) if library else []

    def claim(self, owner, guids, *, until):
        bodies = getattr(self.app, "bodies", None)
        return bodies.claim(owner, guids, rank="training", until=until) if bodies else set(guids)

    def release(self, owner, guids=None):
        bodies = getattr(self.app, "bodies", None)
        if bodies:
            bodies.release(owner, guids)

    def holder(self, guid):
        bodies = getattr(self.app, "bodies", None)
        return bodies.holder(guid) if bodies else None

    def benchmark_allowed(self, map_id, boss_id, size, heroic):
        race = getattr(getattr(self.app, "realm", None), "race", None)
        return race.benchmark_allowed(map_id, boss_id, size, heroic) if race else None

    def raid_at(self, ms):
        from leeroy_mind.guild.raids import CATALOG
        rows = self.conn.execute("SELECT * FROM guild_raid WHERE started<=? "
            "AND (ended IS NULL OR ended>?) LIMIT 2", (ms / 1000, ms / 1000)).fetchall()
        if len(rows) != 1 or rows[0]["raid_key"] not in CATALOG:
            return None
        row = rows[0]
        return RaidRow(row["raid_id"], bool(row["offline"]), CATALOG[row["raid_key"]].map_id,
            row["size"], bool(row["heroic"]), row["lead_guid"], row["started"], row["ended"])
