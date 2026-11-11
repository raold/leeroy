"""Realm's only boundary to neighboring controllers and persisted evidence.

Read methods never claim bodies or finalize jobs. BenchmarkPull is a completed
run summary; MemberMeter exposes raw counters and their recorded metric bases.
"""

import asyncio
from collections import OrderedDict, deque
from dataclasses import dataclass
from datetime import datetime, time
import json
import logging
import math
from pathlib import Path
import re
import sqlite3
import statistics
from typing import Protocol

from leeroy_mind.clock import SystemClock, night_of
from leeroy_mind.bodies import ALL, BodyHold, RunBodyProof
from leeroy_mind.human import HumanIdentity, human_identity
from leeroy_mind.llm.client import LLMError
from leeroy_mind.llm.presets import LIVE
from leeroy_mind.llm.scheduler import Priority, SchedulerError
from leeroy_mind.memory.writer import MemoryCandidate, WriteResult
from leeroy_mind.personas.io import PersonaCache
from leeroy_mind.personas.schema import Persona
from leeroy_mind.protocol import Ack, Member
from leeroy_mind.storage.rows import Relationship
from leeroy_mind.storage.store import Store
from leeroy_mind.talk.turns import Turn, TurnKind
from leeroy_mind.wow import is_raid_fight

from .armory import RealmArmory, RealmArmoryUnavailable

log = logging.getLogger(__name__)
TITLE_WORDS = ("raid lead", "dkp", "pvp", "achievement", "historian", "theorycraft",
               "recruit", "events", "social", "chef")
_HOLDS = frozenset({"WAITING_CALL", "CALLED", "FORMING", "ASSEMBLING", "RUNNING"})
_CALLED = _HOLDS - {"WAITING_CALL"}
_PROVENANCE = "complete_exact_native_scope_and_explicit_pull_receipts"
GUILD_NAME_KEY = "realm:guild_name"
_GUILD_NAME_MAX = 24  # The game's own limit on a guild's name.


class RealmDisabled(RuntimeError):
    pass


class _NoLine(Exception):
    """The live line was given up before the model was asked."""


def _guild_name_ok(name):
    return (type(name) is str and 0 < len(name) <= _GUILD_NAME_MAX and name == name.strip()
            and name.isprintable())


def _messages(messages):
    if type(messages) not in (list, tuple) or not messages or any(
            type(m) is not dict or type(m.get("role")) is not str or type(m.get("content")) is not str
            for m in messages):
        raise ValueError("messages must be a non-empty list of role/content dicts")
    return [dict(m) for m in messages]


class RealmEvidenceUnavailable(RuntimeError):
    """Unknown reservations must block allocation instead of appearing idle."""


@dataclass(frozen=True, slots=True)
class RumorRow:
    id: int
    root: str
    about: int
    canonical: str
    source: int
    created_s: float
    last_told_s: float


@dataclass(frozen=True, slots=True)
class AwardRow:
    id: int
    season: str
    title: str
    guid: int
    value: float


@dataclass(frozen=True, slots=True)
class TrainingPull:
    id: str
    map: int
    instance_id: int
    boss_id: int
    difficulty: int
    size: int
    kind: str
    outcome: str
    started_s: float
    ended_s: float
    members: tuple[int, ...]
    assisted: bool
    damage_basis: str
    healing_basis: str


@dataclass(frozen=True, slots=True)
class BenchmarkComparison:
    boss_id: int
    rival_kill_seconds: float | None
    rival_wipes: int
    guild_farm_kill_seconds: float | None
    pulls: tuple[str, ...]
    baseline_pulls: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BenchmarkPull:
    id: int
    week: str
    raid_key: str
    guild_id: int
    members: tuple[int, ...]
    bosses: tuple[int, ...]
    outcome: str
    kills: int
    wipes: int
    completed_s: float
    pulls: tuple[TrainingPull, ...]
    comparisons: tuple[BenchmarkComparison, ...]


@dataclass(frozen=True, slots=True)
class MemberMeter:
    pull_id: str
    guid: int
    class_: int
    spec_tab: int
    role: str
    ilvl: int
    duration_s: float
    active_ms: int
    damage: int
    damage_boss: int
    healing: int
    overheal: int
    taken: int
    deaths: int
    interrupts: int
    damage_basis: str
    healing_basis: str
    spell_basis: str
    interrupt_basis: str


def _int(value, lo=0, hi=None):
    if type(value) is not int or value < lo or hi is not None and value > hi:
        raise ValueError("invalid integer evidence")
    return value


def _number(value):
    try:
        valid = type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError("invalid numeric evidence")
    return float(value)


def _text(value):
    if type(value) is not str:
        raise ValueError("invalid text evidence")
    return value


def _members(value, *, zero=False):
    if type(value) not in (list, tuple):
        raise ValueError("invalid membership evidence")
    result = tuple(_int(g, 0 if zero else 1, 0xFFFFFFFF) for g in value)
    if len(result) != len(set(result)):
        raise ValueError("duplicate membership evidence")
    return result


def _rumor(row):
    return RumorRow(_int(row["id"], 1), _text(row["root"]), _int(row["about"]),
                    _text(row["canonical"]), _int(row["source"]),
                    _number(row["created"]), _number(row["last_told"]))


def _award(row):
    return AwardRow(_int(row["id"], 1), _text(row["season"]), _text(row["title"]),
                    _int(row["guid"], 1), _number(row["value"]))


def _training_pull(row, members):
    members = _members(members)
    start, end = _int(row["start_ms"]), _int(row["end_ms"])
    if (end <= start or _int(row["members"]) != len(members)
            or type(row["complete"]) is not int or row["complete"] != 1
            or type(row["scoreable"]) is not int or row["scoreable"] != 1
            or row["outcome"] not in {"killed", "wiped"}
            or row["kind"] not in {"his", "offline", "benchmark"}
            or type(row["assisted"]) is not int or row["assisted"] != 0):
        raise ValueError("invalid measured pull evidence")
    map_id, boss = _int(row["map"], 1), _int(row["boss_id"])
    if not is_raid_fight(map_id, boss) or row["size"] not in (10, 25):
        raise ValueError("invalid raid evidence")
    return TrainingPull(_text(row["id"]), map_id, _int(row["instance_id"], 1, 0xFFFFFFFF),
        boss, _int(row["difficulty"], 0, 3), _int(row["size"], 1), row["kind"], row["outcome"],
        start / 1000, end / 1000, members, False, _text(row["damage_basis"]), _text(row["healing_basis"]))


