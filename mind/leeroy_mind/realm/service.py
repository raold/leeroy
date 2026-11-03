"""Default-off Realm attachment and bounded, matched receipt diagnostics.

Receipt rows are observations only. They never establish native body ownership
and the in-memory command identities are intentionally not recovered on restart.
"""

import asyncio
from collections import OrderedDict
from dataclasses import dataclass
import logging

from leeroy_mind.config import ConfigError
from leeroy_mind.protocol import event_type

from . import protocol, store
from .config import load_realm_config
from .ports import LivePorts, RealmDisabled

log = logging.getLogger(__name__)

# Native kQueued in Lib/LeeroyRealmCommands.cpp, plus the two direct handlers
# which publish a value/result. All three arena_team actions use the queue and
# therefore publish a job (create returns the new team ID).
JOB_COMMANDS = frozenset({
    "realm_move", "rpg_status", "leave_pvp", "rearm", "wg_join", "arena_queue",
    "duel", "craft", "equip", "set_profession", "transmog_set",
    "calendar_signup", "mail_send", "bank_deposit", "bank_withdraw",
    "bank_stock", "guild_grow", "arena_team", "item_count", "calendar_post",
})
_PVP_ORIGINS = frozenset({"wg_join", "arena_queue", "duel"})


@dataclass(slots=True)
class _Sent:
    command: object
    kind: str
    expects_job: bool
    ack: bool = False
    job: bool = False


