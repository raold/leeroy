"""The narrow guild-life boundary; no socket, task or SQL client starts on construction."""

import asyncio
from collections import OrderedDict
import logging
import sqlite3
import threading
from typing import Protocol

from leeroy_mind.clock import Clock
from leeroy_mind.human import HumanIdentity
from leeroy_mind.memory.writer import MemoryCandidate
from leeroy_mind.personas.schema import Persona
from leeroy_mind.protocol import Ack, Member
from leeroy_mind.storage.rows import PersonaStateRow, Relationship
from leeroy_mind.talk.turns import TurnKind

from .protocol import Job, StopOfflineRaid


log = logging.getLogger(__name__)


class JobWaiter:
    """Bounded terminal mailbox, armed before dispatch (a job can beat its ack)."""

    def __init__(self, *, capacity=512):
        self.capacity = capacity
        self._jobs = OrderedDict()
        self._wake = {}

    def expect(self, cmd_id):
        if cmd_id in self._jobs:
            raise ValueError("job identity already registered")
        if len(self._jobs) >= self.capacity:
            old = next((key for key in self._jobs if key not in self._wake), None)
            if old is None:
                raise RuntimeError("job mailbox is full")
            self._jobs.pop(old)
        self._jobs[cmd_id] = None

    def feed(self, ev):
        if ev.id not in self._jobs or ev.state not in {"done", "failed"}:
            return
        if self._jobs[ev.id] is None:
            self._jobs[ev.id] = ev
        wake = self._wake.get(ev.id)
        if wake is not None:
            wake.set()

    async def wait(self, cmd_id, timeout_s):
        if cmd_id not in self._jobs:
            return None
        if self._jobs[cmd_id] is not None:
            return self._jobs.pop(cmd_id)
        if cmd_id in self._wake:
            raise ValueError("job already has a waiter")
        wake = self._wake[cmd_id] = asyncio.Event()
        try:
            await asyncio.wait_for(wake.wait(), timeout_s)
            return self._jobs.pop(cmd_id, None)
        except TimeoutError:
            return None
        finally:
            self._wake.pop(cmd_id, None)
            self._jobs.pop(cmd_id, None)

    def close(self):
        for wake in self._wake.values():
            wake.set()
        self._jobs.clear()


