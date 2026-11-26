"""Guild-life attachment. Dispatch records events; independent bounded lanes do work."""

import asyncio
from collections import deque
import logging
import random
import sqlite3

from leeroy_mind.llm.client import GpuPaused
from leeroy_mind.protocol import Hello, Login, Logout

from .calls import CallGap, RaidCalls
from .config import load_guild_config
from .offline import OfflineRaids
from .play import Play, summon_waves
from .ports import LazyArmory, LivePorts, MaintenancePorts
from .protocol import register
from .raid_night import ConsoleRefused, RaidNight
from .roster import Roster


log = logging.getLogger(__name__)


def _where(error) -> str:
    """Where `error` was raised: the innermost traceback frame as `file.py:line function`."""
    tb, last = error.__traceback__, None
    while tb is not None:
        last, tb = tb, tb.tb_next
    if last is None:
        return "?"
    code = last.tb_frame.f_code
    return f"{code.co_filename.replace(chr(92), '/').rsplit('/', 1)[-1]}:{last.tb_lineno} {code.co_name}"


EVENTS = ("hello", "login", "logout", "chat", "zone", "boss", "death", "resurrect", "dc_status", "loot_drop", "job",
          "guild_group", "group", "member_dungeon_capability")


class GuildTurnSource:
    def __init__(self, app, *, capacity=128):
        self.app = app
        self._turns = deque(maxlen=capacity)
        self.managed = False

    def add(self, turn, *, run=None):
        self._turns.append((self.app.human_guid, turn, run))

    def take(self, now):
        row = self.take_scoped(now)
        return row[1] if row is not None else None

    def take_scoped(self, now):
        while self._turns:
            owner, turn, run = self._turns.popleft()
            if owner == self.app.human_guid and now < turn.deadline:
                return owner, turn, run
        return None

    async def propose(self, now):
        # Standalone consumers retain the TurnSource contract. While GuildLife
        # runs, its own lane retains scope through the awaited public pipeline.
        if self.managed or not self.app.world.human_online:
            return []
        turns = []
        while (turn := self.take(now)) is not None:
            turns.append(turn)
        return turns

    def clear(self):
        self._turns.clear()

    def drop_run(self, run):
        self._turns = deque((row for row in self._turns if row[2] is not run), maxlen=self._turns.maxlen)


