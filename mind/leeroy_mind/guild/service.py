"""Guild-life attachment. Dispatch records events; independent bounded lanes do work."""

import asyncio
from collections import deque
import logging
import random

from leeroy_mind.protocol import Hello, Login, Logout

from .calls import CallGap, RaidCalls
from .config import load_guild_config
from .offline import OfflineRaids
from .play import Play, summon_waves
from .ports import LazyArmory, LivePorts, MaintenancePorts
from .protocol import register
from .raid_night import RaidNight
from .roster import Roster


log = logging.getLogger(__name__)
EVENTS = ("hello", "login", "logout", "chat", "zone", "boss", "death", "resurrect", "dc_status", "loot_drop", "job")


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
            await self.offline.on_job(ev)
            await self.roster.on_job(ev)
            return
        if self.app.is_backlog(ev):
            return
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
        self.ports.enabled = False
        self.source.clear()
        self.ports.jobs.close()
        self.raid_night._end("disconnected")
        self.offline._cancel()
        await self.offline._release()

    async def online_tick(self):
        if self._ready() and self._available() and not self.offline.active:
            await self.raid_night.tick()

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

    async def speech_tick(self):
        if not self._ready() or not self.source.managed and self.ports.human_online():
            return
        row = self.source.take_scoped(self.app.clock.now())
        if row is None:
            return
        owner, turn, run = row
        online = self.ports.human_online()
        try:
            if await self.ports.acquire_gpu("guild_speech") is False:
                return
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
            except Exception:
                # No SQL parameters, identities, paths or chat in public logs.
                log.warning("guild lane failed: %s", name)
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