class LazyArmory:
    """Read-only game data. Every method is synchronous and runs in a worker thread."""

    def __init__(self, cfg):
        self.cfg = cfg
        self._backend = None
        self._lock = threading.RLock()
        self.available = True
        self._warned = False
        if not cfg.worldserver_conf.is_file():
            self.unavailable()

    def unavailable(self):
        self.available = False
        if not self._warned:
            log.warning("guild_armory_unavailable")
            self._warned = True

    def _get(self):
        from .armory import MysqlArmory
        from leeroy_mind.personas.roster import read_db_info, RosterError
        if not self.available:
            raise RosterError("guild armory unavailable")
        if self._backend is None:
            try:
                self._backend = MysqlArmory(read_db_info(self.cfg.worldserver_conf),
                    read_db_info(self.cfg.worldserver_conf, "WorldDatabaseInfo"), self.cfg.mysql_exe)
            except (OSError, ValueError, RosterError):
                self.unavailable()
                raise RosterError("guild armory unavailable") from None
        return self._backend

    def _call(self, name, *args):
        with self._lock:
            return getattr(self._get(), name)(*args)

    def items(self, ids):
        return self._call("items", ids)

    def equipped(self, guids):
        return self._call("equipped", guids)

    def human_tier(self, guid):
        return self._call("human_tier", guid)

    def name(self, guid):
        return self._call("name", guid)

    def bound_with(self, owner, map_id, guids):
        return self._call("bound_with", owner, map_id, guids)

    def saved_sizes(self, owner, map_id):
        return self._call("saved_sizes", owner, map_id)

    def roster(self, guild_id):
        from leeroy_mind.personas.roster import query_roster
        with self._lock:
            backend = self._get()
            return {m.guid: m for m in query_roster(backend.chars, guild_id, self.cfg.mysql_exe)}

    def bank(self, guild_id):
        """The guild bank (bankread.parse_bank's dict): four read-only SELECTs on the character
        database, the credentials never leaving this process."""
        from leeroy_mind.personas.roster import run_mysql
        from .bankread import bank_sql, parse_bank
        queries = bank_sql(guild_id)
        with self._lock:
            backend = self._get()
            return parse_bank(*(run_mysql(backend.chars, q, self.cfg.mysql_exe) for q in queries))

    def friends(self, human):
        from leeroy_mind.personas.roster import run_mysql, RosterError
        if type(human) is not int or not 0 < human <= 0xFFFFFFFF:
            raise ValueError("invalid owner identity")
        with self._lock:
            backend = self._get()
            rows = run_mysql(backend.chars,
                f"SELECT friend FROM character_social WHERE guid={human} AND (flags & 1)<>0", self.cfg.mysql_exe)
        if any(len(row) != 1 or not row[0].isascii() or not row[0].isdigit()
               or not 0 < int(row[0]) <= 0xFFFFFFFF for row in rows):
            raise RosterError("invalid friends snapshot")
        return {int(row[0]) for row in rows}

    def kit(self, human):
        from leeroy_mind.personas.roster import run_mysql
        from .kitread import kit_sql, parse_kit
        query = kit_sql(human)
        with self._lock:
            backend = self._get()
            if not run_mysql(backend.chars, f'SELECT guid FROM characters WHERE guid={human}', self.cfg.mysql_exe):
                return None
            return parse_kit(run_mysql(backend.chars, query, self.cfg.mysql_exe), log)