def _member_meter(member, pull):
    if member["pull"] != pull["id"] or member["role"] not in {"tank", "healer", "melee", "ranged"}:
        raise ValueError("invalid meter identity")
    return MemberMeter(_text(member["pull"]), _int(member["guid"], 1, 0xFFFFFFFF),
        _int(member["class"], 1), _int(member["spec_tab"], -1, 2), member["role"], _int(member["ilvl"]),
        (_int(pull["end_ms"]) - _int(pull["start_ms"])) / 1000,
        *(_int(member[k]) for k in ("active_ms", "damage", "damage_boss", "healing", "overheal",
                                    "taken", "deaths", "interrupts")),
        *(_text(pull[k]) for k in ("damage_basis", "healing_basis", "spell_basis", "interrupt_basis")))


def _title_matches(title, word):
    canonical = "chef" if word.casefold() in {"chef", "baker", "cook"} else word.casefold()
    aliases = ("chef", "baker", "cook") if canonical == "chef" else (canonical,)
    return any(re.search(r"(?<!\w)" + re.escape(alias) + r"(?!\w)", title, re.IGNORECASE)
               for alias in aliases)


def title_holders(conn: sqlite3.Connection) -> dict[str, tuple[tuple[int, str], ...]]:
    """Read all readable cached personas, including former guild members."""
    personas = PersonaCache(Store(conn, clock=SystemClock())).all()
    return {word: tuple((g, p.character_name) for g, p in personas.items()
                        if _title_matches(p.play.officer_title, word)) for word in TITLE_WORDS}


def _armory_from_guild_config(cfg):
    from leeroy_mind.personas.roster import RosterError, read_db_info
    try:
        return RealmArmory(read_db_info(cfg.worldserver_conf),
            read_db_info(cfg.worldserver_conf, "WorldDatabaseInfo"),
            read_db_info(cfg.worldserver_conf, "LoginDatabaseInfo"), cfg.mysql_exe)
    except (OSError, ValueError, RosterError, RealmArmoryUnavailable):
        log.warning("realm_armory_configuration_unavailable")
        return None


def armory_from_config(config_path: Path) -> RealmArmory | None:
    from leeroy_mind.config import ConfigError
    from leeroy_mind.guild.config import load_guild_config
    try:
        cfg = load_guild_config(config_path)
    except (OSError, ValueError, ConfigError):
        log.warning("realm_armory_configuration_unavailable")
        return None
    return _armory_from_guild_config(cfg)


@dataclass(frozen=True, slots=True)
class _HumanFightScope:
    run: object
    session: object
    raid_id: int
    human: int
    map: int
    instance: int
    participants: frozenset[int]


class RealmPorts(Protocol):
    conn: sqlite3.Connection
    def now(self) -> float: ...
    def human(self) -> HumanIdentity: ...
    def human_online(self) -> bool: ...
    def human_zone(self) -> int: ...
    def human_grouped(self) -> bool: ...
    def raid_phase(self): ...
    def cast(self) -> list[Member]: ...
    def persona(self, guid: int) -> Persona | None: ...
    def has_persona(self, guid: int) -> bool: ...
    def by_title(self, word: str) -> list[int]: ...
    def officers(self) -> list[int]: ...
    def relationship(self, a: int, b: int) -> Relationship: ...
    def his_logins(self, since: float) -> list[float]: ...
    def set_enabled(self, enabled: bool) -> None: ...
    def observe(self, ev, name: str) -> None: ...
    async def speech_tick(self) -> None: ...
    async def close(self) -> None: ...
    async def send(self, cmd) -> Ack: ...
    def propose(self, guid, channel, instruction, *, kind=TurnKind.ASIDE, to_guid=0, deadline_s=20) -> None: ...
    async def say(self, guid, channel, text, *, to_guid=0, kind=TurnKind.ASIDE,
                  before_transport=None, on_recorded=None) -> bool: ...
    async def remember(self, candidate: MemoryCandidate) -> WriteResult: ...
    async def remember_notable(self, text: str, write): ...
    async def llm_line(self, messages: list[dict], *, deadline_s: float = 20) -> str | None: ...
    def guild_name(self) -> str: ...
    async def refresh_guild_name(self, guild_id: int) -> str: ...
    def realm_dir(self) -> Path: ...
    def note_lately(self, guid: int, what: str, text: str) -> None: ...
    def away(self) -> set[int]: ...
    def open_rumors(self) -> list[RumorRow]: ...
    def encounters(self, night: str) -> list[tuple[int, int]]: ...
    def goals(self, guid: int) -> list[str]: ...
    def awards(self, season: str) -> list[AwardRow]: ...
    def night_window(self, now: float) -> tuple[float, float] | None: ...
    def raid_state(self) -> str: ...
    def raid_holds_all(self) -> bool: ...
    def raid_body_holds(self) -> tuple[BodyHold, ...]: ...
    def offline_body_holds(self) -> tuple[BodyHold, ...]: ...
    def training_body_holds(self) -> tuple[BodyHold, ...]: ...
    def run_body_proof(self, run) -> RunBodyProof | None: ...
    def offline_raid_members(self) -> set[int]: ...
    def offline_window(self) -> tuple[time, time] | None: ...
    def hangout_tonight(self) -> bool: ...
    def raid_called_or_running(self) -> bool: ...
    def raid_difficulty(self, instance_id: int) -> int: ...
    def in_his_raid(self, instance_id: int) -> bool: ...
    def attendance(self, guid: int, since: float) -> float: ...
    def bench_streak(self, guid: int) -> int: ...
    def close_loot_losses(self, guid: int, since: float) -> int: ...
    def wipes_tonight(self, map: int, boss: int) -> int: ...
    def last_fight_s(self) -> int: ...
    def guild_ranks(self) -> dict[str, int]: ...
    def comms_up(self) -> bool: ...
    def training_members(self) -> set[int]: ...
    def benchmark_results(self, since: float) -> list[BenchmarkPull]: ...
    def pull_meters(self, map: int, instance: int, boss: int) -> list[MemberMeter]: ...
    def armory(self) -> RealmArmory | None: ...


