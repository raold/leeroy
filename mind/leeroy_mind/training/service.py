"""Training attachment with owned command lanes and checked guild cleanup."""

import asyncio
from collections import deque
import logging
from dataclasses import asdict

from leeroy_mind.protocol import event_type
from leeroy_mind.wow import BOSS_NAMES
from leeroy_mind.guild.boss_calls import BOSS_CALLS
from leeroy_mind.guild.loot import _get, _once

from . import protocol, store
from .applier import Applier, ApplierState
from .checks import CHECKS_VERSION, _restore, run_checks
from .config import load_training_config
from .facts import TalkHook
from .gate import Gate
from .ledger import Ledger
from .ledger import combat_role
from .levers import MemberInfo
from .lines import template
from .listener import TrainingListener
from .mechanics import watch_names
from .ports import LivePorts
from .report import training_section
from .score import score_pull
from .spells import SpellBook, load_spellbook, read_bonus_data
from .vocab import Vocab, refresh, suspend_missing
from .voice_lead import VoiceLead

log = logging.getLogger(__name__)


def load_book(data_dir):
    """Blocking file/optional public SQL reads; called only on a worker thread."""
    from leeroy_mind.guild.config import load_guild_config
    from leeroy_mind.library.world import dbc_dir_of
    from leeroy_mind.personas.roster import read_db_info
    cfg = load_guild_config(data_dir / "mind.toml")
    try:
        folder = dbc_dir_of(cfg.worldserver_conf)
    except OSError:
        folder = None
    if folder is None:
        log.warning("training_dbc_unavailable")
        return SpellBook()
    world = None
    if cfg.mysql_exe.is_file():
        try:
            world = read_bonus_data(read_db_info(cfg.worldserver_conf, "WorldDatabaseInfo"), cfg.mysql_exe)
        except Exception:
            log.warning("training_bonus_unavailable")
    return load_spellbook(folder, world=world)


async def rescore_stored(ports, book):
    """Checks invalidate scores; always restore the matching score in this pass."""
    count = 0
    for row in store.pull_rows(ports.conn, scoreable=True):
        if row["checks_version"] >= CHECKS_VERSION and row["score"] is not None:
            continue
        record, roles = _restore(ports.conn, row)
        result = run_checks(record, roles, book)
        if await score_pull(ports, record, None, result) is not None:
            count += 1
        await asyncio.sleep(0)
    return count


def report_section(ctx, week):
    return training_section(ctx.store.conn, week, now=ctx.clock.now(), names=lambda guid: str(guid))