class LivePorts:
    def __init__(self, app, *, source=None, jobs=None):
        self.app, self.clock, self.conn = app, app.clock, app.store.conn
        self.source = source
        self.jobs = jobs if jobs is not None else JobWaiter()
        self._gpu_lock = asyncio.Lock()
        self._gpu_requests = {}
        self._identity = None
        self.online_override = None
        self.enabled = True
        self.run_for_speech = lambda: None
        self._speech = {}

    @property
    def human_guid(self):
        return self.app.human_guid

    def refresh_identity(self):
        from leeroy_mind.human import human_identity
        self._identity = human_identity(self.app.cfg, self.app.store, self.human_guid)

    def human(self):
        if self._identity is None or self._identity.guid != self.human_guid:
            self.refresh_identity()
        return self._identity

    def human_name(self):
        return self.human().called

    def human_online(self):
        return bool(self.app.world.human_online) if self.online_override is None else self.online_override

    def cast(self):
        return self.app.store.cast()

    def persona(self, guid):
        return self.app.personas.get(guid)

    def persona_state(self, guid):
        return self.app.store.get_state(guid)

    async def send(self, cmd):
        if not self.enabled:
            raise RuntimeError("guild transport unavailable")
        if not isinstance(cmd, StopOfflineRaid):
            # A stop's terminal rides the start command's id; a mailbox entry would never be drained.
            self.jobs.expect(cmd.id)
        return await self.app.bridge.send(cmd, preserve_id=True)

    async def wait_job(self, cmd_id, timeout_s):
        return await self.jobs.wait(cmd_id, timeout_s)

    def propose(self, guid, channel, instruction, *, kind=TurnKind.CALLOUT):
        from leeroy_mind.llm.scheduler import Priority
        from leeroy_mind.talk.turns import Turn
        if self.enabled and self.source is not None:
            self.source.add(Turn(kind=kind, speaker=guid, channel=channel, instruction=instruction,
                priority=Priority.RAID_LEAD if kind is TurnKind.CALLOUT else Priority.SOCIAL,
                deadline=self.clock.now() + (30 if kind is TurnKind.CALLOUT else 20), max_lines=1),
                run=self.run_for_speech() if channel == "raid" else None)

    async def say(self, guid, channel, text, *, kind=TurnKind.CALLOUT, to_guid=0):
        if not self.enabled:
            return False
        run = self.run_for_speech() if channel == "raid" else None
        return await self._speak_owned(
            self.app.engine.say_as(guid, channel, text, kind=kind, to_guid=to_guid), run, False)

    async def speak(self, turn, run):
        if not self.enabled:
            return []
        return await self._speak_owned(self.app.engine.speak(turn, why="source:GuildTurnSource"), run, [])

    async def _speak_owned(self, speech, run, cancelled):
        task = asyncio.create_task(speech)
        self._speech[task] = run
        try:
            return await task
        except asyncio.CancelledError:
            if asyncio.current_task().cancelling():
                raise
            return cancelled  # Only this old run's child was invalidated.
        finally:
            self._speech.pop(task, None)

    def invalidate_run(self, run):
        if run is None:
            return
        if self.source is not None:
            self.source.drop_run(run)
        for task, owned in tuple(self._speech.items()):
            if owned is run:
                task.cancel()

    async def close(self):
        tasks = list(self._speech)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.jobs.close()

    def set_lead(self, guid):
        self.app.raid.set_lead(guid)

    def lineup(self):
        """Tonight's posted raid lineup (decisions.lineup) a formation forms from; None while the
        guild's decisions are off or no lineup is posted for a raid night now."""
        if getattr(self.app, "decisions", None) is None:
            return None
        from leeroy_mind.decisions.lineup import tonight
        return tonight(self.conn, self.clock.now())

    def lineup_formed(self, lineup, raid_id):
        """The formation formed from that lineup: it is in effect, with the raid."""
        if getattr(self.app, "decisions", None) is None:
            return False
        from leeroy_mind.decisions.lineup import formed
        return formed(self.app.store, lineup.decision, raid_id, now=self.clock.now())

    def loot_rule(self, raid_id):
        """The loot rule this raid runs under (decisions.loot_rules), fixed when it opened; None
        while the guild's decisions are off, so loot is today's DKP."""
        if getattr(self.app, "decisions", None) is None:
            return None
        from leeroy_mind.decisions.loot_rules import rule_for_raid, snapshot
        rule = rule_for_raid(self.conn, raid_id)
        if rule is None:
            with self.conn:
                rule = snapshot(self.conn, raid_id)
        return rule

    def fix_loot_rule(self, raid_id):
        """Inside the caller's transaction that opens the raid: the rule it will run under."""
        if getattr(self.app, "decisions", None) is not None:
            from leeroy_mind.decisions.loot_rules import snapshot
            snapshot(self.conn, raid_id)

    async def remember(self, candidate):
        return await self.app.writer.write(candidate, enforce_limits=False)

    async def loot_lost(self, guid, item_name):
        return await self.app.event_rules.on_loot_lost(guid, item_name, ())

    def relationship(self, a, b):
        return self.app.store.get_relationship(a, b)

    def away(self):
        # Task14 remains skipped: consume persisted absence without installing it.
        from leeroy_mind.nightly.store import active_away
        return active_away(self.app.store)

    def in_dungeon(self):
        """Cast members the last cast snapshot put in a raid or 5-man map who have not changed
        zone since: zone events are live, the snapshot's map is not, so a moved member is unknown."""
        from .offline import INSTANCE_MAPS
        live = self.app.world.members
        return {g for g, m in self.app.store.cast().items()
                if m.map in INSTANCE_MAPS and g in live and live[g].zone == m.zone}

    def gpu_paused(self):
        """The owner's GPU pause: every lease but his login's is refused until it ends."""
        return bool(getattr(self.app.arbiter, "paused", False))

    async def acquire_gpu(self, holder):
        arbiter = self.app.arbiter
        if arbiter is None:
            return True
        # Reserve a *request* before the first await, including the lock wait.
        # Disconnect can revoke it before acquire_live has reserved a real
        # holder. A revoked waiter must never resurrect that holder afterward.
        request = self._gpu_requests.get(holder)
        if request is None:
            request = {"arbiter": arbiter, "valid": True, "started": False,
                       "ready": False, "release_lock": asyncio.Lock()}
            self._gpu_requests[holder] = request

        def current():
            return self._gpu_requests.get(holder) is request and request["valid"]

        try:
            async with self._gpu_lock:
                if not current():
                    return False
                if not request["started"]:
                    request["started"] = True
                    # GpuArbiter reserves synchronously before its own await.
                    admitted = await request["arbiter"].acquire_live(holder)
                    if admitted is False:
                        await self._release_gpu_request(holder, request)
                        return False
                elif not request["ready"]:
                    raise RuntimeError("previous guild GPU admission did not complete")
                if not current():
                    return False
                # Join the human/voice holder's admission too. Serializing our
                # own callers prevents guild_speech overtaking offline handback.
                admission = getattr(self.app.engine, "live_admission", None)
                if admission is not None:
                    await admission()
                if not current():
                    return False
                request["ready"] = True
                return True
        except BaseException:
            # Cancellation/error owns cleanup only while this exact generation
            # is still registered; a released old waiter cannot free a new one.
            if self._gpu_requests.get(holder) is request:
                try:
                    await self._release_gpu_request(holder, request)
                except Exception:
                    # Retain the invalidated receipt for an explicit retry.
                    log.warning("guild GPU release remains pending")
            raise

    async def release_gpu(self, holder):
        request = self._gpu_requests.get(holder)
        if request is not None:
            await self._release_gpu_request(holder, request)

    async def _release_gpu_request(self, holder, request):
        request["valid"] = False  # Invalidate inline, before a concurrent cleanup can await.
        async with request["release_lock"]:
            if self._gpu_requests.get(holder) is not request:
                return
            if request["started"]:
                await request["arbiter"].release_live(holder)
            if self._gpu_requests.get(holder) is request:
                self._gpu_requests.pop(holder)