class LivePorts(RealmPorts):
    """Callbacks are synchronous and optional; only the service persists receipts.

    on_send(cmd) may reject enrollment before dispatch. on_ack(cmd, ack) runs
    even after disable. on_send_error(cmd, error_type) gets only a safe class
    name. An admitted transport is shielded and never retried; close settles it.
    """
    def __init__(self, app, *, on_send=None, on_ack=None, on_send_error=None):
        self.app, self.clock, self.conn = app, app.clock, app.store.conn
        self.on_send, self.on_ack, self.on_send_error = on_send, on_ack, on_send_error
        self.enabled = False
        self._generation = 0
        self._human = app.human_guid
        self._tasks = set()
        self._settlements = set()
        self._proposals = deque(maxlen=64)
        self._speech_lock = asyncio.Lock()
        self._armory = None
        self._armory_loaded = False
        self._night = None
        self._starts = OrderedDict()
        self._replays = OrderedDict()
        self._wipes = {}
        self._latest = None
        self._ordinal = 0
        self._hold_fallback = False

    def _invalidate(self):
        self._generation += 1
        self._proposals.clear()
        self._starts.clear()
        for task in tuple(self._tasks):
            task.cancel()

    def _sync_human(self):
        if self._human != self.app.human_guid:
            self._human = self.app.human_guid
            self._invalidate()
            self._wipes.clear()
            self._replays.clear()
            self._latest = None

    def set_enabled(self, enabled):
        if type(enabled) is not bool:
            raise TypeError("enabled must be bool")
        self._sync_human()
        if not enabled:
            self.enabled = False
            self._invalidate()
        else:
            self.enabled = True

    def _ticket(self):
        self._sync_human()
        generation, human = self._generation, self._human
        def valid():
            self._sync_human()
            return self.enabled and self._generation == generation and self._human == human
        return valid

    async def _owned(self, operation, valid, *, disabled):
        async def run():
            if not valid():
                return disabled()
            result = await operation()
            if not valid():
                return disabled()
            return result
        task = asyncio.create_task(run())
        self._tasks.add(task)  # Before the first yield, including scheduler admission.
        try:
            return await task
        except asyncio.CancelledError:
            if asyncio.current_task().cancelling() or valid():
                raise
            return disabled()
        finally:
            self._tasks.discard(task)

    @staticmethod
    def _disabled():
        raise RealmDisabled("realm disabled")

    async def send(self, cmd):
        valid = self._ticket()
        if not valid():
            self._disabled()
        async def settle():
            if not valid():
                self._disabled()
            if self.on_send is not None:
                self.on_send(cmd)
            # No await separates enrollment, the final fence and BridgeClient's
            # synchronous writer.write. Enrollment callbacks must be synchronous.
            if not valid():
                if self.on_send_error is not None:
                    self.on_send_error(cmd, "RealmDisabled")
                self._disabled()
            try:
                ack = await self.app.bridge.send(cmd, preserve_id=True)
            except BaseException as exc:
                if self.on_send_error is not None:
                    self.on_send_error(cmd, type(exc).__name__)
                raise
            if self.on_ack is not None:
                self.on_ack(cmd, ack)  # Callback errors are not transport failures.
            return ack
        task = asyncio.create_task(settle())
        self._settlements.add(task)
        # Retrieve errors even when a cancelled caller no longer awaits settlement.
        def settled(done):
            self._settlements.discard(done)
            if not done.cancelled():
                done.exception()
        task.add_done_callback(settled)
        return await asyncio.shield(task)

    def propose(self, guid, channel, instruction, *, kind=TurnKind.ASIDE, to_guid=0, deadline_s=20):
        valid = self._ticket()
        if valid():
            deadline_s = _number(deadline_s)
            if deadline_s <= 0:
                return
            self._proposals.append((valid, Turn(kind=kind, speaker=guid, channel=channel,
                to_guid=to_guid, instruction=instruction, deadline=self.now() + deadline_s, max_lines=1)))

    async def speech_tick(self):
        if self._speech_lock.locked():
            return
        async with self._speech_lock:
            while self._proposals:
                valid, turn = self._proposals.popleft()
                if valid() and turn.deadline > self.now():
                    await self._owned(lambda: self.app.engine.speak(turn,
                        why="source:Realm", still_valid=valid), valid, disabled=lambda: [])
                    return  # One admitted turn per tick.

    async def say(self, guid, channel, text, *, to_guid=0, kind=TurnKind.ASIDE,
                  before_transport=None, on_recorded=None):
        """say_as under this ticket. The exact transport hooks (TurnMeta) pass through only
        when given, so an ordinary say is the call it always was."""
        valid = self._ticket()
        if not valid():
            return False
        hooks = {name: hook for name, hook in (("before_transport", before_transport),
                                               ("on_recorded", on_recorded)) if hook is not None}
        return await self._owned(lambda: self.app.engine.say_as(guid, channel, text,
            kind=kind, to_guid=to_guid, still_valid=valid, **hooks), valid, disabled=lambda: False)

    async def remember(self, candidate):
        valid = self._ticket()
        if not valid():
            self._disabled()
        return await self._owned(lambda: self.app.writer.write(candidate, enforce_limits=False),
                                 valid, disabled=self._disabled)

    async def remember_notable(self, text, write):
        """Embed `text` once, then return write(writer, vector) under this ticket.

        The last validity check and write() run with no await between them, so nothing is
        written once the Realm is disabled. A failed embed gives None: the memory is stored
        without a vector, as the writer stores any memory it cannot embed."""
        valid = self._ticket()
        if not valid():
            self._disabled()

        async def operation():
            try:
                vector = (await self.app.embedder.embed([text], Priority.EXTRACTION))[0]
            except (LLMError, SchedulerError):
                vector = None
            if not valid():
                self._disabled()
            return write(self.app.writer, vector)
        return await self._owned(operation, valid, disabled=self._disabled)

    def _may_speak(self):
        """The live model may be asked only while he is online and its latch is up."""
        return self.human_online() is True and getattr(self.app, "model_ready", False) is True

    async def llm_line(self, messages, *, deadline_s=20):
        """One line from the live model for a light mind, or None.

        A BACKGROUND job under this ticket: the LIVE preset, think=False and the configured live
        model. With him offline or the model latch down nothing is asked, and both are checked
        again once the job is admitted. A failure, a missed deadline, an empty answer or a
        disable gives None; a line finished after its deadline is dropped too.
        """
        messages = _messages(messages)
        deadline_s = _number(deadline_s)
        valid = self._ticket()
        if not valid() or deadline_s <= 0 or not self._may_speak():
            return None
        scheduler, deadline = self.app.scheduler, self.now() + deadline_s

        async def job(llm):
            admission = getattr(self.app.engine, "live_admission", None)
            if admission is not None:
                await admission()  # The night hands the live model back first.
            if not valid() or not self._may_speak() or scheduler.expired(deadline):
                raise _NoLine()
            return await llm.chat(self.app.cfg.models.live, messages, options=dict(LIVE.options),
                                  think=False, timeout=LIVE.timeout)

        async def operation():
            try:
                result = await scheduler.submit(Priority.BACKGROUND, job, deadline=deadline)
            except (LLMError, SchedulerError, _NoLine):
                return None
            if scheduler.expired(deadline) or type(result.text) is not str:
                return None
            return result.text.strip() or None
        return await self._owned(operation, valid, disabled=lambda: None)

    def guild_name(self):
        """The guild's own name from the meta cache, or "" when none has been read yet."""
        try:
            cached = self._meta(GUILD_NAME_KEY)
        except (ValueError, TypeError):
            return ""
        name = cached.get("name") if type(cached) is dict else None
        return name if _guild_name_ok(name) else ""

    async def refresh_guild_name(self, guild_id):
        """Read the guild's name through the armory on a worker thread and cache it in meta.

        A missing armory, a failed or malformed read, or a disable keeps the cached name,
        which is returned either way.
        """
        _int(guild_id, 1, 0xFFFFFFFF)
        valid = self._ticket()
        armory = self.armory() if valid() else None
        read = getattr(armory, "guild_name", None)
        if read is None:
            return self.guild_name()

        def query():
            try:
                return read(guild_id)
            except Exception:  # The armory's own errors carry no configuration or data.
                return None

        async def operation():
            name = await asyncio.to_thread(query)
            if not _guild_name_ok(name):
                log.warning("realm_guild_name_unavailable")
                return self.guild_name()
            if valid() and name != self.guild_name():
                self.app.store.set_meta(GUILD_NAME_KEY, json.dumps(
                    {"guild": guild_id, "name": name, "at": self.now()}, ensure_ascii=False))
            return self.guild_name()
        return await self._owned(operation, valid, disabled=self.guild_name)

    def realm_dir(self):
        """mind-data/realm: the blocklist, cards, plans and approvals. Nothing is created."""
        return Path(self.app.data_dir) / "realm"

    async def close(self):
        self.set_enabled(False)
        tasks = tuple(self._tasks | self._settlements)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def now(self):
        return self.clock.now()

    def human(self):
        self._sync_human()
        return human_identity(self.app.cfg, self.app.store, self._human)

    def human_online(self):
        return bool(self.app.world.human_online)

    def human_zone(self):
        return self.app.world.human_zone

    def human_grouped(self):
        return self.app.world.in_group(self.app.human_guid)

    def raid_phase(self):
        return self.app.raid.phase(self.now())

    def cast(self):
        return [m for m in self.app.store.cast().values()
                if m.guid != self.app.human_guid and m.level >= 80 and self.has_persona(m.guid)]

    def persona(self, guid):
        return self.app.personas.get(guid)

    def has_persona(self, guid):
        return self.persona(guid) is not None

    def by_title(self, word):
        return [m.guid for m in self.cast() if _title_matches(self.persona(m.guid).play.officer_title, word)]

    def officers(self):
        return [m.guid for m in self.cast() if self.persona(m.guid).play.guild_role == "officer"]

    def relationship(self, a, b):
        return self.app.store.get_relationship(a, b)

    def his_logins(self, since):
        return [_number(row[0]) for row in self.conn.execute(
            "SELECT started FROM session WHERE started>=? ORDER BY started,id", (_number(since),))]

    def note_lately(self, guid, what, text):
        """Task21 supplies notes; Task10 deliberately has no note effect."""

    def _tables(self, *names):
        return all(self.conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
                   is not None for name in names)

    def _meta(self, key):
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return None if row is None else json.loads(row[0])

    def away(self):
        return ({_int(row[0], 1) for row in self.conn.execute("SELECT guid FROM away WHERE returned_at IS NULL")}
                if self._tables("away") else set())

    def open_rumors(self):
        return ([_rumor(row) for row in self.conn.execute("SELECT * FROM rumor WHERE status='live' ORDER BY id")]
                if self._tables("rumor") else [])

    def encounters(self, night):
        return ([(_int(row[0], 1), _int(row[1], 1)) for row in self.conn.execute(
            "SELECT a,b FROM encounter WHERE night=? AND status='done' AND private=0 ORDER BY idx,id", (night,))]
                if self._tables("encounter") else [])

    def goals(self, guid):
        return list(self.app.store.get_state(guid).goals)

    def awards(self, season):
        return ([_award(row) for row in self.conn.execute(
            "SELECT * FROM award WHERE season=? ORDER BY title,guid,id", (season,))]
                if self._tables("award") else [])

    def night_window(self, now):
        if not self._tables("night_run"):
            return None
        from leeroy_mind.nightly.settings import night_bounds, night_of_ts
        tz = self.clock.local().tzinfo
        return night_bounds(night_of_ts(_number(now), tz), tz)

    def _guild(self):
        return getattr(self.app, "guild", None)

    def _raid_night(self):
        return getattr(self._guild(), "raid_night", None)

    def raid_state(self):
        controller = self._raid_night()
        return controller.state.name if controller is not None else ""

    def raid_holds_all(self):
        return self.raid_state() in _HOLDS

    def raid_called_or_running(self):
        return self.raid_state() in _CALLED

    def offline_window(self):
        guild = self._guild()
        return tuple(time.fromisoformat(t) for t in guild.cfg.offline_window) if guild is not None else None

    def hangout_tonight(self):
        return self._meta("guild:raid-night:quiet-night") == night_of(self.clock.local())

    def guild_ranks(self):
        guild = self._guild()
        return dict(guild.cfg.ranks) if guild is not None else {}

    def comms_up(self):
        comms = getattr(getattr(self.app, "voice", None), "comms", None)
        return bool(comms.available()) if comms is not None else False

    def training_members(self):
        training = getattr(self.app, "training", None)
        return set(training.training_members()) if training is not None else set()

    def _his_group(self):
        """Everyone in his current group or raid but him, cast or not (addclass fill-ins too)."""
        group = self.app.world.group
        if group is None:
            return set()
        if type(group.members) is not tuple or type(group.leader_guid) is not int:
            raise ValueError("invalid group evidence")
        guids = {_int(g, 0, 0xFFFFFFFF) for g in (*group.members, group.leader_guid)}
        return guids - {0, self.app.human_guid}

    def _scheduling_hold(self, state, members):
        """The raid's hold from his login to the raid night's end (owner ruling, 2026-11-04).

        Until the raid is formed, his whole guild roster and his group; once formed, the raid's
        own members (its picks, late arrivals among them) and his group. Nobody else, so rival
        actors can still move. While the Realm is disabled, or when a member list cannot be
        read, it is today's hold of every body (fail closed), logged once per fallback.
        """
        everyone = BodyHold("raid:scheduling", "raid", 0, ALL, False)
        if not self.enabled:
            return everyone
        try:
            guids = self._his_group()
            if state in {"ASSEMBLING", "RUNNING"}:
                if members is None:
                    raise ValueError("raid members unavailable")
                guids |= set(members)
            else:
                roster = {_int(row[0], 1, 0xFFFFFFFF) for row in self.conn.execute(
                    "SELECT guid FROM cast_member")} - {self.app.human_guid}
                if not roster:
                    raise ValueError("guild roster unavailable")
                guids |= roster
            hold = BodyHold("raid:scheduling", "raid", 0, frozenset(guids), False)
        except Exception:
            if not self._hold_fallback:
                self._hold_fallback = True
                log.warning("realm_raid_hold_fallback")
            return everyone
        self._hold_fallback = False
        return hold

    def raid_body_holds(self):
        """Scheduling intent and exact restoration reservations are distinct."""
        from leeroy_mind.training.body_holds import session_evidence
        controller = self._raid_night()
        if controller is None:
            # A persisted open online session cannot silently become idle just
            # because its controller was not installed/recovered.
            if self._tables("guild_raid") and self.conn.execute(
                    "SELECT 1 FROM guild_raid WHERE ended IS NULL AND offline=0 LIMIT 1").fetchone():
                raise RealmEvidenceUnavailable("raid body evidence unavailable")
            return ()
        try:
            state = self.raid_state()
            holds = []
            sessions = []
            current = controller.session
            if current is not None:
                sessions.append(current)
            for session, run, outcome, events, stop_dc in controller._closing:
                if all(session is not s for s in sessions):
                    sessions.append(session)
            scopes, members = set(), None
            for session in sessions:
                scope, guids, _ = session_evidence(self.conn, session, self.app.human_guid)
                if session.plan.offline or scope in scopes:
                    raise ValueError("ambiguous raid scope")
                scopes.add(scope)
                holds.append(BodyHold(scope, "raid", 0, guids, True))
                if session is current:
                    members = guids
            known_ids = {s.raid_id for s in sessions}
            if any(row[0] not in known_ids for row in self.conn.execute(
                    "SELECT raid_id FROM guild_raid WHERE ended IS NULL AND offline=0")):
                raise ValueError("unowned raid session")
            if state in _HOLDS:
                holds.insert(0, self._scheduling_hold(state, members))
            return tuple(holds)
        except Exception:
            raise RealmEvidenceUnavailable("raid body evidence unavailable") from None

    def _offline_owned(self):
        offline = getattr(self._guild(), "offline", None)
        return offline._owned if offline is not None else self._meta("guild:offline:owned")

    def offline_body_holds(self):
        try:
            hold = self._offline_body_hold()
            return (hold,) if hold is not None else ()
        except Exception:
            raise RealmEvidenceUnavailable("offline body evidence unavailable") from None

    def _offline_body_hold(self):
        """Shared source read, never another registered provider callback."""
        owned = self._offline_owned()
        if owned is None:
            return None
        from leeroy_mind.training.body_holds import identity
        scope = identity(owned["id"])
        if owned.get("phase") not in {"acquiring", "awaiting_ack", "accepted", "uncertain",
                                       "complete", "rejected"}:
            raise ValueError("invalid offline phase")
        guids = frozenset(self.offline_raid_members())
        return BodyHold(scope, "offline_raid", 1, guids, True) if guids else None

    def _corroborate_training_journals(self, training):
        from leeroy_mind.training.body_holds import session_evidence, validate_journals
        if training.ports.conn is not self.conn or training.ports.human_guid != self.app.human_guid:
            raise ValueError("unowned training journals")
        for evidence in validate_journals(training):
            run, session = evidence.run, evidence.run.session
            if evidence.owner == "offline_raid":
                offline = getattr(self._guild(), "offline", None)
                owned = self._offline_owned()
                persisted = self._meta("guild:offline:owned")
                hold = self._offline_body_hold()
                if (offline is None or offline.run is not run or offline.session is not session
                        or type(owned) is not dict or type(owned.get("raid_id")) is not int
                        or owned.get("raid_id") != session.raid_id
                        or owned.get("phase") not in {"awaiting_ack", "accepted", "uncertain"}
                        or type(persisted) is not dict or persisted.get("id") != evidence.scope_id
                        or type(persisted.get("raid_id")) is not int or persisted.get("raid_id") != session.raid_id
                        or persisted.get("phase") not in {"awaiting_ack", "accepted", "uncertain"}
                        or hold is None or hold.scope_id != evidence.scope_id
                        or not evidence.members <= hold.guids):
                    raise ValueError("unowned training journals")
            else:
                raid = self._raid_night()
                current = (raid is not None and raid.run is run and raid.session is session
                           and self.raid_state() in _HOLDS)
                closing = (raid is not None and any(s is session and r is run for s, r, *_ in raid._closing))
                scope, guids, _ = session_evidence(self.conn, session, self.app.human_guid)
                if not (current or closing) or scope != evidence.scope_id or not evidence.members <= guids:
                    raise ValueError("unowned training journals")

    def training_body_holds(self):
        training = getattr(self.app, "training", None)
        # Disabled/unattached Training has no controller able to validate an
        # unresolved journal or operation. It cannot certify an empty source.
        try:
            if training is not None:
                holds = tuple(training.body_holds())
                self._corroborate_training_journals(training)
                return holds
            if self._tables("tg_applied") and self.conn.execute(
                    "SELECT 1 FROM tg_applied WHERE undone IS NULL LIMIT 1").fetchone():
                raise ValueError("unowned training journal")
            for key in ("tg:lab:owned", "tg:benchmark:active"):
                owned = self._meta(key)
                if owned is not None:
                    if key != "tg:lab:owned" or type(owned) is not dict or owned.get("phase") != "complete":
                        raise ValueError("unowned training operation")
                    from leeroy_mind.training.body_holds import validate_lab_complete
                    validate_lab_complete(self.conn, owned)
            return ()
        except Exception:
            raise RealmEvidenceUnavailable("training body evidence unavailable") from None

    def run_body_proof(self, run):
        """Resolve a real current/retained run without calling Training.state_for.

        This supplies local identity only. BodyLedger checks source coverage and
        conflicts; Training still checks its native offline binding and fences.
        """
        from leeroy_mind.training.body_holds import identity, session_evidence
        training = getattr(self.app, "training", None)
        if training is None or run is None:
            return None
        try:
            epoch = training.ports.connection_epoch
            if (training._connected is not True or training._presence_known is not True
                    or training._stopping is not False or type(epoch) is not int
                    or not 0 < epoch <= 0xFFFFFFFFFFFFFFFF or training._epoch != epoch
                    or training.ports.human_guid != self.app.human_guid
                    or training.ports.conn is not self.conn):
                return None
            listener = training.listener
            key = listener.run_key(run)
            retained = listener._ending.get(key)
            if (retained is not None and retained is not run
                    or not (listener.run is run or retained is run)):
                return None
            session = run.session
            session_key, guids, instance = session_evidence(
                self.conn, session, self.app.human_guid, run=run, proof=True)
            if run.plan.offline:
                controller = getattr(self._guild(), "offline", None)
                owned = self._offline_owned()
                if (controller is None or controller.run is not run or controller.session is not session
                        or type(owned) is not dict or owned.get("raid_id") != session.raid_id
                        or owned.get("phase") not in {"awaiting_ack", "accepted", "uncertain"}):
                    return None
                owner, scope = "offline_raid", identity(owned["id"])
            else:
                controller = self._raid_night()
                if controller is None:
                    return None
                current = (controller.run is run and controller.session is session
                           and self.raid_state() in _HOLDS)
                closing = any(s is session and r is run for s, r, *_ in controller._closing)
                if not (current or closing):
                    return None
                owner, scope = "raid", session_key
            if training.ports.connection_epoch != epoch or training._epoch != epoch:
                return None
            return RunBodyProof(run, session, owner, scope, session_key, epoch, instance, guids)
        except Exception:
            return None

    def armory(self):
        if not self._armory_loaded:
            guild = self._guild()
            self._armory = _armory_from_guild_config(guild.cfg) if guild is not None else None
            self._armory_loaded = True
        return self._armory


    def offline_raid_members(self):
        offline = getattr(self._guild(), "offline", None)
        try:
            owned = offline._owned if offline is not None else self._meta("guild:offline:owned")
        except (ValueError, TypeError):
            raise RealmEvidenceUnavailable("offline reservations unavailable") from None
        if owned is None:
            return set()
        if not isinstance(owned, dict):
            raise RealmEvidenceUnavailable("offline reservations unavailable")
        try:
            planned = set(_members(owned["plan"]["members"]))
        except (KeyError, TypeError, ValueError):
            raise RealmEvidenceUnavailable("offline reservations unavailable") from None
        retained = set()
        if offline is not None and type(owned.get("raid_id")) is int and owned["raid_id"] > 0:
            for session in (getattr(offline, "session", None),
                            getattr(getattr(offline, "run", None), "session", None)):
                if session is not None and session.raid_id == owned["raid_id"]:
                    try:
                        retained.update(_members(session.plan.members))
                    except ValueError:
                        raise RealmEvidenceUnavailable("offline reservations unavailable") from None
        try:
            accepted = None if owned.get("accepted") is None else set(_members(owned["accepted"]))
            if accepted is not None and not accepted <= planned:
                raise ValueError("invalid accepted scope")
        except (TypeError, ValueError):
            log.warning("realm_offline_reservations_corrupt")
            return planned | retained
        if type(owned.get("phase")) is not str:
            log.warning("realm_offline_reservations_corrupt")
            return planned | retained
        if owned["phase"] in ("complete", "rejected"):
            return set()
        return (planned if accepted is None else accepted) | retained

    def _open(self, session):
        if session is None or not self._tables("guild_raid"):
            return False
        row = self.conn.execute("SELECT ended FROM guild_raid WHERE raid_id=?", (session.raid_id,)).fetchone()
        return row is not None and row[0] is None

    def _current_run(self):
        controller = self._raid_night()
        return (controller.run if controller is not None and self.raid_state() in {"ASSEMBLING", "RUNNING"}
                else None)

    def raid_difficulty(self, instance_id):
        if type(instance_id) is not int or not 0 < instance_id <= 0xFFFFFFFF:
            return -1
        guild = self._guild()
        sessions = (getattr(self._raid_night(), "session", None),
                    getattr(getattr(guild, "offline", None), "session", None))
        modes = set()
        for session in sessions:
            if self._open(session) and type(session.ctx.instance_id) is int and session.ctx.instance_id == instance_id:
                plan = session.plan
                if type(plan.size) is not int or plan.size not in (10, 25) or type(plan.heroic) is not bool:
                    return -1
                modes.add(int(plan.size == 25) + 2 * int(plan.heroic))
        return modes.pop() if len(modes) == 1 else -1

    def in_his_raid(self, instance_id):
        run = self._current_run()
        return (type(instance_id) is int and 0 < instance_id <= 0xFFFFFFFF and run is not None
                and run.human_in_raid is True and run.plan.human_in_raid is True
                and run.plan.offline is False and self._open(run.session)
                and type(run.session.ctx.instance_id) is int and run.session.ctx.instance_id == instance_id)

    def attendance(self, guid, since):
        if not self._tables("guild_raid", "guild_attendance"):
            return 0.0
        numerator, denominator = self.conn.execute(
            "SELECT COALESCE(SUM(EXISTS(SELECT 1 FROM guild_attendance a WHERE a.raid_id=r.raid_id "
            "AND a.guid=? AND a.status='raided')),0),COUNT(*) FROM guild_raid r "
            "WHERE r.offline=0 AND r.ended IS NOT NULL AND r.started>=? AND r.started<=? "
            "AND EXISTS(SELECT 1 FROM guild_attendance h WHERE h.raid_id=r.raid_id AND h.guid=? AND h.status='raided')",
            (guid, _number(since), self.now(), self.app.human_guid)).fetchone()
        return numerator / denominator if denominator else 0.0

    def bench_streak(self, guid):
        if not self._tables("guild_raid", "guild_attendance"):
            return 0
        from leeroy_mind.guild.store import bench_streaks
        return bench_streaks(self.conn, [guid])[guid]

    def close_loot_losses(self, guid, since):
        if not self._tables("dkp_ledger"):
            return 0
        since, now, count = _number(since), self.now(), 0
        # Called once per member (realm.poach): a record that cannot be read is counted once per
        # process, in one count line from the call that first finds it, never once per member.
        reported, unreadable = self.__dict__.setdefault("_loot_evidence_reported", set()), 0
        for key, value in self.conn.execute("SELECT key,value FROM meta WHERE key GLOB 'guild:loot:*' ORDER BY key"):
            match = re.fullmatch(r"guild:loot:([0-9]+):[a-f0-9]+", key)
            if match is None:
                continue
            try:
                rec = json.loads(value)
                # Only a charged award can be a close loss; unclaimed or uncertain loot has none.
                if rec["state"] != "charged":
                    continue
                award = rec["award"]
                winner, runner = _int(award["guid"], 1), _int(award["runner_up"], 1)
                item, paid = _int(rec["item_id"], 1), _number(award["paid"])
                if award["close"] is not True or runner != guid or winner == runner or paid < 0:
                    continue
                debits = self.conn.execute("SELECT ts,delta FROM dkp_ledger WHERE reason='item' AND note=? "
                    "AND raid_id=? AND item_id=? AND guid=?", (key, int(match[1]), item, winner)).fetchall()
                if (len(debits) == 1 and _number(debits[0][1]) == -paid
                        and since <= _number(debits[0][0]) <= now):
                    count += 1
            except (ValueError, TypeError, KeyError):
                if key not in reported:
                    reported.add(key)
                    unreadable += 1
        if unreadable:
            log.warning("realm_loot_evidence_unavailable: records=%d", unreadable)
        return count

    def _roll_night(self):
        self._sync_human()
        current = night_of(self.clock.local())
        if current != self._night:
            self._night = current
            self._starts.clear()
            self._replays.clear()
            self._wipes.clear()
            self._latest = None

    def _scope(self, run, ev, *, retained=False):
        if run is None:
            return None
        session, plan = run.session, run.plan
        if (run.human_in_raid is not True or plan.human_in_raid is not True or plan.offline is not False
                or not retained and not self._open(session)):
            return None
        ctx = session.ctx
        try:
            participants = frozenset(_members(plan.members)) | {_int(self.app.human_guid, 1, 0xFFFFFFFF)}
            at = _number(ev.ts) / 1000 if ev.ts > 0 else self.now()
            if (type(ctx.instance_id) is not int or ctx.instance_id <= 0
                    or type(ev.instance_id) is not int or ev.instance_id != ctx.instance_id
                    or ev.map != ctx.map_id or not is_raid_fight(ev.map, ev.boss_id)
                    or not participants.intersection(_members(ev.members))
                    or at < _number(session.started) - 5):
                return None
            return _HumanFightScope(run, session, _int(session.raid_id, 1), self.app.human_guid,
                                    ctx.map_id, ctx.instance_id, participants)
        except (ValueError, TypeError):
            return None

    def observe(self, ev, name):
        self._roll_night()
        if not self.enabled or name != "boss" or ev.state not in {"engaged", "killed", "wiped"}:
            return
        current = self._current_run()
        scope = self._scope(current, ev)
        if scope is None and ev.state != "engaged":
            controller = self._raid_night()
            for session, run, outcome, events, stop_dc in getattr(controller, "_closing", ()):
                # Exact queued object, exact retained run. No guessed end cutoff.
                if (run is not None and run.session is session
                        and any(r is run and n == "boss" and queued is ev for r, n, queued in events)):
                    scope = self._scope(run, ev, retained=True)
                    if scope is not None:
                        break
        if scope is None:
            return
        at = _number(ev.ts) / 1000 if ev.ts > 0 else self.now()
        try:
            event_night = night_of(datetime.fromtimestamp(at, self.clock.local().tzinfo))
        except (ValueError, OverflowError, OSError):
            return
        if event_night != self._night:
            return
        triple = (scope.map, scope.instance, ev.boss_id)
        key = (id(scope.run), id(scope.session), scope.raid_id, *triple)
        replay = (*key, ev.state, ev.ts)
        if replay in self._replays:
            return
        self._replays[replay] = None
        while len(self._replays) > 512:
            self._replays.popitem(last=False)
        if ev.state == "engaged":
            self._starts[key] = (scope, at)
            while len(self._starts) > 64:
                self._starts.popitem(last=False)
            return
        started = self._starts.pop(key, None)
        duration = 0
        if (started is not None and started[0] == scope and at >= started[1]):
            duration = int(at - started[1])
        self._ordinal += 1
        latest = (at, self._ordinal, duration)
        if self._latest is None or latest[:2] > self._latest[:2]:
            self._latest = latest
        if ev.state == "wiped":
            boss_key = (scope.map, ev.boss_id)
            self._wipes[boss_key] = self._wipes.get(boss_key, 0) + 1

    def wipes_tonight(self, map, boss):
        self._roll_night()
        return self._wipes.get((map, boss), 0)

    def last_fight_s(self):
        self._roll_night()
        return self._latest[2] if self._latest is not None else 0


    def pull_meters(self, map, instance, boss):
        _int(map, 1, 0xFFFFFFFF)
        _int(instance, 1, 0xFFFFFFFF)
        _int(boss, 0, 0xFFFFFFFF)
        if not self._tables("tg_pull", "tg_member"):
            return []
        row = self.conn.execute("SELECT * FROM tg_pull WHERE map=? AND instance_id=? AND boss_id=? "
            "AND complete=1 AND scoreable=1 AND outcome IN ('killed','wiped') AND end_ms>start_ms "
            "AND end_ms<=? AND assisted=0 AND kind IN ('his','offline') "
            "ORDER BY end_ms DESC,start_ms DESC,id DESC LIMIT 1", (map, instance, boss, int(self.now() * 1000))).fetchone()
        if row is None:
            return []
        from leeroy_mind.training.store import member_rows
        members = member_rows(self.conn, row["id"])
        try:
            _training_pull(row, [m["guid"] for m in members])
            return [_member_meter(m, row) for m in members]
        except (ValueError, TypeError, KeyError):
            log.warning("realm_meter_evidence_unavailable")
            return []

    def benchmark_results(self, since):
        if not self._tables("tg_benchmark"):
            return []
        result = []
        for row in self.conn.execute("SELECT * FROM tg_benchmark WHERE ts>=? AND ts<=? ORDER BY ts,id",
                                     (_number(since), self.now())):
            try:
                normalized = _benchmark(row, self._meta("tg:benchmark:result:" + row["week"]))
            except (ValueError, TypeError):
                normalized = None
            if normalized is not None:
                result.append(normalized)
            else:
                log.warning("realm_benchmark_evidence_unavailable")
        return result


