"""Realm lifecycle, durable admission and bounded receipt diagnostics.

The action journal and BodyLedger are ownership authority. Receipt rows remain
observations; direct no-body diagnostics have their own bounded correlation. Later
features attach as parts (parts.py); a command the direct part prepared is its own.
"""

import asyncio
from collections import OrderedDict
from dataclasses import dataclass
import logging
import uuid

from leeroy_mind.bodies import BodyLedger
from leeroy_mind.bridge import TransportState
from leeroy_mind.config import ConfigError
from leeroy_mind.protocol import BgEnd, Chat, event_type
from leeroy_mind.talk.turns import TurnKind

from . import protocol, store
from .action_policy import classify
from .action_store import ActionStore
from .actions import ActionContext, ActionQueue
from .announce import Announcer
from .config import load_realm_config
from .fact_store import FactStore, FactWriteRejected
from .guard import FALLBACK, FALLBACK_RETRY_S, FALLBACK_TRIES, Admission, KickGuard
from .kill_retention import PrivateKillRejected, PrivateKillStore
from .ledger import Ledger, actor_exclusions_in_unit
from .parts import build_parts, install_night
from .ports import LivePorts, RealmDisabled
from .provenance import cannot_qualify, capture_qualification_in_unit
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
# GuildReadmit is, but only the kick guard's own prepared admission passes its barrier.
_DIRECT = frozenset({protocol.RealmActors, protocol.RealmSay, protocol.RealmEmote,
                     protocol.CalendarUpdate, protocol.BankMoney,
                     protocol.GuildRename, protocol.ItemCount, protocol.GuildReadmit})
_SOURCES = frozenset({BgEnd, protocol.ArenaEnd, protocol.Wg, protocol.PvpKill,
                      protocol.ChannelChat, protocol.RealmKill, protocol.RealmAchievement,
                      protocol.DuelEnd, protocol.RealmDungeonKill})
_GUARD_ALERTS = frozenset({"gone", "blocked"})
_U32 = 0xFFFFFFFF
ACTORS_MAX, GUILDS_MAX = 16, 3  # Native's own bounds on one actor set.
ACTORS_RETRY_S = 60  # An actor set that never reached the wire is tried again after this.