class MaintenancePorts:
    """Full current guild visibility for maintenance only; never mutate the talk cast."""

    def __init__(self, ports):
        self.ports = ports
        self.members = {}

    def __getattr__(self, name):
        return getattr(self.ports, name)

    def cast(self):
        from dataclasses import replace
        cast = self.ports.cast()
        # Retain bridge-only spec_tab for its current members; MySQL is the
        # authoritative membership filter, including lower-level guild bots.
        return {g: replace(m, spec_tab=cast[g].spec_tab) if g in cast and cast[g].class_ == m.class_ else m
                for g, m in self.members.items()}


class GuildPorts(Protocol):
    clock: Clock
    conn: sqlite3.Connection
    human_guid: int

    def human(self) -> HumanIdentity: ...
    def human_name(self) -> str: ...
    def human_online(self) -> bool: ...
    def cast(self) -> dict[int, Member]: ...
    def persona(self, guid: int) -> Persona | None: ...
    def persona_state(self, guid: int) -> PersonaStateRow: ...
    async def send(self, cmd) -> Ack: ...
    async def wait_job(self, cmd_id: str, timeout_s: float) -> Job | None: ...
    def propose(self, guid: int, channel: str, instruction: str,
                *, kind: TurnKind = TurnKind.CALLOUT) -> None: ...
    async def say(self, guid: int, channel: str, text: str, *,
                  kind=TurnKind.CALLOUT, to_guid=0) -> bool: ...
    def set_lead(self, guid: int | None) -> None: ...
    async def remember(self, c: MemoryCandidate): ...
    async def loot_lost(self, guid: int, item_name: str): ...
    def relationship(self, a: int, b: int) -> Relationship: ...
    def away(self) -> set[int]: ...
    def in_dungeon(self) -> set[int]: ...
    def gpu_paused(self) -> bool: ...
    async def acquire_gpu(self, holder: str): ...
    async def release_gpu(self, holder: str): ...