class Realm:
    """Service foundation. Future parts implement async on_event(ev) and tick().

    add_part registers declared events once. Parts must use this service's ports
    for external work; their owned child tasks are cancelled on local disable.
    Construction only registers callbacks; run() owns the asynchronous work.
    """

    CAPACITY = 128
    COMPLETED = 256

    def __init__(self, app):
        self.app = app
        self.cfg = load_realm_config(app.data_dir / "mind.toml")
        self.enabled = self.cfg.enabled
        self._generation = 0
        self._parts = []
        self._handlers = set()
        self._children = set()
        self._pending = OrderedDict()
        self._completed = OrderedDict()
        self._stopping = False
        self._running = False
        self._close_task = None
        self.ports = LivePorts(app, on_send=self.on_send, on_ack=self.on_ack,
                               on_send_error=self.on_send_error)
        self.ports.set_enabled(self.enabled)

    def _set_enabled(self, enabled):
        self.enabled = enabled and not self._stopping
        if not self.enabled:
            self._generation += 1
            for task in tuple(self._children):
                task.cancel()
        self.ports.set_enabled(self.enabled)

    def reload(self):
        """Synchronous presence-boundary reload, before any app session awaits."""
        try:
            self.cfg = load_realm_config(self.app.data_dir / "mind.toml")
        except ConfigError:
            # An unreadable switch cannot authorize continued work.
            log.warning("realm_config_unavailable")
            self._set_enabled(False)
            return
        self._set_enabled(self.cfg.enabled)

    async def on_presence(self, ev):
        name = event_type(ev)
        if name == "hello":
            if ev.human_guid != self.ports.human().guid:
                # The app adopts this identity after its before handlers. Fence
                # both service and ports children before that row can await.
                self._set_enabled(False)
            self.reload()
        elif name == "login" and ev.human and ev.guid == self.ports.human().guid:
            self.reload()

    def _register_event(self, name):
        if name not in self._handlers:
            self.app.register_event_handler(name, self.on_event)
            self._handlers.add(name)

    def add_part(self, part, *, event_types=()):
        """Attach a real part without starting tasks or invoking its methods."""
        if self._stopping:
            raise RuntimeError("realm is closing")
        names = frozenset(event_types)
        self._parts.append((part, names))
        for name in sorted(names):
            self._register_event(name)

    async def _owned(self, operation):
        generation = self._generation
        human = self.ports.human().guid

        def valid():
            return (self.enabled and not self._stopping
                    and generation == self._generation and self.ports.human().guid == human)

        async def invoke():
            if valid():
                await operation()

        if not valid():
            return
        task = asyncio.create_task(invoke())
        self._children.add(task)
        try:
            await task
        except asyncio.CancelledError:
            if asyncio.current_task().cancelling() or valid():
                raise
        finally:
            self._children.discard(task)

    async def on_event(self, ev):
        name = event_type(ev)
        if name == "realm_job":
            if not self.on_job(ev):
                return
        elif name == "guard" and ev.action == "his_kick":
            # App ordinary handlers run outside Store.transaction(). This local
            # write is deliberately allowed even while Realm is disabled.
            store.record_kick(self.ports.conn, ev.guid, ev.ts / 1000, ev.rank)
        if not self.enabled or self._stopping:
            return
        generation = self._generation
        self.ports.observe(ev, name)
        for part, names in tuple(self._parts):
            if generation != self._generation or not self.enabled or self._stopping:
                return
            if name in names:
                await self._owned(lambda part=part: part.on_event(ev))

    async def tick(self):
        """Advance real registered parts and the ports-owned speech queue once."""
        if not self.enabled or self._stopping:
            return
        generation = self._generation
        for part, _ in tuple(self._parts):
            if generation != self._generation or not self.enabled or self._stopping:
                return
            await self._owned(part.tick)
        if generation == self._generation and self.enabled and not self._stopping:
            await self.ports.speech_tick()

    async def run(self):
        if self._stopping:
            return
        if self._running:
            raise RuntimeError("realm already running")
        self._running = True
        try:
            while not self._stopping:
                await self.tick()
                await self.app.clock.sleep(1)
        finally:
            await self.close()
            self._running = False

    async def close(self):
        if self._close_task is None:
            self._stopping = True
            self._set_enabled(False)
            self._close_task = asyncio.create_task(self._drain())
        await asyncio.shield(self._close_task)

    async def _drain(self):
        if self._children:
            await asyncio.gather(*tuple(self._children), return_exceptions=True)
        # Ports joins already-entered shielded transport settlement, including
        # ACK callbacks, before app closes its bridge and database.
        await self.ports.close()

    def on_send(self, cmd):
        """Enroll immediately before Bridge.send; may deny before dispatch."""
        if not self.enabled or self._stopping:
            raise RealmDisabled("realm disabled")
        kind = next((name for name, cls in protocol.COMMANDS.items()
                     if type(cmd) is cls), None)
        if (kind is None or type(cmd.id) is not str or not cmd.id
                or len(cmd.id) > 128 or not cmd.id.isascii()
                or not all(c.isalnum() or c in "_-" for c in cmd.id)
                or cmd.id in self._pending or cmd.id in self._completed):
            raise ValueError("invalid realm command identity")
        if len(self._pending) >= self.CAPACITY:
            raise RuntimeError("realm command capacity exhausted")
        self._pending[cmd.id] = _Sent(cmd, kind, kind in JOB_COMMANDS)

    def _finish(self, command_id):
        sent = self._pending.pop(command_id)
        self._completed[command_id] = sent
        while len(self._completed) > self.COMPLETED:
            self._completed.popitem(last=False)

    def on_ack(self, cmd, ack):
        """Short synchronous local bookkeeping, including after local disable."""
        sent = self._pending.get(cmd.id)
        if (sent is None or sent.command is not cmd or sent.ack or ack.id != cmd.id
                or type(ack.ok) is not bool or type(ack.error) is not str):
            return
        store.record_receipt(self.ports.conn, command_id=cmd.id, kind=sent.kind,
                             stage="ack", ok=ack.ok, reason=ack.error,
                             ts=self.ports.now())
        sent.ack = True
        if not ack.ok or not sent.expects_job or sent.job:
            self._finish(cmd.id)

    def on_send_error(self, cmd, error_type):
        sent = self._pending.get(cmd.id)
        if sent is not None and sent.command is cmd and error_type == "NotConnected":
            # This Bridge error proves writer.write was never entered. Timeouts,
            # connection loss and unknown errors retain uncertain enrollment.
            del self._pending[cmd.id]

    def on_job(self, ev):
        # calendar_post can publish an uncertain/failed result and return a
        # negative ACK. The ACK frees pending capacity, while bounded completed
        # identity evidence still permits its later matching job in either order.
        sent = self._pending.get(ev.id) or self._completed.get(ev.id)
        if sent is None or not sent.expects_job or sent.job or ev.kind != sent.kind:
            return False
        owner = getattr(sent.command, "pvp_owner", "")
        if owner:
            if ev.pvp_owner != owner:
                return False
        elif ev.pvp_owner and sent.kind not in _PVP_ORIGINS:
            return False
        store.record_receipt(self.ports.conn, command_id=ev.id, kind=sent.kind,
                             stage="job", ok=ev.state == "done", reason=ev.reason,
                             value=ev.value, ts=ev.ts / 1000)
        sent.job = True
        if sent.ack and ev.id in self._pending:
            self._finish(ev.id)
        return True


def attach_realm(app) -> Realm:
    """Always attach the foundation, without sockets, tasks, MySQL or models."""
    protocol.register()
    realm = Realm(app)
    app.skip_event_rows |= protocol.NOISY
    # Presence switches preempt before app session startup can await a model.
    for name in ("hello", "login"):
        app.register_event_handler(name, realm.on_presence, before=True)
    for name in (*protocol.EVENTS, "hello", "login", "logout", "boss", "group"):
        realm._register_event(name)
    app.register_background(realm.run)
    app.register_shutdown_receipt("realm_job", realm.on_job)
    app.register_shutdown_drain(realm.close)
    return realm
