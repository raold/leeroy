"""A coarse A/B screen of rolling world-update time, never fight-local timing.

``get_stats.mean_ms`` is the core's integer mean of its last 500 world updates.
It includes other world work and can overlap a pull boundary. This gate compares
samples observed during matched encounters; it cannot establish causal overhead.

The adapter calls ``sample`` from its independent ticker, supplies ``watch_names``
as ``watch``, and fences disconnects with ``on_connection``. No task is spawned
here. Only completed, uninterrupted fights with at least two samples qualify.

Both offline raids and his raids (raid nights, and the add-command groups the
raid night adopts) are measured while the gate is in ``ab``. Each kind keeps its
own per-family parity counter and its own perf rows, and the verdict pairs an
armed with an unarmed fight only within one kind and one boss context, so his
presence can neither stand in for overhead nor hide it. A raid led by a bot has
dungeon-clear's muster naming the next boss and alternates by boss index. A raid
he leads has no such muster: the gate musters itself at the start and after
every finished pull, and alternates by pull (the pull's ordinal in the run takes
the boss index's place). After ``passed`` every pull is armed; after ``failed``
none is.
"""

import asyncio
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
import json
import math
from statistics import fmean
from typing import Literal
from uuid import uuid4

from leeroy_mind.protocol import Ack
from leeroy_mind.storage.store import Store
from leeroy_mind.wow import is_raid_fight

from .protocol import GetStats, ServerStats, Telemetry
from .store import perf_rows, put_perf


INTERVAL = 30.0
MAX_SAMPLES = 256
BASIS = "rolling_world_500_updates_v1"
KINDS = ("offline", "his")


