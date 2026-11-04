"""Realm lifecycle, durable admission and bounded receipt diagnostics.

The action journal and BodyLedger are ownership authority. Receipt rows remain
observations; direct no-body diagnostics have their own bounded correlation.
"""

import asyncio
from collections import OrderedDict
from dataclasses import dataclass
import logging
import uuid

from leeroy_mind.bodies import BodyLedger
from leeroy_mind.bridge import TransportState
from leeroy_mind.config import ConfigError
from leeroy_mind.protocol import BgEnd, event_type

from . import protocol, store
from .action_policy import classify
from .action_store import ActionStore
from .actions import ActionContext, ActionQueue
from .announce import Announcer
from .config import load_realm_config
from .fact_store import FactStore, FactWriteRejected
from .kill_retention import PrivateKillRejected, PrivateKillStore
from .ledger import Ledger
from .ports import LivePorts, RealmDisabled
from .provenance import capture_qualification_in_unit
from .recall import Recall
from .records import RealmObservation, Rejected

log = logging.getLogger(__name__)

JOB_COMMANDS = frozenset({
    "realm_move", "rpg_status", "leave_pvp", "rearm", "wg_join", "arena_queue",
    "duel", "craft", "equip", "set_profession", "transmog_set",
    "calendar_signup", "mail_send", "bank_deposit", "bank_withdraw",
    "bank_stock", "guild_grow", "arena_team", "item_count", "calendar_post",
})
# Announce is not here: only the announcer's own prepared row may reach the system channel.
_DIRECT = frozenset({protocol.RealmActors, protocol.RealmSay, protocol.RealmEmote,
                     protocol.CalendarUpdate, protocol.BankMoney,
                     protocol.GuildRename, protocol.ItemCount})
_SOURCES = frozenset({BgEnd, protocol.ArenaEnd, protocol.Wg, protocol.PvpKill,
                      protocol.ChannelChat, protocol.RealmKill})
_GUARD_ALERTS = frozenset({"gone", "blocked"})


@dataclass(slots=True)
class _Sent:
    command: object
    kind: str
    expects_job: bool
    ack: bool = False
    job: bool = False


