"""Stage-one attachment with owned bounded command and sampling lanes."""

import asyncio
from collections import deque
import logging

from leeroy_mind.protocol import event_type

from . import protocol, store
from .checks import CHECKS_VERSION, _restore, run_checks
from .config import load_training_config
from .facts import TalkHook
from .gate import Gate
from .ledger import Ledger
from .listener import TrainingListener
from .mechanics import watch_names
from .ports import LivePorts
from .report import training_section
from .score import score_pull
from .spells import SpellBook, load_spellbook, read_bonus_data

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
        self.listener = TrainingListener(ports, self.ledger, self.gate, submit=self.submit)
        self.ledger.after_store.insert(0, self._score)
        self._queue = deque()
        self._wake = asyncio.Event()
        self._epoch = 0
        self._operation = self._sample = None
        self._connected = False
        self._presence_known = False
        self._stopping = False
        self._running = False

    def book(self):
        return self._book

    def watch_ids(self):
        return tuple(sorted({id for name in watch_names() for id in self._book.ids(name)})) if self._book else ()

    def training_members(self):
        return set()

    async def _score(self, rec, ctx):
        if self._book is None:
            return
        roles = {row["guid"]: row["role"] for row in store.member_rows(self.ports.conn, rec.header.id)}
        await score_pull(self.ports, rec, ctx, run_checks(rec, roles, self._book))

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
        elif name == "hello":
            # A hello is a new observed server boundary even when a transport
            # implementation did not issue a separate reconnect notification.
            await self.on_connection(True)
            self._presence_known = True
            self.submit("on_hello")
        # Bosses deliberately arrive only through the validated guild listener.
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
            self._operation = asyncio.create_task(getattr(self.gate, name)(*args, **kwargs))
            try:
                await self._operation
            except asyncio.CancelledError:
                if asyncio.current_task().cancelling():
                    raise
            except Exception:
                log.warning("training_gate_operation_failed")
            finally:
                self._operation = None

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

    async def _maintenance(self):
        expired, pruned = float("-inf"), None
        while True:
            now, local = self.ports.clock.now(), self.ports.clock.local()
            if now - expired >= 60:
                self.ledger.expire(now)
                expired = now
            if (local.hour, local.minute) >= (4, 30) and pruned != local.date():
                store.prune_tracks(self.ports.conn, int((now - 30 * 86400) * 1000))
                pruned = local.date()
            await self.ports.clock.sleep(1)

    async def _load(self):
        while (not self._presence_known or not self._connected or self.ports.human_online()
                or self.listener.run is not None or self._guild_busy()):
            await self.ports.clock.sleep(1)
        worker = asyncio.create_task(asyncio.to_thread(load_book, self.app.data_dir))
        try:
            self._book = await asyncio.shield(worker)
        except asyncio.CancelledError:
            # to_thread cannot stop file IO; join it before closing the service.
            await asyncio.gather(worker, return_exceptions=True)
            raise
        await rescore_stored(self.ports, self._book)

    def _guild_busy(self):
        guild = getattr(self.app, "guild", None)
        return guild is not None and any(getattr(getattr(guild, name, None), "active", False)
                                        for name in ("raid_night", "offline"))

    async def run(self):
        if self._running:
            raise RuntimeError("training already running")
        self._running = True
        tasks = [asyncio.create_task(fn()) for fn in
                 (self._commands, self._sampling, self._maintenance, self._load)]
        try:
            await asyncio.gather(*tasks)
        finally:
            self._stopping = True
            self.listener.invalidate(self.listener.run)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self.on_connection(False)
            self._running = False


def attach_training(app):
    cfg = load_training_config(app.data_dir / "mind.toml")
    if not cfg.enabled:
        log.info("training_off")
        return None
    protocol.register()
    app.skip_event_rows |= protocol.TG_EVENT_TYPES
    ground = TrainingGround(app, LivePorts(app, cfg=cfg))
    for name in (*protocol.TG_EVENT_TYPES, "hello", "boss", "login", "logout"):
        app.register_event_handler(name, ground.on_event)
    app.register_connection_hook(ground.on_connection)
    guild = getattr(app, "guild", None)
    if guild is not None:
        for controller in (guild.raid_night, guild.offline):
            controller.add_listener(ground.listener)
    app.engine.add_instruction_hook(TalkHook(ground.ports))
    from leeroy_mind.eval import register_section
    register_section("training", report_section)
    app.register_background(ground.run)
    log.info("training_attached")
    return ground