class GuildLife:
    def __init__(self, app, cfg, armory):
        self.app, self.cfg, self.armory = app, cfg, armory
        self.source = GuildTurnSource(app)
        self.ports = LivePorts(app, source=self.source)
        self.maintenance = MaintenancePorts(self.ports)
        rng = getattr(app, "rng", None) or random.Random()
        self.gap = CallGap(app.clock)
        self.play = Play(self.ports, rng)
        self.calls = RaidCalls(self.ports, gap=self.gap)
        # Imported only when the installation is assembled; no optional stage
        # or background job is attached as a side effect of module import.
        from .habits import BetweenPulls
        self.habits = BetweenPulls(self.ports, rng, gap=self.gap)
        self.listeners = (self.play, self.calls, self.habits)
        self.raid_night = RaidNight(self.ports, cfg, armory, rng,
            summon_plan=lambda members: summon_waves(members,
                {g: self.ports.persona(g) for g in members}, rng, cfg.late_summon_s))
        self.offline = OfflineRaids(self.ports, cfg, armory, rng)
        self.ports.run_for_speech = lambda: self.offline.run if self.offline.active else self.raid_night.run
        for controller in (self.raid_night, self.offline):
            for listener in self.listeners:
                controller.add_listener(listener)
        self.roster = self._roster()
        self._tasks = []
        self._stopping = False
        self._presence_pending = False
        self._last_offline = float("-inf")
        self._snapshot_at = float("-inf")
        self._snapshot_owner = None
        self._snapshot_valid = False
        self._connected = False
        self.heroics = None
        self._heroic_feature = False

    def configure_dungeons(self, enabled, realm=None):
        realm = realm if realm is not None else self.app.realm
        if self.heroics is None:
            from leeroy_mind.godmode.heroics import HELD, Heroics, end_offers
            if not enabled and not self.app.store.conn.execute(
                    "SELECT 1 FROM member_heroic WHERE " + HELD + " LIMIT 1").fetchone():
                if self.app.store.conn.execute("SELECT 1 FROM member_heroic WHERE phase IN "
                        "('offered','chat','agreed') LIMIT 1").fetchone():
                    end_offers(self.app.store, "disabled")
                return
            if "day" not in realm.parts:
                realm.configure_day_plans(False, owner_handoff=True)
            self.heroics = Heroics(self,realm)
            self.heroics.advertised = self._heroic_feature
            if self._tasks and not self._stopping:
                self._tasks.append(asyncio.create_task(self._loop("heroics",self.heroics.tick),name="guild-heroics"))
        self.heroics.configure(enabled)

    def _roster(self):
        return Roster(self.maintenance, self.cfg, self.armory,
            friends_of=lambda: self.armory.friends(self.ports.human_guid),
            raid_active=lambda: self.raid_night.active or self.offline.active)

    def _ready(self):
        return self._connected and not self._stopping and not self._presence_pending

    def _available(self):
        return bool(getattr(self.armory, "available", True))

    async def before_presence(self, ev):
        if self.app.is_backlog(ev):
            return
        if not isinstance(ev, Hello) and not ev.human:
            return
        self._presence_pending = True
        online = bool(ev.human_guid) if isinstance(ev, Hello) else isinstance(ev, Login)
        self.ports.online_override = online
        # This happens before core session startup may await its model check.
        if online:
            self.offline._cancel()
            if self.heroics is not None:
                self.heroics.end_offers("owner_online")
                await self.heroics.release()
        identity_changed = (ev.human_guid if isinstance(ev, Hello) else ev.guid) not in {0, self.ports.human_guid}
        if not online or isinstance(ev, Hello) or identity_changed:
            if self.raid_night.active or self.raid_night.run is not None:
                self.raid_night._end("logout" if not online else "reconnected")
        if isinstance(ev, Hello) or identity_changed:
            self.raid_night._login_seen = False
            self.source.clear()
        for listener in self.listeners:
            run = getattr(listener, "_run", None)
            if run is not None and (bool(run.plan.offline) == online or isinstance(ev, Hello) or identity_changed):
                listener.invalidate(run)

    async def event(self, ev, name):
        if self._stopping:
            return
        # Durable owned job receipts remain relevant on reconnect even when old;
        # each recipient checks its own sent timestamp, ID, kind and partition.
        if name == "job":
            self.ports.jobs.feed(ev)
            if ev.kind=="member_dungeon" and self.heroics is not None:
                self.heroics.job(ev)
                if ev.state in {"done","failed"}:
                    await self.ports.jobs.wait(ev.id,0)
                return
            await self.offline.on_job(ev)
            await self.roster.on_job(ev)
            return
        if self.app.is_backlog(ev):
            return
        if name == "member_dungeon_capability":
            realm = getattr(self.app,"realm",None)
            if realm is not None and realm._delivery_epoch(ev)==realm._transport_epoch:
                c = realm.action_context()
                if c.presence_known and (c.guild,c.human)==(ev.guild,ev.human):
                    self._heroic_feature = ev.enabled
                    if self.heroics is not None:
                        self.heroics.advertised = ev.enabled
                        if not ev.enabled:
                            self.heroics.end_offers("native_disabled")
            return
        if name=="boss" and self.heroics is not None:
            self.heroics.boss(ev)
        if name in {"hello", "login", "logout"}:
            if name != "hello" and (not ev.human or ev.guid != self.ports.human_guid):
                return
            self._presence_pending = False
            self.ports.online_override = None
            self.ports.refresh_identity()
            if self.roster._human != self.ports.human_guid:
                self.maintenance.members = {}
                self.roster = self._roster()
            if name == "hello":
                self.ports.loot_receipts = "loot_receipts_v1" in ev.features
                self.ports.loot_receipt_status = "loot_receipt_status_v1" in ev.features
                if self.cfg.loot_receipts and not self.ports.loot_receipts:
                    self._receipts_unavailable()
                # First: the runner's raids must be known before any offline tick can plan one.
                self._heroic_feature = "member_dungeon_v1" in ev.features
                if self.heroics is not None:
                    self.heroics.advertised = self._heroic_feature
                    self.heroics.poll_at = 0
                    self.heroics.set_playing(None)
                await self.offline.on_hello(ev)
                self._connected = True
                self.ports.enabled = True
                self._snapshot_at = float("-inf")
                await self.raid_night.on_hello(ev)
                await self.roster.on_hello(ev)
                presence = (Login if ev.human_guid else Logout)(ev.ts, self.ports.human_guid, True)
            else:
                presence = ev
            method = "on_login" if self.ports.human_online() else "on_logout"
            await getattr(self.offline, method)(presence)
            await getattr(self.raid_night, method)(presence)
            await getattr(self.roster, method)(presence)
            return
        controller = self.offline if self.offline.active else self.raid_night
        if name == "chat" and self.offline.active:
            # An outstanding remote raid cannot be replaced by a local online
            # formation, even after its local GPU hold has been released.
            return
        if name == "chat" and not self._available():
            return
        hook = getattr(controller, "on_" + name, None)
        if hook is not None:
            await hook(ev)
        if name in {"boss", "death", "resurrect", "dc_status"}:
            await getattr(self.habits, "on_" + name)(ev)

    async def connection(self, connected):
        # A TCP connection is not a greeted/current guild snapshot yet.
        if connected:
            return
        self._connected = False
        self._heroic_feature = False
        if self.heroics is not None:
            self.heroics.advertised = False
            self.heroics.attached = None
            self.heroics.set_playing(None)
            self.heroics.end_offers("disconnected")
            await self.heroics.release()
        self.ports.enabled = False
        self.source.clear()
        self.ports.jobs.close()
        self.raid_night._end("disconnected")
        self.offline._cancel()
        # Revoke queued speech before releasing a blocking offline admission:
        # otherwise the waiter could preload a model after this disconnect.
        try:
            await self.ports.release_gpu("guild_speech")
        finally:
            await self.offline._release()

    async def online_tick(self):
        if self._ready() and self._available() and not self.offline.active:
            await self.raid_night.tick()

    # -- the owner's console (leeroy_mind.endpoint): the raid night's own guards, then its own
    # transitions (RaidNight.console_act), on this event loop

    def _console_refusal(self):
        if self._stopping:
            return "the mind is stopping"
        if not self._connected:
            return "the mind is not connected to the game"
        if self._presence_pending:
            return "your login or logout is still being handled: try again in a moment"
        if not self._available():
            return ("the game's database (the guild armory) is unavailable, so guild life runs "
                    "no raid nights")
        if self.offline.active:
            return "an offline raid is still running or closing"
        return None

    def console_status(self):
        status = self.raid_night.console_status()
        why = self._console_refusal()
        if why is not None:
            status["actions"] = dict.fromkeys(status["actions"], why)
        status["blocked"] = why
        status["offline_raid"] = bool(self.offline.active)
        return status

    async def console_action(self, args):
        """args as leeroy_mind.endpoint.raid_args checked them; what it did, or
        ConsoleRefused."""
        why = self._console_refusal()
        if why is not None:
            raise ConsoleRefused(why)
        pick = None
        if args["action"] == "form":
            pick = self.raid_night.console_pick(args["raid"], args["size"], args["heroic"],
                                                args["lead"])
        return await self.raid_night.console_act(args["action"], pick,
                                                 guard=self._console_refusal)

    def members_in_play(self) -> frozenset[int]:
        playing = self.raid_night.members_in_play() | self.offline.members_in_play()
        if (heroics := getattr(self, "heroics", None)) is not None:
            playing |= heroics.members_in_play()
        return playing - {self.ports.human_guid}

    async def offline_tick(self):
        if not self._ready():
            return
        now = self.app.clock.now()
        # Active remote jobs need prompt bookkeeping, loot windows and terminal
        # release. Only the search for a new offline run is on a 60s schedule.
        if self.offline.active or now - self._last_offline >= 60:
            self._last_offline = now
            if self.offline.active or self._available() and not self.raid_night.active:
                await self.offline.tick()

    async def roster_tick(self):
        if not self._ready() or not self._available():
            return
        raw = self.app.store.get_meta("guild_id")
        if not raw or not raw.isascii() or not raw.isdigit() or not 0 < int(raw) <= 0xFFFFFFFF:
            return
        owner = (self.ports.human_guid, int(raw))
        now = self.app.clock.now()
        if owner != self._snapshot_owner or now - self._snapshot_at >= 60:
            self._snapshot_at = now
            self._snapshot_owner = owner
            self._snapshot_valid = False
            self.maintenance.members = {}
            members = await asyncio.to_thread(self.armory.roster, owner[1])
            if not self._ready() or owner != (self.ports.human_guid, int(self.app.store.get_meta("guild_id") or 0)):
                return
            self.maintenance.members = members
            self._snapshot_valid = True
        if not self._snapshot_valid:
            return
        await self.roster.tick()

    def _receipts_unavailable(self):
        """[guild] loot_receipts is on but this bridge's hello has no loot_receipts_v1 (native
        Leeroy.LootReceipts.Enable off, or an older worldserver): new hand-outs go without receipts,
        and an award already waiting on one stays held, never sent again. Once a connection, with
        counts only."""
        try:
            held = self.ports.conn.execute(
                "SELECT count(*) FROM meta WHERE key LIKE 'guild:loot:%' AND json_extract(value,'$.receipt')=1 "
                "AND json_extract(value,'$.state') IN ('sending','uncertain')").fetchone()[0]
        except sqlite3.Error:
            held = "unknown"
        log.warning("loot_receipts_unavailable feature=loot_receipts_v1 held=%s", held)

    async def loot_receipts_tick(self):
        # Settlements survive the raid, reconnects and turning new receipt awards off.
        if self._ready() and getattr(self.ports, "loot_receipt_status", False):
            from .loot import reconcile_receipts
            await reconcile_receipts(self.ports)

    async def speech_tick(self):
        if not self._ready() or not self.source.managed and self.ports.human_online():
            return
        row = self.source.take_scoped(self.app.clock.now())
        if row is None:
            return
        owner, turn, run = row
        online = self.ports.human_online()
        try:
            try:
                if await self.ports.acquire_gpu("guild_speech") is False:
                    return
            except GpuPaused:
                return  # the owner paused the GPU: the turn is dropped, as a refused one is
            if (not self._ready() or owner != self.ports.human_guid
                    or online != self.ports.human_online() or self.app.clock.now() >= turn.deadline
                    or run is not None and run is not self.ports.run_for_speech()):
                return
            await self.ports.speak(turn, run)
        finally:
            await self.ports.release_gpu("guild_speech")

    async def listener_tick(self, listener):
        if self._ready():
            if listener is self.habits:
                controller = self.offline if self.offline.active else self.raid_night
                # During ordered on_start, an event can precede Habits' own
                # initialization. Its normal queued replay must be drained
                # before its first action; later direct observations preempt
                # work immediately and exact replays are idempotent.
                if controller._lock.locked():
                    return
            await listener.tick()

    async def _loop(self, name, action):
        while True:
            try:
                await action()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                # No SQL parameters, identities, paths or chat in public logs: the exception's
                # class and the line that raised it are code, never its message.
                log.warning("guild lane failed: %s %s at %s", name, type(error).__name__, _where(error))
            await self.app.clock.sleep(1)

    async def run(self):
        if self._tasks:
            return
        # One consumer for this source while installed. Public engine.speak
        # retains normal priority/guards/typing; owning its awaited child keeps
        # the run scope attached throughout composition and delivery.
        self.source.managed = True
        lanes = [("online", self.online_tick), ("offline", self.offline_tick),
                 ("roster", self.roster_tick), ("speech", self.speech_tick)]
        if self.cfg.loot_receipts or self.ports.conn.execute(
                "SELECT 1 FROM meta WHERE key LIKE 'guild:loot:%' AND json_extract(value,'$.receipt')=1 "
                "AND json_extract(value,'$.state') IN ('sending','uncertain','acknowledged') LIMIT 1").fetchone():
            lanes.append(("loot_receipts", self.loot_receipts_tick))
        if self.heroics is not None:
            lanes.append(("heroics", self.heroics.tick))
        lanes += [(type(listener).__name__, lambda listener=listener: self.listener_tick(listener))
                  for listener in self.listeners]
        self._tasks = [asyncio.create_task(self._loop(name, action), name="guild-" + name)
                       for name, action in lanes]
        try:
            await asyncio.gather(*self._tasks)
        finally:
            self._stopping = True
            self.ports.enabled = False
            self.source.clear()
            self.raid_night._end("shutdown")
            self.offline._cancel()
            self.ports.jobs.close()
            for task in self._tasks:
                task.cancel()
            await asyncio.gather(*self._tasks, return_exceptions=True)
            await self.offline._release()
            await self.ports.release_gpu("guild_speech")
            if self.heroics is not None:
                await self.heroics.release()
            await self.ports.close()
            self._tasks = []


def install(app, *, cfg=None, armory=None):
    """Construct and register only. Never start tasks or issue a MySQL query here."""
    register()
    cfg = cfg or load_guild_config(app.data_dir / "mind.toml")
    if armory is None:
        from leeroy_mind.llm.client import OllamaClient
        armory = LazyArmory(cfg)
        # Like nightly NullAdmin: an injected synthetic model client must not
        # quietly reach the configured game DB. Synthetic integration can pass
        # its own armory explicitly; normal apps retain lazy real game reads.
        if hasattr(app, "llm") and not isinstance(app.llm, OllamaClient):
            armory.unavailable()
    service = GuildLife(app, cfg, armory)
    for name in EVENTS:
        async def handler(ev, name=name):
            await service.event(ev, name)
        app.register_event_handler(name, handler)
    for name in ("hello", "login", "logout"):
        app.register_event_handler(name, service.before_presence, before=True)
    app.register_connection_hook(service.connection)
    app.engine.register_source(service.source)
    app.register_background(service.run)
    return service