class Realm:
    CAPACITY = 128
    COMPLETED = 256

    def __init__(self, app):
        self.app = app
        self._source_app = app
        self._source_store = app.store
        self._source_bridge = app.bridge
        self._facts = FactStore(app.store)
        self._private_kills = PrivateKillStore(app.store)
        self.cfg = load_realm_config(app.data_dir / "mind.toml")
        self.enabled = self.cfg.enabled
        self._generation = 0
        self._process_id = uuid.uuid4().hex
        self._connected = False
        self._presence_known = False
        self._connection_epoch = 0
        self._transport_epoch = 0
        self._guild = 0
        self._human = 0
        self._parts = []
        self._handlers = set()
        self._children = set()
        self._pending = OrderedDict()
        self._completed = OrderedDict()
        self._stopping = False
        self._running = False
        self._close_task = None
        self._queue_task = None
        self.ports = LivePorts(app, on_send=self.on_send, on_ack=self.on_ack,
                               on_send_error=self.on_send_error)
        self.ports.set_enabled(self.enabled)
        self.ledger = Ledger(app.store, self.ports, self._facts)
        self.action_store = ActionStore(self.ports.conn)
        self.announcer = Announcer(app.store, process_id=self._process_id, clock=app.clock)
        # Recovery is local bookkeeping, exactly once, before the source and
        # app attachment exist. Prepared payloads are never replayed.
        self.action_store.recover(process_id=self._process_id, now=self.ports.now())
        self.announcer.recover(process_id=self._process_id, now=self.ports.now())
        self.bodies = BodyLedger(app.clock, run_proof=self.ports.run_body_proof)
        self.bodies.add_holder_source("raid", self.ports.raid_body_holds)
        self.bodies.add_holder_source("offline_raid", self.ports.offline_body_holds)
        self.bodies.add_holder_source("training", self.ports.training_body_holds)
        self.bodies.add_holder_source("realm_actions", self.action_store.holds)
        self.queue = ActionQueue(self.ports.send, self.bodies, app.clock,
                                 store=self.action_store, context=self.action_context,
                                 actor_probe=None)
        self.recall = Recall(self.ports, self.queue, self.bodies, app.clock)
        self._raid_state = self.ports.raid_state()
        app.bodies = self.bodies

    def _transport(self):
        try:
            state = self.app.bridge.transport_state
        except AttributeError:
            return TransportState(0, False)
        if (type(state) is not TransportState or type(state.epoch) is not int or state.epoch < 0
                or type(state.connected) is not bool):
            return TransportState(0, False)
        return state

    def _delivery_epoch(self, value):
        try:
            epoch = self.app.bridge.delivery_epoch(value)
        except AttributeError:
            return None
        return epoch if type(epoch) is int and epoch > 0 else None

    def action_context(self):
        transport = self._transport()
        connected = (self._connected and transport.connected
                     and transport.epoch == self._transport_epoch)
        known = (connected and self._presence_known and self._human > 0
                 and self.app.confirmed_human_guid == self._human)
        return ActionContext(self.enabled and not self._stopping, connected, known,
                             self._generation, self._process_id, self._connection_epoch,
                             self._guild, self._human)

    def _invalidate(self):
        self._generation += 1
        for task in tuple(self._children):
            task.cancel()
        # Also fence ports-owned speech/model children before any presence await.
        self.ports.set_enabled(False)
        self.ports.set_enabled(self.enabled and not self._stopping)
        if hasattr(self, "queue"):
            self.queue.context_changed()
            self.recall.invalidate()

    def _set_enabled(self, enabled):
        enabled = enabled and not self._stopping
        changed = enabled != self.enabled
        self.enabled = enabled
        if changed or not enabled:
            self._invalidate()
        else:
            self.ports.set_enabled(enabled)

    def reload(self):
        """Synchronous switch reload; never establishes connection identity."""
        try:
            self.cfg = load_realm_config(self.app.data_dir / "mind.toml")
        except ConfigError:
            log.warning("realm_config_unavailable")
            self._set_enabled(False)
            return
        self._set_enabled(self.cfg.enabled)

    async def on_connection(self, up):
        if type(up) is not bool:
            raise ValueError("connection state must be bool")
        transport = self._transport()
        epoch = self._delivery_epoch(up)
        self._connected = bool(up and transport.connected and epoch == transport.epoch and epoch)
        self._presence_known = False
        if self._connected:
            if epoch != self._transport_epoch:
                self._connection_epoch += 1
            self._transport_epoch = epoch
        self._invalidate()

    async def on_presence(self, ev):
        name = event_type(ev)
        if name in {"login", "logout"} and self.app.is_backlog(ev):
            return
        if name == "hello":
            # Ordinary hello bookkeeping can await session/model work. Invalidate
            # every old ticket synchronously before that app row begins.
            self._presence_known = False
            self._invalidate()
            self.reload()
        elif name in {"login", "logout"} and ev.human:
            if ev.guid != self.app.human_guid:
                self._presence_known = False
                self._invalidate()
            if name == "login":
                self.reload()

    def _adopt_hello(self, ev):
        transport = self._transport()
        epoch = self._delivery_epoch(ev)
        human = self.app.confirmed_human_guid
        valid = (self._connected and transport.connected and epoch == transport.epoch
                 and epoch == self._transport_epoch and epoch is not None
                 and type(ev.guild_id) is int and 0 < ev.guild_id <= 0xFFFFFFFF
                 and type(human) is int and 0 < human <= 0xFFFFFFFF
                 and human == self.app.human_guid
                 and type(ev.human_guid) is int and 0 <= ev.human_guid <= 0xFFFFFFFF
                 and (ev.human_guid == 0 or ev.human_guid == human))
        self._guild = ev.guild_id if valid else 0
        self._human = human if valid else 0
        self._presence_known = valid
        self.queue.context_changed()
        if valid:
            self.queue.on_hello(ev)
            self.recall.on_hello(ev)

    def _register_event(self, name):
        if name not in self._handlers:
            self.app.register_event_handler(name, self.on_event)
            self._handlers.add(name)

    def add_part(self, part, *, event_types=()):
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
            return (self.enabled and not self._stopping and generation == self._generation
                    and self.ports.human().guid == human)

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

    def _source_context_current(self, context: ActionContext, transport_epoch: int) -> bool:
        """Check actual owners and writer, independently of queued connection hooks."""
        app = self.app
        if (app is not self._source_app or getattr(app, "realm", None) is not self
                or app.store is not self._source_store or app.bridge is not self._source_bridge
                or self._facts.store is not self._source_store
                or self._private_kills.store is not self._source_store
                or getattr(app, "stopping", None) is not False
                or not self.enabled or self._stopping or type(context) is not ActionContext):
            return False
        current = self.action_context()
        transport = self._transport()
        return (current == context
                and all(getattr(current, key) is True for key in ("enabled", "connected", "presence_known"))
                and all(type(getattr(current, key)) is int and getattr(current, key) > 0
                        for key in ("guild", "human", "connection_epoch"))
                and app.confirmed_human_guid == current.human == app.human_guid
                and self.ports.human().guid == current.human
                and type(transport_epoch) is int and transport_epoch > 0
                and transport.connected and transport.epoch == self._transport_epoch == transport_epoch)

    def _capture_source_observation(self, ev):
        """Capture the exact dispatcher receipt before creating any owned child."""
        if type(ev) not in _SOURCES:
            return None
        receipt_reader = getattr(self.app, "row_receipt", None)
        if receipt_reader is None:
            return Rejected("invalid_input")
        receipt = receipt_reader(ev)
        if (receipt is None or self.app.bridge.delivery_origin(ev) is not receipt.wire
                or self._delivery_epoch(ev) != receipt.wire.transport_epoch):
            return Rejected("invalid_input")
        with self._source_store.transaction():
            context = self.action_context()
            if not self._source_context_current(context, receipt.wire.transport_epoch):
                return Rejected("proof_changed")
            qualification = capture_qualification_in_unit(self._source_store,
                human=self.ports.human(), event=ev, receipt=receipt, context=context,
                confirmed_human_guid=self.app.confirmed_human_guid,
                transport_epoch=self._transport().epoch, captured_at=self.app.clock.now())
            if type(qualification) is Rejected:
                return qualification
            return RealmObservation(ev, receipt, context, qualification)

    async def _on_source_observation(self, observation):
        # There is deliberately no await from the final current-state barrier
        # through reducer validation and commit of the original Store unit.
        try:
            with self._source_store.transaction():
                if not self._source_context_current(observation.context,
                                                    observation.receipt.wire.transport_epoch):
                    return Rejected("proof_changed")
                human = self.ports.human()
                ev = observation.event
                if type(ev) is not protocol.RealmKill:
                    return self._facts.put_fact_in_unit(observation, human=human)
                existing = self._source_store.conn.execute(
                    "SELECT fact FROM realm_kill WHERE kill_id=?", (ev.kill_id,)).fetchone()
                public = (existing["fact"] is not None if existing is not None else
                          ev.human_present or len(set(ev.cast_members) & set(observation.qualification.cast)) >= 5)
                if not public:
                    return self._private_kills.record_in_unit(observation, human=human)
                fact = self._facts.record_verified_kill_in_unit(observation, human=human)
                # The announcement decision commits with the kill's own fact.
                plan = self.announcer.prepare_in_unit(observation, fact)
        except (FactWriteRejected, PrivateKillRejected) as error:
            # Semantic exceptions must leave the complete Store unit first.
            return error.rejection
        if plan is not None:
            self._announce(plan)
        return fact

    def _announce(self, plan):
        """Send one committed plan from its own owned child, so event dispatch never waits on
        the native ack. Its row records every outcome; nothing is ever sent again."""
        generation, human = self._generation, self.ports.human().guid
        cmd = self.announcer.command(plan)

        async def send():
            if (self.enabled and not self._stopping and generation == self._generation
                    and self.ports.human().guid == human):
                await self.ports.send(cmd)

        def done(task):
            self._children.discard(task)
            if not task.cancelled() and task.exception() is not None:
                log.debug("realm_announcement_unsent: %s", type(task.exception()).__name__)
            # Cancelled before it started, disabled or refused: the prepared row is closed.
            try:
                self.announcer.abandon_unsent(cmd, now=self.ports.now())
            except Exception as error:  # Never sent either way; startup recovery closes it.
                log.warning("realm_announcement_close_failed: %s", type(error).__name__)

        task = asyncio.create_task(send())
        self._children.add(task)
        task.add_done_callback(done)

    async def _admit_source_observation(self, observation):
        result = await self._on_source_observation(observation)
        if type(result) is Rejected:
            log.debug("realm_source_rejected: type=%s reason=%s",
                      event_type(observation.event), result.reason)

    async def on_event(self, ev):
        name = event_type(ev)
        # Authority reducers precede both local gates and first-receipt diagnostic
        # dedup. In particular Wg enrollment success and later failure both run.
        if name == "realm_job":
            self.on_job(ev)
        elif name == "job":
            self.on_bot_job(ev)
        elif name == "realm_pvp":
            self.on_realm_pvp(ev)
        elif name == "guard" and ev.action == "his_kick":
            store.record_kick(self.ports.conn, ev.guid, ev.ts / 1000, ev.rank)
        elif name == "guard":
            self._guard_alert(ev)
        elif name == "hello" and not self._stopping:
            self._adopt_hello(ev)
        if name in {"login", "logout"} and self.app.is_backlog(ev):
            return
        if not self.enabled or self._stopping:
            return
        if name == "login" and ev.human and ev.guid == self._human and self._presence_known:
            self.recall.on_login(self.ports.now())
        generation = self._generation
        self.ports.observe(ev, name)
        failure = None
        try:
            observation = self._capture_source_observation(ev)
            if type(observation) is RealmObservation:
                await self._owned(lambda: self._admit_source_observation(observation))
            elif type(observation) is Rejected:
                log.debug("realm_source_rejected: type=%s reason=%s", name, observation.reason)
        except Exception as error:
            # A database or programming fault stays this event's failure, raised
            # only after the generic parts have still seen the event.
            failure = error
        try:
            for part, names in tuple(self._parts):
                if generation != self._generation or not self.enabled or self._stopping:
                    break
                if name in names:
                    await self._owned(lambda part=part: part.on_event(ev))
        finally:
            if failure is not None:
                raise failure

    @staticmethod
    def _guard_alert(ev):
        """A cast member the guard could not keep; ids only, also while disabled."""
        if (type(ev.action) is str and ev.action in _GUARD_ALERTS
                and type(ev.guid) is int and 0 < ev.guid <= 0xFFFFFFFF):
            log.warning("realm_guard_alert guid=%d action=%s", ev.guid, ev.action)

    async def tick(self):
        # Retained reservations and provider releases continue to be observed
        # while disabled. Callbacks still cannot admit local-disabled work.
        self.bodies.poll()
        if not self.enabled or self._stopping:
            return
        state = self.ports.raid_state()
        self.recall.on_raid_state(self._raid_state, state)
        self._raid_state = state
        self.recall.tick(self.ports.now())
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
        self._queue_task = asyncio.create_task(self.queue.run())
        try:
            while not self._stopping:
                if self._queue_task.done():
                    self._queue_task.result()
                    raise RuntimeError("realm action queue stopped")
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
        await self.queue.close()
        if self._queue_task is not None:
            await self._queue_task
        # Actual settlement callbacks remain active while both drains wait.
        await self.ports.close()

    def on_send(self, cmd):
        if not self.enabled or self._stopping:
            raise RealmDisabled("realm disabled")
        if type(cmd) is protocol.Announce:
            # Before the queue and the direct path: an unowned announcement is refused.
            self.announcer.before_send(cmd, context=self.action_context())
            return
        if self.queue.owns_command(cmd):
            self.queue.before_send(cmd)
            return
        if type(cmd) not in _DIRECT:
            raise ValueError("realm body command requires queue enrollment")
        classified = classify(cmd)
        if cmd.id in self._pending or cmd.id in self._completed:
            raise ValueError("invalid realm command identity")
        if len(self._pending) >= self.CAPACITY:
            raise RuntimeError("realm command capacity exhausted")
        self._pending[cmd.id] = _Sent(cmd, classified.kind, classified.kind in JOB_COMMANDS)

    def _finish(self, command_id):
        sent = self._pending.pop(command_id)
        self._completed[command_id] = sent
        while len(self._completed) > self.COMPLETED:
            self._completed.popitem(last=False)

    def _queued_receipt(self, command_id, *, stage, ok, reason, ts, value=0):
        record = self.action_store.get(command_id)
        if record is not None:
            return store.record_receipt(self.ports.conn, command_id=command_id, kind=record.kind,
                                        stage=stage, ok=ok, reason=reason, value=value, ts=ts)

    def on_ack(self, cmd, ack):
        if type(cmd) is protocol.Announce:
            now = self.ports.now()
            if self.announcer.on_ack(cmd, ack, now=now):
                store.record_receipt(self.ports.conn, command_id=cmd.id, kind="announce",
                                     stage="ack", ok=ack.ok, reason=ack.error, ts=now)
            return
        if self.queue.on_ack(cmd, ack):
            self._queued_receipt(cmd.id, stage="ack", ok=ack.ok, reason=ack.error, ts=self.ports.now())
            return
        sent = self._pending.get(cmd.id)
        if (sent is None or sent.command is not cmd or sent.ack or ack.id != cmd.id
                or type(ack.ok) is not bool or type(ack.error) is not str):
            return
        store.record_receipt(self.ports.conn, command_id=cmd.id, kind=sent.kind,
                             stage="ack", ok=ack.ok, reason=ack.error, ts=self.ports.now())
        sent.ack = True
        if not ack.ok or not sent.expects_job or sent.job:
            self._finish(cmd.id)

    def on_send_error(self, cmd, error_type):
        if type(cmd) is protocol.Announce:
            self.announcer.on_send_error(cmd, error_type, now=self.ports.now())
            return
        if self.queue.on_send_error(cmd, error_type):
            return
        sent = self._pending.get(cmd.id)
        if sent is not None and sent.command is cmd and error_type == "NotConnected":
            del self._pending[cmd.id]

    def on_job(self, ev):
        """RealmJob authority and independent first-job diagnostics."""
        if self.queue.on_realm_job(ev):
            return self._queued_receipt(ev.id, stage="job", ok=ev.state == "done", reason=ev.reason,
                                        value=ev.value, ts=ev.ts / 1000)
        sent = self._pending.get(ev.id) or self._completed.get(ev.id)
        if (sent is None or not sent.expects_job or sent.job or ev.kind != sent.kind
                or ev.pvp_owner):
            return False
        # ItemCount is the sole direct no-body command which emits a result.
        if ev.guid != sent.command.guid:
            return False
        store.record_receipt(self.ports.conn, command_id=ev.id, kind=sent.kind,
                             stage="job", ok=ev.state == "done", reason=ev.reason,
                             value=ev.value, ts=ev.ts / 1000)
        sent.job = True
        if sent.ack and ev.id in self._pending:
            self._finish(ev.id)
        return True

    def on_bot_job(self, ev):
        if not self.queue.on_job(ev):
            return False
        return self._queued_receipt(ev.id, stage="job", ok=ev.state == "done" and not ev.skipped,
                                    reason=ev.reason, ts=ev.ts / 1000)

    def on_realm_pvp(self, ev):
        return self.queue.on_realm_pvp(ev)


def attach_realm(app) -> Realm:
    """Attach recovered local ownership without sockets, tasks or models."""
    protocol.register()
    realm = Realm(app)
    app.skip_event_rows |= protocol.NOISY - {"channel_chat"}
    app.skip_event_rows.discard("channel_chat")
    for name in ("hello", "login", "logout"):
        app.register_event_handler(name, realm.on_presence, before=True)
    for name in (*protocol.EVENTS, "job", "hello", "login", "logout", "boss", "group", "bg_end"):
        realm._register_event(name)
    app.register_connection_hook(realm.on_connection)
    app.register_background(realm.run)
    app.register_shutdown_receipt("realm_job", realm.on_job)
    app.register_shutdown_receipt("job", realm.on_bot_job)
    app.register_shutdown_receipt("realm_pvp", realm.on_realm_pvp)
    app.register_shutdown_drain(realm.close)
    return realm