class TrainingGround:
    CAPACITY = 128

    def __init__(self, app, ports):
        self.app, self.ports = app, ports
        self._book = None
        self.ledger = Ledger(ports)
        self.gate = Gate(ports, watch=self.watch_ids)
        self.vocab = Vocab({}, {}, {}, frozenset(), 0)
        self._startup_ready = False
        self.listener = TrainingListener(ports, self.ledger, self.gate, submit=self.submit,
                                         members_provider=self.members_for)
        self.applier = Applier(ports, self.ledger, state_provider=self.state_for,
                               application_allowed=self.application_allowed)
        self.listener.set_applier(self.applier)
        self.voice = VoiceLead(ports, self)
        self.ledger.after_store.insert(0, self._score)
        self._queue = deque()
        self._wake = asyncio.Event()
        self._epoch = 0
        self._operation = self._sample = None
        self._connected = False
        self._presence_known = False
        self._stopping = False
        self._running = False
        self._lanes = set()
        self._loader = None
        self._close_task = None

    def book(self):
        return self._book

    def watch_ids(self):
        return tuple(sorted({id for name in watch_names() for id in self._book.ids(name)})) if self._book else ()

    def training_members(self):
        return set()

    def application_allowed(self):
        return bool(self._connected and self._presence_known and not self._stopping
                    and self._startup_ready and self.vocab.complete and self.gate.phase == "passed")

    def members_for(self, run):
        members = {}
        cast = self.ports.cast()
        for guid in run.plan.members:
            m, p = cast.get(guid), self.ports.persona(guid)
            if m is not None:
                role = combat_role(m.class_, m.spec_tab, p.play.raid_role if p else None,
                                   tank=guid == run.plan.tank_guid)
                members[guid] = MemberInfo(guid, role, m.class_, m.spec_tab)
        return members

    def state_for(self, key):
        """Cleanup retains exact session identity even after active-run detachment."""
        if not self._connected or not self._presence_known or self._stopping:
            return None
        run = self.listener.retained_run(key) if key is not None else self.listener.run
        bodies = getattr(self.app, "bodies", None)
        if run is None:
            # No active-run fallback is permitted for an explicit old run key.
            if (key is not None or self.listener._ending or self._guild_busy() or bodies is not None
                    or self.ports.human_online()):
                return None
            members = {}
            for guid, m in self.ports.cast().items():
                if guid == self.ports.human_guid:
                    continue
                p = self.ports.persona(guid)
                members[guid] = MemberInfo(guid, combat_role(m.class_, m.spec_tab,
                    p.play.raid_role if p else None), m.class_, m.spec_tab)
            online = frozenset(g for g in members if self.ports.cast()[g].online and g not in self.ports.away())
            return ApplierState(None, f"training:idle:{self.ports.human_guid}", None,
                self.ports.connection_epoch, 0, False, members, online, self.vocab)
        session = run.session
        row = self.ports.conn.execute("SELECT * FROM guild_raid WHERE raid_id=?", (session.raid_id,)).fetchone()
        if (row is None or row["ended"] is not None or session._closing
                or asdict(run.plan) != asdict(session.plan)
                or _get(self.ports.conn, f"guild:raid:{session.raid_id}:plan") != asdict(run.plan)
                or session.ctx.instance_id != _get(self.ports.conn, f"guild:raid:{session.raid_id}:instance")
                or type(session.ctx.instance_id) is not int or session.ctx.instance_id <= 0):
            return None
        owner = None
        if bodies is not None:
            owner_for = getattr(bodies, "owner_for_run", None)
            owner = owner_for(run) if callable(owner_for) else None
            if not isinstance(owner, str) or not owner:
                return None  # Realm must supply proof of this session's claim.
        members = self.members_for(run)
        cast = self.ports.cast()
        online = frozenset(g for g in members if g != self.ports.human_guid
            and cast[g].online and cast[g].map == session.ctx.map_id and g not in self.ports.away())
        return ApplierState(self.listener.run_key(run), f"guild:raid:{session.raid_id}:{session.started}",
            session.ctx.instance_id, self.ports.connection_epoch, run.plan.tank_guid,
            run.lead_is_human, members, online, self.vocab, owner)

    def pre_call(self, map_id, boss_id):
        run = self.listener.run
        if (run is None or not run.human_in_raid or run.lead_is_human or map_id != run.session.ctx.map_id
                or self.listener._ending or run.session._closing
                or not self.applier.acting(self.ports.clock.now())):
            return None
        context = self.listener._context(boss_id, run)
        selected, _ = self.listener.card_for(context, run)
        row = store.champion(self.ports.conn, context)
        if not selected.call or selected.call == getattr(BOSS_CALLS.get((map_id, boss_id)), "pre", None):
            return None
        call = selected.call
        # Promotion date, not candidate creation, describes when it was tested.
        # Old-plan fallbacks and runtime progression changes are not newly
        # promoted plans, even when they retain a champion's durable card id.
        if (row is not None and row["id"] == selected.id and row["origin"] != "stock"
                and row["call"] == call and not self.listener.change_for(context)
                and _once(self.ports.conn, f"tg_intro:{row['id']}")):
            age = max(0, self.ports.clock.now() - row["changed"])
            when = "yesterday" if age < 2 * 86400 else "this week" if age < 7 * 86400 else "last week"
            return template("new_plan", boss=BOSS_NAMES.get((map_id, boss_id), "boss"), when=when, call=call)
        return call

    def lead_pre_call(self, map_id, boss_id):
        call = self.pre_call(map_id, boss_id)
        return self.voice.pre_call(map_id, boss_id, call=call) or call

    async def _score(self, rec, ctx):
        if self._book is None:
            return
        roles = {row["guid"]: row["role"] for row in store.member_rows(self.ports.conn, rec.header.id)}
        result = run_checks(rec, roles, self._book)
        await score_pull(self.ports, rec, ctx, result)
        await self.listener.after_pull(rec, ctx, result)

    def submit(self, name, args=(), kwargs=None, run=None):
        if self._stopping or not self._connected:
            return
        if len(self._queue) >= self.CAPACITY:
            # Missing a lifecycle boundary invalidates the whole experiment run.
            self.listener.invalidate(self.listener.run)
            self._queue.clear()
            if self._operation:
                self._operation.cancel()
            log.warning("training_gate_queue_full")
            return
        self._queue.append((self._epoch, name, args, kwargs or {}, run))
        self._wake.set()

    async def on_connection(self, connected):
        self._connected = False
        self._presence_known = False
        self._startup_ready = False
        self.voice.invalidate(self.listener.run)
        self.listener.invalidate(self.listener.run)
        self._epoch += 1
        self._queue.clear()
        # Fence before cancellation can yield to a late response.
        await self.gate.on_connection(connected)
        await self.ledger.on_connection(connected)
        await self.ports.disconnect()
        tasks = [task for task in (self._operation, self._sample) if task is not None]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._connected = connected and not self._stopping

    async def on_event(self, ev):
        self.ports.feed(ev)  # Reply delivery never waits for a command's ACK.
        name = event_type(ev)
        if name.startswith("pull"):
            await self.ledger.on_event(ev)
            if name == "pull_pulse":
                await self.listener.on_pulse(ev)
        elif name == "chat":
            if ev.from_guid == self.ports.human_guid:
                await self.listener.on_human_chat(ev)
        elif name == "boss_cast":
            await self.voice.on_cast(ev)
        elif name == "boss":
            await self.voice.on_boss(ev)
        elif name == "hello":
            # A hello is a new observed server boundary even when a transport
            # implementation did not issue a separate reconnect notification.
            await self.on_connection(True)
            self._presence_known = True
            self.submit("on_hello")
            self.submit("startup")
        # Training actions use the validated guild listener. Voice consumes
        # independently checked boss boundaries inline, ahead of following casts.
        # Presence is read from app.world by the independent loader lane.

    async def _commands(self):
        while True:
            if not self._queue:
                self._wake.clear()
                await self._wake.wait()
                continue
            epoch, name, args, kwargs, run = self._queue.popleft()
            if (epoch != self._epoch or not self._connected
                    or run is not None and run is not self.listener.run):
                continue
            action = self._startup if name == "startup" else getattr(self.gate, name)
            self._operation = asyncio.create_task(action(*args, **kwargs))
            try:
                await self._operation
            except asyncio.CancelledError:
                if asyncio.current_task().cancelling():
                    raise
            except Exception:
                log.warning("training_gate_operation_failed")
            finally:
                self._operation = None

    async def _startup(self):
        epoch = self._epoch
        self._startup_ready = False
        await self.applier.replay_on_hello()
        if epoch != self._epoch or not self._connected or store.open_applied(self.ports.conn, 0):
            return
        current = await refresh(self.ports)
        if epoch != self._epoch or not self._connected:
            return
        self.vocab = current
        suspend_missing(self.ports.conn, current, ts=self.ports.clock.now())
        self._startup_ready = current.complete
        log.info("training_vocab_refreshed classes=%d complete=%s", len(current.classes), current.complete)

    async def _listening(self):
        while True:
            if self._connected:
                try:
                    await self.listener.tick()
                except Exception:
                    log.warning("training_listener_tick_failed")
            await self.ports.clock.sleep(1)

    async def _sampling(self):
        while True:
            if self._connected:
                self._sample = asyncio.create_task(self.gate.sample())
                try:
                    await self._sample
                except asyncio.CancelledError:
                    if asyncio.current_task().cancelling():
                        raise
                except Exception:
                    log.warning("training_sample_failed")
                finally:
                    self._sample = None
            await self.ports.clock.sleep(1)

    async def _voicing(self):
        while True:
            if self._connected:
                try:
                    await self.voice.tick()
                except Exception:
                    log.warning("training_voice_tick_failed")
            await self.ports.clock.sleep(.1)

    async def _maintenance(self):
        expired, pruned = float("-inf"), None
        while True:
            now, local = self.ports.clock.now(), self.ports.clock.local()
            if now - expired >= 60:
                self.ledger.expire(now)
                expired = now
                if self._presence_known and not self._startup_ready and not self._operation and not self._queue:
                    self.submit("startup")
            if (local.hour, local.minute) >= (4, 30) and pruned != local.date():
                store.prune_tracks(self.ports.conn, int((now - 30 * 86400) * 1000))
                pruned = local.date()
            await self.ports.clock.sleep(1)

    async def _load(self):
        while (not self._presence_known or not self._connected or self.ports.human_online()
                or self.listener.run is not None or self._guild_busy()):
            await self.ports.clock.sleep(1)
        worker = self._loader = asyncio.create_task(asyncio.to_thread(load_book, self.app.data_dir))
        try:
            self._book = await asyncio.shield(worker)
        except asyncio.CancelledError:
            # to_thread cannot stop file IO. The shared close task owns this
            # shielded worker until it actually ends, even after repeated cancel.
            raise
        await rescore_stored(self.ports, self._book)

    def _guild_busy(self):
        guild = getattr(self.app, "guild", None)
        return guild is not None and any(getattr(getattr(guild, name, None), "active", False)
                                        for name in ("raid_night", "offline"))

    async def run(self):
        if self._stopping:
            return
        if self._running:
            raise RuntimeError("training already running")
        self._running = True
        self._lanes = {asyncio.create_task(fn()) for fn in
                       (self._commands, self._sampling, self._maintenance, self._load, self._listening, self._voicing)}
        try:
            # Cancellation fences promptly; one shared drain owns cancellation
            # and joins instead of allowing gather to cancel the loader twice.
            await asyncio.shield(asyncio.gather(*self._lanes))
        finally:
            await self.close()
            self._running = False

    async def close(self):
        """One shared, shielded resource barrier, also safe before run()."""
        if self._close_task is None:
            self._stopping = True
            self._connected = self._presence_known = self._startup_ready = False
            self._epoch += 1
            self._queue.clear()
            self.listener.invalidate(self.listener.run)
            self._close_task = asyncio.create_task(self._drain())
        await asyncio.shield(self._close_task)

    async def _drain(self):
        owned = self._lanes | {task for task in (self._operation, self._sample) if task is not None}
        for task in owned:
            task.cancel()
        if owned:
            await asyncio.gather(*owned, return_exceptions=True)
        if self._loader is not None:
            # Never cancel the to_thread worker: its synchronous IO cannot be
            # cancelled, and it remains owned until this actual join completes.
            await asyncio.gather(self._loader, return_exceptions=True)
        while not await self.listener.close():
            log.warning("training listener shutdown is still draining")
        while not await self.applier.close():
            log.warning("training command shutdown is still draining")
        await self.voice.close()
        await self.on_connection(False)
        self._lanes.clear()