def _benchmark(row, payload):
    """Read the immutable publication, never rejoin later mutable pull rows."""
    try:
        if type(payload) is not dict or payload["provenance"] != _PROVENANCE:
            return None
        members, bosses = _members(json.loads(row["members"])), _members(json.loads(row["bosses"]), zero=True)
        summary = dict(week=_text(row["week"]), raid_key=_text(row["raid_key"]), guild_id=_int(row["guild_id"], 1),
            members=list(members), bosses=list(bosses), outcome=_text(row["outcome"]),
            kills=_int(row["kills"]), wipes=_int(row["wipes"]), ts=_number(row["ts"]))
        # JSON comparison distinguishes bools from integers as well as nested values.
        if json.dumps(payload["result"], sort_keys=True, allow_nan=False) != json.dumps(summary, sort_keys=True, allow_nan=False):
            # SQLite REAL may normalize integral epoch seconds to float.
            supplied = dict(payload["result"])
            supplied["ts"] = _number(supplied["ts"])
            if json.dumps(supplied, sort_keys=True, allow_nan=False) != json.dumps(summary, sort_keys=True, allow_nan=False):
                return None
        receipt = payload["receipt"]
        from leeroy_mind.training.protocol import TrainingCleanup
        proof_dict = receipt["proof"]
        proof = TrainingCleanup(**(proof_dict | {"members": tuple(proof_dict["members"])}))
        if (proof.state != "complete" or receipt["query_receipt"] != proof_dict
                or receipt["query"]["id"] != proof.id or proof.training_run != receipt["handle"]
                or proof.job_id != receipt["command_id"] or proof.instance_id != receipt["instance"]
                or proof.map_id != receipt["map"] or proof.leader_guid != receipt["leader"]
                or proof.guild_id != receipt["cast_guild"] or proof.human_guid != receipt["human"]
                or set(proof.members) != set(members) or _members(receipt["members"]) != members
                or _members(receipt["bosses"], zero=True) != bosses
                or receipt["week"] != summary["week"] or receipt["raid_key"] != summary["raid_key"]
                or receipt["guild_id"] != summary["guild_id"] or receipt["conflict"] is not False
                or type(receipt["heroic"]) is not bool or type(receipt["size"]) is not int
                or receipt["size"] not in (10, 25)):
            return None
        binding = {k: v for k, v in proof_dict.items() if k not in {"id", "ts", "state"}}
        binding["members"] = sorted(proof.members)
        if binding != receipt["binding"]:
            return None
        start = _number(receipt["started"])
        if (not 0 <= start <= summary["ts"] or proof.ts < int(start * 1000)
                or proof.ts > int(summary["ts"] * 1000) + 15000):
            return None
        rows = payload["admitted_pulls"]
        if type(rows) is not list or len(rows) > 256:
            return None
        pulls = tuple(_training_pull(r, r["member_guids"]) for r in rows)
        if len({p.id for p in pulls}) != len(pulls):
            return None
        for pull in pulls:
            if (pull.kind != "benchmark" or pull.id not in receipt["pulls"]
                    or pull.map != proof.map_id or pull.instance_id != proof.instance_id
                    or pull.size != receipt["size"] or pull.difficulty != int(pull.size == 25) + 2 * receipt["heroic"]
                    or set(pull.members) != set(members) or pull.boss_id not in bosses
                    or not start <= pull.started_s < pull.ended_s <= proof.ts / 1000):
                return None
        if summary["kills"] != sum(p.outcome == "killed" for p in pulls) or summary["wipes"] != sum(p.outcome == "wiped" for p in pulls):
            return None
        baselines, records = payload["baselines"], payload["comparisons"]
        if (type(baselines) is not dict or type(records) is not list
                or set(baselines) != {str(p.boss_id) for p in pulls}
                or any(type(values) is not list or any(type(r) is not dict for r in values)
                       for values in baselines.values())):
            return None
        comparisons = []
        for boss in bosses:
            observed = [r for r in rows if r["boss_id"] == boss]
            if not observed:
                continue
            if len(comparisons) >= len(records):
                return None
            rec, guild = records[len(comparisons)], baselines[str(boss)]
            if (type(rec) is not dict or type(rec["pulls"]) is not list
                    or type(rec["baseline_pulls"]) is not list or _int(rec["boss_id"]) != boss):
                return None
            for baseline in guild:
                if (baseline["kind"] not in {"his", "offline"} or baseline["map"] != proof.map_id
                        or baseline["boss_id"] != boss or baseline["size"] != receipt["size"]
                        or baseline["difficulty"] != int(receipt["size"] == 25) + 2 * receipt["heroic"]
                        or _int(baseline["complete"], 1, 1) != 1 or _int(baseline["scoreable"], 1, 1) != 1
                        or _int(baseline["assisted"], 0, 0) != 0 or baseline["outcome"] not in {"killed", "wiped"}
                        or not 0 <= _int(baseline["start_ms"]) < _int(baseline["end_ms"]) <= int(start * 1000)
                        or not 0 <= _number(baseline["received"]) <= start):
                    return None
            # Match benchmark._comparisons from the sealed inputs only. Farm
            # uses the last ten outcomes, three final kills and >=80% kills.
            recent = [r["outcome"] for r in guild[-10:]]
            farm = len(recent) >= 3 and recent[-3:] == ["killed"] * 3 and recent.count("killed") / len(recent) >= 0.8
            rival_times = [(r["end_ms"] - r["start_ms"]) / 1000 for r in observed if r["outcome"] == "killed"]
            guild_times = [(r["end_ms"] - r["start_ms"]) / 1000 for r in guild if r["outcome"] == "killed"]
            expected_times = (statistics.median(rival_times) if rival_times else None,
                              statistics.median(guild_times) if farm and guild_times else None)
            times = tuple(None if rec[k] is None else _number(rec[k])
                          for k in ("rival_kill_seconds", "guild_farm_kill_seconds"))
            ids = tuple(_text(i) for i in rec["pulls"])
            baseline_ids = tuple(_text(i) for i in rec["baseline_pulls"])
            expected_ids = tuple(_text(r["id"]) for r in guild) if farm else ()
            wipes = sum(r["outcome"] == "wiped" for r in observed)
            if (times != expected_times or ids != tuple(r["id"] for r in observed)
                    or baseline_ids != expected_ids or _int(rec["rival_wipes"]) != wipes):
                return None
            comparisons.append(BenchmarkComparison(boss, times[0], wipes, times[1], ids, baseline_ids))
        if len(comparisons) != len(records):
            return None
        return BenchmarkPull(_int(row["id"], 1), summary["week"], summary["raid_key"], summary["guild_id"],
            members, bosses, summary["outcome"], summary["kills"], summary["wipes"], summary["ts"], pulls, tuple(comparisons))
    except (ValueError, TypeError, KeyError, OverflowError):
        return None