def _number(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


@dataclass
class _Fight:
    id: str
    generation: str
    key: tuple[int, int, int]
    context: str
    raid: int
    armed: bool
    start_ms: int
    started: float
    due: float
    samples: list = field(default_factory=list)
    pending: bool = False
    kind: str = "offline"


class Gate:
    def __init__(self, ports, *, watch: Callable[[], tuple[int, ...]] = lambda: ()):
        self.ports, self.watch = ports, watch
        self.store = Store(ports.conn, clock=ports.clock)
        self._phase, self._raids, self._verdict_ms = "ab", 0, None
        saved = self._read("tg_gate")
        if saved is not None:
            if (isinstance(saved, dict) and type(saved.get("phase")) is str
                    and saved["phase"] in {"ab", "passed", "failed"}
                    and type(saved.get("raids")) is int and saved["raids"] >= 0
                    and (saved.get("verdict_ms") is None or _number(saved["verdict_ms"]))
                    and (saved["phase"] != "passed" or _number(saved.get("verdict_ms")))):
                self._phase, self._raids, self._verdict_ms = (
                    saved["phase"], saved["raids"], saved.get("verdict_ms"))
            else:
                self._phase = "failed"  # Corrupt state cannot silently re-arm a final gate.
        self._run = self._raid_index = self._boss_index = None
        # The parity slot of the current muster: the named boss, or for a raid he
        # leads the pull's ordinal. None before the run's first muster.
        self._slot = None
        self._pulls = 0
        self._pulling = None
        self._muster_ms = 0
        self._connection_ms = -1
        self._record, self._watch, self._acked = False, (), False
        self._revision = 0
        self._generation = uuid4().hex
        self._fight = None
        self._last_boss_ms = -1
        self._send_lock = asyncio.Lock()

    def _read(self, key):
        raw = self.store.get_meta(key)
        if raw is None:
            return None
        try:
            value = json.loads(raw)
            return False if value is None else value  # Only an absent key returns None.
        except (ValueError, TypeError):
            return False

    def _save(self):
        self.store.set_meta("tg_gate", json.dumps(dict(
            phase=self._phase, raids=self._raids, verdict_ms=self._verdict_ms), allow_nan=False))

    @property
    def phase(self) -> Literal["ab", "passed", "failed"]:
        return self._phase

    def _fence(self):
        self._revision += 1
        self._generation = uuid4().hex
        self._acked = False
        self._fight = None

    async def on_start(self, run):
        if self._run is run:
            return
        self._fence()
        self._run, self._raid_index, self._boss_index = run, None, None
        self._slot, self._pulls, self._pulling = None, 0, None
        self._record, self._last_boss_ms = False, -1
        if self.phase == "ab" and self._kind(run) is not None:
            self._count(run)
        if self._self_musters(run):
            await self._muster_next(run)

    def _count(self, run):
        # Raids rotate, so parity is counted within one kind, map and
        # difficulty: the family's next visit starts on the other side.
        family = self._family(run)
        key, count_key = f"tg_gate:raid:{run.session.raid_id}", f"tg_gate:family:{family}"
        saved, count = self._read(key), self._read(count_key)
        count = 0 if count is None else count
        if type(count) is not int or count < 0:
            return
        if saved is None:
            saved = dict(family=family, index=count)
            with self.store.transaction():
                self.store.set_meta(key, json.dumps(saved))
                self.store.set_meta(count_key, json.dumps(count + 1))
                self._raids += 1
                self._save()
            count += 1
        if (isinstance(saved, dict) and saved.get("family") == family
                and type(saved.get("index")) is int and 0 <= saved["index"] < count):
            self._raid_index = saved["index"]

    @staticmethod
    def _offline(run):
        return run.plan.offline and not run.human_in_raid and not run.plan.human_in_raid

    @classmethod
    def _kind(cls, run):
        """The measured kind: an offline raid, his raid, or None (never measured)."""
        if cls._offline(run):
            return "offline"
        if run.human_in_raid is True and not run.plan.offline and run.plan.human_in_raid:
            return "his"
        return None

    @classmethod
    def _family(cls, run):
        family = f"{run.session.ctx.map_id}:{int(run.plan.size == 25) + 2 * int(run.plan.heroic)}"
        # Offline families keep their original keys; his raids count apart.
        return family if cls._offline(run) else f"his:{family}"

    @classmethod
    def _self_musters(cls, run):
        """His own lead sends no dungeon-clear muster, so the gate musters each pull itself."""
        return run is not None and cls._kind(run) == "his" and getattr(run, "lead_is_human", False) is True

    def record_flag(self, run, boss_index: int) -> bool:
        """The flag for a pull. ``boss_index`` is the parity slot: the boss a muster
        names, or for a raid he leads the pull's ordinal in the run."""
        if self.phase == "failed" or not self.ports.cfg.telemetry:
            return False
        if self.phase == "passed":
            return True
        return bool(run is self._run and self._kind(run) is not None and self._raid_index is not None
                    and type(boss_index) is int and boss_index >= 0
                    and (self._raid_index + boss_index) % 2 == 0)

    def recording(self) -> bool:
        return bool(self._run is not None and self._slot is not None
                    and not getattr(self._run.session, "_closing", False)
                    and self._record and self._acked and self.ports.cfg.telemetry
                    and self.phase != "failed")

    async def _send(self):
        revision, run = self._revision, self._run
        async with self._send_lock:
            if revision != self._revision or run is not self._run:
                return
            cmd = Telemetry(uuid4().hex, self._record, self._watch)
            try:
                ack = await self.ports.send(cmd)
            except Exception:
                return
            if revision == self._revision and run is self._run:
                self._acked = isinstance(ack, Ack) and ack.id == cmd.id and ack.ok is True and not ack.error

    async def on_muster(self, run, boss_index):
        if run is not self._run or getattr(run.session, "_closing", False):
            return
        record, watch = self.record_flag(run, boss_index), tuple(self.watch())
        changed = (record, watch) != (self._record, self._watch)
        # A retry of this same boss is a new pull boundary too. The previous
        # command ACK can be reused, but its unfinished fight cannot.
        self._fight = None
        self._boss_index = self._slot = boss_index
        self._muster_ms = int(self.ports.clock.now() * 1000)
        if changed or not self._acked:
            self._fence()
            self._record, self._watch = record, watch
            await self._send()

    async def _muster_next(self, run):
        """His lead's next pull, whatever boss it is: the slot is the pull's ordinal."""
        if run is not self._run or getattr(run.session, "_closing", False):
            return
        slot = self._pulls
        record, watch = self.record_flag(run, slot), tuple(self.watch())
        changed = (record, watch) != (self._record, self._watch)
        self._fight = self._pulling = None
        self._boss_index, self._slot = None, slot
        self._muster_ms = int(self.ports.clock.now() * 1000)
        if changed or not self._acked:
            self._fence()
            self._record, self._watch = record, watch
            await self._send()

    def invalidate(self, run):
        if run is self._run:
            self._fence()
            self._run = self._boss_index = self._raid_index = self._slot = self._pulling = None
            self._record = False

    async def on_end(self, outcome, *, run=None):
        if run is not None and self._run is not None and run is not self._run:
            return
        self.invalidate(self._run)
        await self._send()

    async def on_connection(self, connected):
        # Preserve the desired flag for hello, never a pre-disconnect ACK/fight.
        self._fence()
        self._connection_ms = max(self._connection_ms, int(self.ports.clock.now() * 1000))

    async def on_hello(self):
        self._fence()
        self._connection_ms = max(self._connection_ms, int(self.ports.clock.now() * 1000))
        self._watch = tuple(self.watch())
        if self._run is not None:
            self._record = self.record_flag(self._run, self._slot)
        await self._send()

    def _matches(self, ev):
        run = self._run
        if run is None or getattr(run.session, "_closing", False):
            return False
        ctx = run.session.ctx
        return (type(ev.ts) is int and ev.ts >= max(self._muster_ms, run.session.started * 1000)
                # Same-millisecond ordering around a connection/hello boundary
                # is unknown. A later muster must not lower this causal floor.
                and ev.ts > self._connection_ms
                and ev.ts <= self.ports.clock.now() * 1000 + 5000
                and type(ctx.instance_id) is int and ctx.instance_id > 0
                and ev.map == ctx.map_id and ev.instance_id == ctx.instance_id
                and self._slot is not None
                and (ev.boss_id == self._boss_index or self._any_boss())
                and is_raid_fight(ev.map, ev.boss_id)
                and bool(set(run.plan.members).intersection(ev.members)))

    def _any_boss(self):
        """A self-muster names no boss: the next raid fight engaged is its pull."""
        return self._boss_index is None and self._slot is not None and self._self_musters(self._run)

    async def on_boss(self, ev):
        if not self._matches(ev) or ev.state not in {"engaged", "killed", "wiped"}:
            return
        key = (ev.map, ev.instance_id, ev.boss_id)
        if ev.state == "engaged":
            if ev.ts <= self._last_boss_ms:
                return
            self._last_boss_ms = ev.ts
            self._fight = None
            self._pulling = (key, ev.ts)
            kind = self._kind(self._run)
            if self.phase != "ab" or kind is None or not self._acked:
                return
            plan = self._run.plan
            difficulty = int(plan.size == 25) + 2 * int(plan.heroic)
            now = self.ports.clock.now()
            self._fight = _Fight(f"gate:{self._generation}:{uuid4().hex}", self._generation, key,
                f"{ev.map}:{ev.boss_id}:{difficulty}", self._run.session.raid_id,
                self._record, ev.ts, now, now + INTERVAL, kind=kind)
            return
        fight, pulling = self._fight, self._pulling
        ours = fight is not None and fight.key == key and ev.ts >= fight.start_ms
        # His lead's next pull is mustered once this one ends, armed or not.
        advance = (self._self_musters(self._run) and pulling is not None
                   and pulling[0] == key and ev.ts >= pulling[1])
        if not ours and not advance:
            return
        self._last_boss_ms = max(self._last_boss_ms, ev.ts)
        self._pulling = None
        changed = False
        if ours:
            self._fight = None  # An in-flight response can never finish this fight afterward.
        if ours and len(fight.samples) >= 2 and fight.generation == self._generation:
            mean = fmean(s[2] for s in fight.samples)
            receipt = dict(basis=BASIS, fight=fight.id, generation=fight.generation,
                raid=fight.raid, context=fight.context, armed=fight.armed, kind=fight.kind,
                instance_id=fight.key[1], started=fight.started,
                start_ms=fight.start_ms, end_ms=ev.ts, outcome=ev.state,
                count=len(fight.samples), samples=fight.samples, mean_ms=mean)
            # put_perf owns a transaction and deduplicates by fight. Write the
            # receipt second: a crash leaves an ineligible orphan, never proof.
            put_perf(self.ports.conn, self.ports.clock.now(), fight.id, fight.context,
                     fight.kind, fight.armed, mean)
            self.store.set_meta(f"tg_gate:perf:{fight.id}", json.dumps(receipt, allow_nan=False))
            prior = self.phase
            self.verdict()
            changed = self.phase != prior
        if advance and self._run is not None:
            self._pulls += 1
            await self._muster_next(self._run)
        elif changed:
            await self._send()

    async def sample(self):
        """One throttled ticker step; no catch-up bursts and no concurrent polls."""
        fight, run, revision = self._fight, self._run, self._revision
        now = self.ports.clock.now()
        if (fight is None or fight.pending or now < fight.due or not self._acked
                or len(fight.samples) >= MAX_SAMPLES or self.phase != "ab"
                or not self._current_fight(fight, run)):
            return
        fight.pending = True
        fight.due = now + INTERVAL
        cmd = GetStats(uuid4().hex)
        try:
            rows = await self.ports.request(cmd, "server_stats",
                until=lambda ev: isinstance(ev, ServerStats) and ev.id == cmd.id, timeout_s=5)
        except Exception:
            return
        finally:
            fight.pending = False
        if (self._fight is not fight or self._run is not run or revision != self._revision
                or fight.generation != self._generation or not self._acked
                or not self._current_fight(fight, run)):
            return
        if not isinstance(rows, (list, tuple)):
            return
        stats = [ev for ev in rows if isinstance(ev, ServerStats) and ev.id == cmd.id]
        if len(stats) != 1:
            return
        ev = stats[0]
        if (type(ev.mean_ms) is not int or not 0 <= ev.mean_ms <= 0xFFFFFFFF
                or type(ev.ts) is not int or ev.ts < max(fight.start_ms, int(now * 1000) - 5000)
                or ev.ts > self.ports.clock.now() * 1000 + 5000
                or ev.telemetry is not fight.armed or type(ev.armed) is not int
                or ev.armed != int(fight.armed)):
            self._fight = None
            return
        if fight.samples and (now - fight.samples[-1][0] < INTERVAL
                              or ev.ts - fight.samples[-1][1] < INTERVAL * 1000):
            return
        fight.samples.append([now, ev.ts, ev.mean_ms])
        fight.due = self.ports.clock.now() + INTERVAL

    def _current_fight(self, fight, run):
        boss = fight.key[2] if self._any_boss() else self._boss_index
        return (run is not None and not getattr(run.session, "_closing", False)
                and (run.session.ctx.map_id, run.session.ctx.instance_id, boss) == fight.key
                and self.record_flag(run, self._slot) == fight.armed == self._record)

    def _eligible(self, row):
        r = self._read(f"tg_gate:perf:{row['fight']}")
        # A receipt written before his raids were measured names no kind: offline.
        if (not isinstance(r, dict) or r.get("kind", "offline") != row["kind"]
                or r.get("basis") != BASIS
                or r.get("fight") != row["fight"] or r.get("context") != row["context"]
                or type(r.get("armed")) is not bool or int(r["armed"]) != row["armed"]
                or type(r.get("generation")) is not str or not r["generation"]
                or not row["fight"].startswith(f"gate:{r['generation']}:")
                or type(r.get("raid")) is not int or r["raid"] <= 0
                or type(r.get("instance_id")) is not int or r["instance_id"] <= 0
                or not _number(r.get("started"))
                or type(r.get("outcome")) is not str or r["outcome"] not in {"killed", "wiped"}
                or type(r.get("start_ms")) is not int or type(r.get("end_ms")) is not int
                or r["end_ms"] < r["start_ms"]):
            return False
        try:
            map_id, boss_id, difficulty = map(int, r["context"].split(":"))
        except (ValueError, AttributeError):
            return False
        if (r["context"] != f"{map_id}:{boss_id}:{difficulty}" or not 0 <= difficulty <= 3
                or boss_id < 0 or not is_raid_fight(map_id, boss_id)):
            return False
        samples = r.get("samples")
        if (type(samples) is not list or not 2 <= len(samples) <= MAX_SAMPLES
                or type(r.get("count")) is not int or r["count"] != len(samples)):
            return False
        previous = None
        for s in samples:
            if (type(s) is not list or len(s) != 3 or not _number(s[0])
                    or s[0] < r["started"] + INTERVAL
                    or type(s[1]) is not int or not r["start_ms"] <= s[1] <= r["end_ms"]
                    or type(s[2]) is not int or not 0 <= s[2] <= 0xFFFFFFFF
                    or previous is not None and (s[0] - previous[0] < INTERVAL
                                                 or s[1] - previous[1] < INTERVAL * 1000)):
                return False
            previous = s
        mean = fmean(s[2] for s in samples)
        return _number(r.get("mean_ms")) and r["mean_ms"] == mean == row["mean_ms"]

    def verdict(self) -> tuple[str, float | None]:
        if self.phase != "ab":
            return self.phase, self._verdict_ms
        # Pairs stay within one kind: his raids and offline raids never meet.
        contexts = defaultdict(lambda: defaultdict(list))
        seen = set()
        for kind in KINDS:
            for row in perf_rows(self.ports.conn, kind=kind):
                if row["fight"] not in seen and self._eligible(row):
                    contexts[kind, row["context"]][row["armed"]].append(row["mean_ms"])
                    seen.add(row["fight"])
        differences = [fmean(sides[1]) - fmean(sides[0]) for sides in contexts.values()
                       if sides[0] and sides[1]]
        if len(differences) >= self.ports.cfg.gate_pulls:
            self._verdict_ms = fmean(differences)
            self._phase = "passed" if self._verdict_ms <= self.ports.cfg.gate_max_ms else "failed"
            self._fence()
            self._record = bool(self.phase == "passed" and self.ports.cfg.telemetry
                                and self._run is not None and self._slot is not None)
            self._save()
        return self.phase, self._verdict_ms