def attach_training(app):
    cfg = load_training_config(app.data_dir / "mind.toml")
    if not cfg.enabled:
        log.info("training_off")
        return None
    protocol.register()
    app.skip_event_rows |= protocol.TG_EVENT_TYPES
    ground = TrainingGround(app, LivePorts(app, cfg=cfg))
    for name in (*protocol.TG_EVENT_TYPES, "hello", "boss", "login", "logout", "chat", "job"):
        app.register_event_handler(name, ground.on_event)
    for name in ("job", "vocab", "server_stats", "dummy_result", "rivals"):
        app.register_shutdown_receipt(name, ground.ports.feed)
    app.register_connection_hook(ground.on_connection)
    guild = getattr(app, "guild", None)
    if guild is not None:
        for controller in (guild.raid_night, guild.offline):
            controller.add_listener(ground.listener)
            controller.add_listener(ground.voice.lifecycle)
        calls = getattr(guild, "calls", None)
        if calls is not None:
            calls.pre_call, calls.lust_held = ground.lead_pre_call, ground.listener.lust_held
            ground.listener.lust_provider = calls.lust
    hook = TalkHook(ground.ports)
    hook.change_for = ground.listener.change_for
    app.engine.add_instruction_hook(hook)
    runner = getattr(app, "night_runner", None)
    if runner is not None:
        from .coach import CoachSources, stage_coach
        def mechanics(context):
            map_id, boss_id, _ = map(int, context.split(":"))
            return ground.ports.boss_mechanics(BOSS_NAMES.get((map_id, boss_id), ""), None)
        async def coach(ctx):
            await stage_coach(ctx, sources=CoachSources(lambda: ground.vocab, mechanics))
        runner.add_stage("coach", coach, after="reflect", model="reflection")
    from leeroy_mind.eval import register_section
    register_section("training", report_section)
    app.register_background(ground.run)
    app.register_shutdown_drain(ground.close)
    log.info("training_attached stage=2")
    return ground