def _kind(cmd):
    return next((kind for kind, cls in protocol.COMMANDS.items() if type(cmd) is cls), "direct")


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
    SPAWN_CAPACITY = 64

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
        self.parts = {}
        self._actors_ref = None  # "hello:<connection_epoch>" whose actor set went out
        self._actors_due = 0.0
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
        self.guard = KickGuard(app.store, self.ports, process_id=self._process_id, clock=app.clock)
        self._asking = None
        # Recovery is local bookkeeping, exactly once, before the source and
        # app attachment exist. Prepared payloads are never replayed.
        self.action_store.recover(process_id=self._process_id, now=self.ports.now())
        self.announcer.recover(process_id=self._process_id, now=self.ports.now())
        self.guard.recover(process_id=self._process_id)
        self.bodies = BodyLedger(app.clock, run_proof=self.ports.run_body_proof)
        self.bodies.add_holder_source("raid", self.ports.raid_body_holds)
        self.bodies.add_holder_source("offline_raid", self.ports.offline_body_holds)
        self.bodies.add_holder_source("training", self.ports.training_body_holds)
        self.bodies.add_holder_source("realm_actions", self.action_store.holds)
        self.queue = ActionQueue(self.ports.send, self.bodies, app.clock,
                                 store=self.action_store, context=self.action_context,
                                 actor_probe=self._actor_probe)
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

    def part(self, name):
        """The attached part of that name (parts.PART_MODULES), or None while it is absent."""
        return self.parts.get(name)

    @property
    def race(self):
        """The race part, as Training's benchmark gate reads it (app.realm.race); None while absent."""
        return self.part("race")

    def spawn(self, fn):
        """Start fn(), an async callable, as an owned child nobody awaits; returns its task.

        It starts only while the Realm is enabled, runs only while this generation and his
        identity hold, is cancelled by every invalidation and drained by close, as the
        announcer's sends are. Its failure is logged by type only. None when it did not start.
        """
        if not callable(fn):
            raise TypeError("spawn takes an async callable")
        valid = self._valid_ticket()
        if not valid():
            return None
        if len(self._children) >= self.SPAWN_CAPACITY:
            log.warning("realm_spawn_capacity")
            return None

        async def child():
            if valid():
                await fn()

        def done(task):
            self._children.discard(task)
            if not task.cancelled() and task.exception() is not None:
                log.warning("realm_spawn_failed: %s", type(task.exception()).__name__)

        task = asyncio.create_task(child())
        self._children.add(task)
        task.add_done_callback(done)
        return task

    def actor_set(self):
        """The complete actor set the rows hold, as one RealmActors for native; also the queue's
        realm_off probe. Active realm_actor rows (gankers among them) and active rival guilds,
        never him, a guild member, a former member or anyone he ever kicked (native would
        refuse the whole set for one such guid). None when the rows exceed native's bounds."""
        store = self.app.store
        human = self.ports.human().guid
        human = human if type(human) is int and 0 <= human <= _U32 else 0
        with store.transaction():
            excluded = actor_exclusions_in_unit(store, human_guid=human)
            roster = {row[0] for row in store.conn.execute("SELECT guid FROM cast_member")}
            actors = store.conn.execute(
                "SELECT guid, role FROM realm_actor WHERE status='active' ORDER BY guid").fetchall()
            guilds = [row[0] for row in store.conn.execute(
                "SELECT id FROM realm_guild WHERE status='active' ORDER BY id")]
        guids, gankers = [], []
        for guid, role in actors:
            if (type(guid) is not int or not 0 < guid <= _U32 or guid == human or guid in roster
                    or guid in excluded):
                log.warning("realm_actor_excluded guid=%s", guid if type(guid) is int else "?")
                continue
            guids.append(guid)
            if role == "ganker":
                gankers.append(guid)
        guilds = [g for g in guilds if type(g) is int and 0 < g <= _U32 and g != self._guild]
        if len(guids) > ACTORS_MAX or len(guilds) > GUILDS_MAX:
            log.warning("realm_actors_invalid actors=%d guilds=%d", len(guids), len(guilds))
            return None
        return protocol.RealmActors(uuid.uuid4().hex, tuple(guids), tuple(gankers), tuple(guilds))

    def _actor_probe(self):
        """actor_set, but never the queue's or the Realm loop's failure."""
        try:
            return self.actor_set()
        except Exception as error:
            log.warning("realm_actors_unavailable: %s", type(error).__name__)
            return None

    def _upkeep(self):
        """Once per valid hello, native's actor set (in memory only, so lost with each restart),
        from an owned child. A set with nobody in it is not sent: native starts with none."""
        if not self._presence_known or self._connection_epoch <= 0:
            return
        ref, now = f"hello:{self._connection_epoch}", self.ports.now()
        if self._actors_ref != ref and now >= self._actors_due:
            self._actors_ref = ref
            cmd = self._actor_probe()
            if cmd is not None and (cmd.guids or cmd.guilds):
                task = self.spawn(lambda: self.ports.send(cmd))
                if task is None:
                    self._actors_ref, self._actors_due = None, now + ACTORS_RETRY_S
                else:
                    task.add_done_callback(lambda done: self._actors_settled(done, cmd, ref))

    def _actors_settled(self, task, cmd, ref):
        if self._actors_ref == ref and cmd.id not in self._pending and cmd.id not in self._completed:
            # Never enrolled, so never on the wire: a later tick of this hello sends it again,
            # after a pause when it failed rather than being cancelled.
            failed = not task.cancelled() and task.exception() is not None
            self._actors_ref = None
            self._actors_due = self.ports.now() + ACTORS_RETRY_S if failed else 0.0

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
        refused = cannot_qualify(ev)
        if refused is not None:
            return refused
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
                    # A 5-man kill is a fact or nothing: never retained privately, never announced.
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

    # -- the kick guard: his kick, the lead's question, his answer, one readmission -------------

    def _guard_receipt(self, ev):
        """The dispatcher's exact row receipt and the current context, before any await."""
        receipt_reader = getattr(self.app, "row_receipt", None)
        receipt = receipt_reader(ev) if receipt_reader is not None else None
        if (receipt is None or self.app.bridge.delivery_origin(ev) is not receipt.wire
                or self._delivery_epoch(ev) != receipt.wire.transport_epoch):
            return None, None
        context = self.action_context()
        if not self._source_context_current(context, receipt.wire.transport_epoch):
            return None, None
        return receipt, context

    def _retain_kick(self, ev):
        receipt, context = self._guard_receipt(ev)
        if receipt is not None:
            self.guard.retain(ev, receipt, context)

    def _answer(self, ev):
        if type(ev) is not Chat or ev.channel != "whisper" or ev.via_bridge:
            return
        receipt, context = self._guard_receipt(ev)
        if receipt is None:
            return
        result = self.guard.answer(ev, receipt, context)
        if type(result) is Admission:
            self._readmit(result)

    def _valid_ticket(self):
        generation, human = self._generation, self.ports.human().guid

        def valid():
            return (self.enabled and not self._stopping and generation == self._generation
                    and self.ports.human().guid == human)
        return valid

    def _lead_ready(self, guid):
        world = self.app.world
        return guid in getattr(world, "online", ()) and guid not in getattr(world, "afk", ())

    def _speakable(self):
        """What a guard whisper's barrier needs besides its own row, read synchronously: the
        live model's latch (app._send refuses every say while it is off, before the bridge)
        and his presence (native refuses a whisper to him offline). The barrier runs right
        before the Say is written, with no await between it and those checks or the bridge's
        own connection check, so a line it lets through cannot be held back before the wire;
        one it holds back never left, and is refused, not unknown."""
        return getattr(self.app, "model_ready", False) is True and self.ports.human_online() is True

    def _guard_tick(self):
        """Close stale windows, then reserve and ask at most one question at a time."""
        try:
            self.guard.sweep()
            if self._asking is not None or not self.ports.human_online():
                return
            intent = self.guard.reserve(self.action_context(), ready=self._lead_ready)
        except Exception as error:  # Never the realm loop's failure; startup recovery closes rows.
            log.warning("realm_guard_failed: %s", type(error).__name__)
            return
        if intent is not None:
            self._ask(intent)

    def _ask(self, intent):
        """The lead's one typed whisper, from its own owned child. Its row records every
        outcome; the barrier and the recorded hook make the send exact."""
        valid, guard = self._valid_ticket(), self.guard

        def before_transport():  # Held back here, the row stays undispatched: settled refused.
            return (valid() and self._speakable()
                    and guard.before_transport(intent, context=self.action_context()))

        def on_recorded(line_id, ack):
            guard.on_recorded(intent, line_id, ack)

        async def ask():
            if valid():
                await self.ports.say(intent.lead_guid, "whisper", intent.text, to_guid=intent.human_guid,
                                     kind=TurnKind.WHISPER, before_transport=before_transport,
                                     on_recorded=on_recorded)

        def done(task):
            self._children.discard(task)
            if self._asking is task:
                self._asking = None
            if not task.cancelled() and task.exception() is not None:
                log.debug("realm_guard_question_unsent: %s", type(task.exception()).__name__)
            try:
                guard.settle_question(intent)
            except Exception as error:  # Startup recovery makes a reserved row unknown.
                log.warning("realm_guard_question_close_failed: %s", type(error).__name__)

        task = asyncio.create_task(ask())
        self._children.add(task)
        self._asking = task
        task.add_done_callback(done)

    def _readmit(self, admission):
        """His yes: the one guild_readmit, then, on any refusal, the lead's plain whisper."""
        valid, guard = self._valid_ticket(), self.guard
        cmd = guard.admission_command(admission) if admission.state == "prepared" else None

        async def readmit():
            if cmd is not None:
                try:
                    if valid():
                        await self.ports.send(cmd)
                except Exception as error:
                    log.debug("realm_guard_readmit_unsent: %s", type(error).__name__)
                guard.abandon_admission(cmd)  # Only a row that never reached its barrier.
            state = guard.admission_state(admission.episode_id)
            if state is not None and state[0] == "refused":
                await self._fallback(admission, valid)

        def done(task):
            self._children.discard(task)
            if not task.cancelled() and task.exception() is not None:
                log.debug("realm_guard_readmit_failed: %s", type(task.exception()).__name__)
            if cmd is not None:
                try:
                    guard.abandon_admission(cmd)
                except Exception as error:  # Startup recovery refuses a prepared row.
                    log.warning("realm_guard_readmit_close_failed: %s", type(error).__name__)

        task = asyncio.create_task(readmit())
        self._children.add(task)
        task.add_done_callback(done)

    async def _fallback(self, admission, valid):
        """The lead tells him plainly to invite the member himself: one typed line, at most
        once on the wire, tried again only while no attempt has passed its barrier (held back
        there by the model latch or his absence, it never left)."""
        reached = False

        def once():
            nonlocal reached
            if reached or not valid() or not self._speakable():
                return False
            reached = True
            return True

        text = FALLBACK.format(name=admission.name)
        for attempt in range(FALLBACK_TRIES):
            if attempt:
                await self.app.clock.sleep(FALLBACK_RETRY_S)
            if reached or not valid():
                return
            await self.ports.say(admission.lead, "whisper", text, to_guid=admission.human,
                                 kind=TurnKind.REPLY_HUMAN, before_transport=once)

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
            # Synchronous, before any await: both read the dispatcher's own row receipt.
            if name == "guard" and ev.action == "his_kick":
                self._retain_kick(ev)
            elif name == "chat":
                self._answer(ev)
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
        try:
            self._upkeep()
        except Exception as error:  # Never the Realm loop's failure.
            log.warning("realm_upkeep_failed: %s", type(error).__name__)
        for part, _ in tuple(self._parts):
            if generation != self._generation or not self.enabled or self._stopping:
                return
            tick = getattr(part, "tick", None)
            if tick is not None:
                await self._owned(tick)
        if generation == self._generation and self.enabled and not self._stopping:
            self._guard_tick()
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
        if type(cmd) is protocol.WorldSay:
            from leeroy_mind.world.realm import allowed
            if not allowed(self.app.store,self.app.data_dir,self.ports.now(),cmd.guid,cmd.guild_id):
                raise RealmDisabled("world_speaker_unavailable")
        if type(cmd) is protocol.Announce:
            # Before the queue and the direct path: an unowned announcement is refused.
            self.announcer.before_send(cmd, context=self.action_context())
            return
        if self.queue.owns_command(cmd):
            self.queue.before_send(cmd)
            return
        direct = self.parts.get("direct")
        if direct is not None and direct.owns_command(cmd):
            # Its own committing barrier, last, so nothing refuses the command after it.
            direct.before_send(cmd, context=self.action_context())
            return
        if type(cmd) not in _DIRECT:
            raise ValueError("realm body command requires queue enrollment")
        classified = classify(cmd)
        if cmd.id in self._pending or cmd.id in self._completed:
            raise ValueError("invalid realm command identity")
        if len(self._pending) >= self.CAPACITY:
            raise RuntimeError("realm command capacity exhausted")
        if type(cmd) is protocol.GuildReadmit:
            # Last, so nothing refuses it after its row commits 'dispatched'.
            self.guard.before_admission(cmd, context=self.action_context())
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
        direct = self.parts.get("direct")
        if direct is not None and direct.on_ack(cmd, ack):
            if type(ack.ok) is bool and type(ack.error) is str:
                store.record_receipt(self.ports.conn, command_id=cmd.id, kind=_kind(cmd),
                                     stage="ack", ok=ack.ok, reason=ack.error, ts=self.ports.now())
            return
        if type(cmd) is protocol.GuildReadmit:
            self.guard.on_admission_ack(cmd, ack)  # Its receipt is recorded below, as any direct one.
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
        direct = self.parts.get("direct")
        if direct is not None and direct.on_send_error(cmd, error_type):
            return
        if type(cmd) is protocol.GuildReadmit:
            self.guard.on_admission_send_error(cmd, error_type)
        sent = self._pending.get(cmd.id)
        if sent is not None and sent.command is cmd and error_type == "NotConnected":
            del self._pending[cmd.id]

    def on_job(self, ev):
        """RealmJob authority and independent first-job diagnostics."""
        if self.queue.on_realm_job(ev):
            return self._queued_receipt(ev.id, stage="job", ok=ev.state == "done", reason=ev.reason,
                                        value=ev.value, ts=ev.ts / 1000)
        direct = self.parts.get("direct")
        if direct is not None and direct.on_job(ev):
            # The part reads the job's value (an event id, a count) itself; this is diagnostics.
            store.record_receipt(self.ports.conn, command_id=ev.id, kind=ev.kind, stage="job",
                                 ok=ev.state == "done", reason=ev.reason, value=ev.value, ts=ev.ts / 1000)
            return True
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
    for name in (*protocol.EVENTS, "job", "hello", "login", "logout", "boss", "group", "bg_end", "chat"):
        realm._register_event(name)
    app.register_connection_hook(realm.on_connection)
    app.register_background(realm.run)
    app.register_shutdown_receipt("realm_job", realm.on_job)
    app.register_shutdown_receipt("job", realm.on_bot_job)
    app.register_shutdown_receipt("realm_pvp", realm.on_realm_pvp)
    app.register_shutdown_drain(realm.close)
    build_parts(realm)
    if (direct := realm.parts.get("direct")) is not None:
        # Like the recoveries in Realm.__init__: once, before anything can send.
        direct.recover(process_id=realm._process_id)
    install_night(app, realm)
    return realm
