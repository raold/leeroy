"""Journaled per-member tactic layers with conservative, retryable cleanup.

No hooks are installed here. The integration supplies current immutable state on
every boundary; a hello is not evidence that the game's mutation journal reset.
"""

import asyncio
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
import json
import logging
import math
import re
from types import MappingProxyType
from typing import Literal
from uuid import uuid4

from leeroy_mind.guild.protocol import BotDo, Job, RaidControl
from leeroy_mind.protocol import Ack

from . import store
from .cards import Card, Directive, Targets, validate_card, validate_directive
from .levers import Applied, MemberInfo, to_commands
from .protocol import CustomStrategy, DcSetting
from .vocab import Vocab


log = logging.getLogger(__name__)
Layer = Literal["standing", "pooled", "boss", "arm"]
_RANK = {"pooled": 0, "standing": 1, "boss": 2, "arm": 3}
_TYPES = {c.__name__: c for c in (BotDo, CustomStrategy, DcSetting, RaidControl)}
_DRIFT = {"not_primed", "no_target", "failed", "unknown_strategy", "unknown_action",
          "not_in_vocab", "bad_value", "bad_key", "bad_lines"}
_ROTATION_NAME = re.compile(r"tg_[a-z0-9_]{1,40}\Z")
_CONTEXT = re.compile(r"(0|[1-9][0-9]{0,9}):(0|[1-9][0-9]{0,9}):[0-3]\Z")
_WAIT_S = 5.0


@dataclass(frozen=True, slots=True)
class ApplierState:
    run_key: str | None
    session_key: str
    instance_id: int | None
    connection_epoch: int
    tank_guid: int
    lead_is_human: bool
    members: Mapping[int, MemberInfo]
    online: frozenset[int]
    vocab: Vocab
    # None certifies that the integration has no body arbiter installed. An
    # installed arbiter with unknown ownership must yield no state snapshot.
    body_owner: str | None = None

    def __post_init__(self):
        object.__setattr__(self, "members", MappingProxyType(dict(self.members)))
        object.__setattr__(self, "online", frozenset(self.online))


@dataclass
class _Receipt:
    id: int
    envelope: dict
    acked: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task | None = None
    settled: bool = False
    ok: bool = False
    reason: str = "uncertain"
    skipped: bool = False
    provider: object = None
    run: str = ""


@dataclass
class _Entry:
    run: str
    layer: Layer
    context: str
    key: tuple[int, str]
    commands: tuple[Applied, ...]
    serial: int
    scope: dict
    member: MemberInfo
    directives: tuple[Directive, ...]
    receipts: list[int] = field(default_factory=list)


def _encode(command):
    return None if command is None else {"type": type(command).__name__, "data": asdict(command)}


def _decode(value):
    if value is None:
        return None
    cls = _TYPES[value["type"]]
    data = dict(value["data"])
    for key in ("guids", "lines"):
        if key in data:
            data[key] = tuple(data[key])
    return cls(**data)


def _premutation_refusal(command, error):
    """Only errors emitted before mutation by the consumed C++ handlers."""
    if isinstance(command, BotDo):
        return error in {"disabled", "training_off", "busy"} or bool(re.fullmatch(r"not_cast:[0-9]+", error))
    if isinstance(command, DcSetting):
        # In particular, GetRun's `busy` can follow partial old-run cleanup.
        return error in {"guild_off", "training_off", "dc_unavailable", "not_leader", "restore_ownership"}
    if isinstance(command, CustomStrategy):
        return error in {"training_off", "name_taken", "busy"} or error.startswith(
            ("no_bot:", "unknown_trigger:", "unknown_action:", "not_spell:"))
    if isinstance(command, RaidControl):
        # dc_refused can follow DoSpecificAction; it is deliberately excluded.
        return error in {"no_group", "not_leader", "driver_unavailable:no_dungeon_clear"} or bool(
            re.fullmatch(r"(?:not_cast|leader_mismatch):[0-9]+", error))
    return False


def _command_guids(command):
    if isinstance(command, BotDo):
        return command.guids
    if isinstance(command, DcSetting):
        return (command.leader_guid,)
    if isinstance(command, RaidControl):
        return tuple(sorted({g for g in (command.member_guid, command.leader_guid) if g}))
    if isinstance(command, CustomStrategy):
        return (command.probe,)
    return ()


class Applier:
    def __init__(self, ports, ledger, *, state_provider=None, application_allowed=None):
        self.ports, self.ledger = ports, ledger
        self.state_provider = state_provider
        self.application_allowed = application_allowed
        self._lock = asyncio.Lock()
        self._entries: list[_Entry] = []
        self._receipts: dict[int, _Receipt] = {}
        self._blocked: set[str] = set()
        self._stopped: set[str] = set()
        self._claims: dict[str, set[int]] = {}
        self._scopes: dict[str, dict] = {}
        self._serial = 0
        self._hello = 0
        self._primed: set[tuple[int, str]] = set()
        self._closing = False
        self._operations: dict[str, int] = {}

    @contextmanager
    def _operation(self, run):
        self._operations[run] = self._operations.get(run, 0) + 1
        try:
            yield
        finally:
            self._operations[run] -= 1
            if not self._operations[run]:
                self._operations.pop(run)

    def cleanup_pending(self, run_key):
        """True until this exact run has no owned or possibly active mutation."""
        return bool(self._rows(run_key) or run_key in self._scopes or run_key in self._blocked
            or run_key in self._claims or self._operations.get(run_key)
            or any(e.run == run_key for e in self._entries)
            or any(r.run == run_key and r.task is not None and not r.task.done()
                   for r in self._receipts.values()))

    def acting(self, now):
        try:
            if self.application_allowed is not None and self.application_allowed() is not True:
                return False
        except Exception:
            return False
        first = self.ledger.first_pull_ts()
        return (self.ports.cfg.act is True and type(first) in (int, float)
                and type(now) in (int, float) and math.isfinite(first) and math.isfinite(now)
                and 0 <= first <= now and now - first >= max(28, self.ports.cfg.observe_days) * 86400)

    def _state(self, run):
        if self._closing:
            return None
        state = self.state_provider(run) if self.state_provider is not None else None
        if (not isinstance(state, ApplierState) or run is not None and state.run_key != run
                or not state.session_key):
            return None
        return state

    @staticmethod
    def _scope(state):
        return dict(run_key=state.run_key, session_key=state.session_key,
                    instance_id=state.instance_id, connection_epoch=state.connection_epoch,
                    body_owner=state.body_owner, tank_guid=state.tank_guid,
                    lead_is_human=state.lead_is_human)

    def _matches(self, scope, *, epoch=True):
        state = self._state(scope["run_key"])
        return state is not None and all(self._scope(state)[k] == v for k, v in scope.items()
                                        if epoch or k != "connection_epoch")

    def _rows(self, run=None):
        # Old unresolved mutations are not safe to forget after an arbitrary TTL.
        return [r for r in store.open_applied(self.ports.conn, 0) if run is None or r["run"] == run]

    def _top(self, run, key):
        rows = [e for e in self._entries if e.run == run and e.key == key]
        return max(rows, key=lambda e: (_RANK[e.layer], e.serial), default=None)

    def _available(self, state, guids, *, cleanup=False):
        if not isinstance(state, ApplierState) or not set(guids) <= state.online or self.ports.human_guid in guids:
            return False
        for guid in guids:
            holder = self.ports.holder(guid)
            expected = "training" if state.run_key is None and state.body_owner is not None else state.body_owner
            if holder != expected:
                return False
        return True

    def _write_receipt(self, receipt):
        receipt.envelope["settled"] = receipt.settled
        with self.ports.conn:
            self.ports.conn.execute("UPDATE tg_applied SET command=? WHERE id=?",
                (json.dumps(receipt.envelope, separators=(",", ":")), receipt.id))

    def _resume_allowed(self, state, run, *, exclude=0):
        if (state is None or state.run_key != run or not self._matches(self._scope(state))
                or not self.acting(self.ports.clock.now()) or self._blocked or run in self._stopped
                or not self._available(state, (state.tank_guid,))):
            return False
        rows = [row for row in self._rows() if row["id"] != exclude]
        try:
            if any(row["id"] not in self._receipts or not self._receipts[row["id"]].ok
                   or not json.loads(row["command"]).get("settled")
                   for row in rows):
                return False
        except (ValueError, TypeError):
            return False
        active = {row["id"] for row in rows if row["run"] == run}
        for ident in active:
            receipt = self._receipts[ident]
            command = _decode(receipt.envelope["wire"])
            if (receipt.ok and receipt.settled and receipt.envelope["scope"] == self._scope(state)
                    and isinstance(command, DcSetting) and command.op == "set"
                    and command.key == "WaitAtBoss" and command.centi == 100
                    and command.leader_guid == state.tank_guid and state.tank_guid in state.members
                    and receipt.envelope["members"].get(str(state.tank_guid)) == asdict(state.members[state.tank_guid])):
                return True
        return False

    async def _perform(self, receipt, command):
        """Survives caller timeout/cancellation; only terminal evidence settles it."""
        try:
            scope = receipt.envelope["scope"]
            state = self._state(scope["run_key"])
            guids = _command_guids(command)
            if (receipt.provider is not self.state_provider or not self._matches(scope)
                    or not self._available(state, guids)
                    or any(g not in state.members or asdict(state.members[g]) != receipt.envelope["members"].get(str(g))
                           for g in guids)
                    or receipt.envelope.get("application", False) and not self.acting(self.ports.clock.now())
                    or receipt.envelope.get("resume_held", False)
                    and not self._resume_allowed(state, receipt.run, exclude=receipt.id)):
                # No transport call occurred. This local receipt is provably
                # unsent, unlike a negative acknowledgement from the server.
                receipt.settled, receipt.reason = True, "unsent"
                store.mark_undone(self.ports.conn, [receipt.id], self.ports.clock.now())
                return
            async with asyncio.timeout(_WAIT_S):
                ack = await self.ports.send(command)
            receipt.acked.set()
            if not isinstance(ack, Ack) or ack.id != command.id:
                return
            if type(ack.ok) is not bool:
                return
            if ack.ok and ack.error:
                return  # Contradictory evidence cannot certify non-mutation.
            if not ack.ok:
                receipt.reason = ack.error or "refused"
                if _premutation_refusal(command, receipt.reason):
                    receipt.settled = True
                    store.mark_undone(self.ports.conn, [receipt.id], self.ports.clock.now())
                elif isinstance(command, DcSetting) and (receipt.reason in {
                        "apply_failed", "restore_failed", "resume_failed", "busy", "override_changed", "bad_value"}
                        or receipt.reason.startswith("refused:")):
                    # These synchronous DC paths can fail after mutation. The
                    # handler has finished, but its inverse is still required.
                    receipt.settled = True
                return
            if isinstance(command, BotDo):
                async with asyncio.timeout(_WAIT_S):
                    job = await self.ports.wait_job(command.id, 5.0)
                if (not isinstance(job, Job) or job.id != command.id or job.kind != "bot_do"
                        or job.state not in {"done", "failed"}):
                    return
                targets = set(command.guids)
                seen = set(job.guids) | {s.guid for s in job.skipped}
                if (seen != targets or len(job.guids) != len(set(job.guids))
                        or len(job.skipped) != len({s.guid for s in job.skipped})
                        or set(job.guids) & {s.guid for s in job.skipped}):
                    return
                receipt.settled = True
                receipt.reason = next((s.reason for s in job.skipped
                    if s.reason not in {"offline", "dead"}), job.reason)
                receipt.skipped = bool(job.skipped)
                receipt.ok = job.state == "done" and not receipt.reason
                if job.skipped and not job.guids:
                    # An offline/dead *apply* did nothing, but is not a restore.
                    if command.action != "restore" and all(s.reason in {"offline", "dead"} for s in job.skipped):
                        store.mark_undone(self.ports.conn, [receipt.id], self.ports.clock.now())
            else:
                receipt.settled, receipt.ok, receipt.reason = True, True, ""
        except asyncio.CancelledError:
            # Shutdown does not erase a possibly transmitted mutation.
            raise
        except Exception:
            log.warning("training command completion unavailable")
        finally:
            receipt.acked.set()
            self._write_receipt(receipt)

    async def _send(self, run, layer, context, command, inverse, scope, keys=(), *,
                    resume_held=False, application=False):
        if not self._matches(scope):
            return _Receipt(0, {}, settled=True, reason="stale")
        command = replace(command, id="tg_" + uuid4().hex)
        state = self._state(scope["run_key"])
        envelope = dict(wire=_encode(command), scope=scope, keys=list(keys), settled=False,
                        resume_held=resume_held, application=application,
                        members={str(g): asdict(state.members[g]) for g in _command_guids(command)
                                 if state is not None and g in state.members})
        ident = store.log_applied(self.ports.conn, run, layer, context, envelope,
                                 _encode(inverse), self.ports.clock.now())
        receipt = self._receipts[ident] = _Receipt(ident, envelope, provider=self.state_provider, run=run)
        receipt.task = asyncio.create_task(self._perform(receipt, command))
        def finished(task):
            if not task.cancelled() and task.exception() is not None:
                log.warning("training command receipt persistence failed")
        receipt.task.add_done_callback(finished)
        try:
            await asyncio.wait_for(receipt.acked.wait(), _WAIT_S)
            await asyncio.wait_for(asyncio.shield(receipt.task), _WAIT_S)
        except TimeoutError:
            self._blocked.add(run)
        except asyncio.CancelledError:
            self._blocked.add(run)
            raise
        except Exception:
            self._blocked.add(run)
            raise
        if receipt.reason == "training_off":
            if run not in self._stopped:
                log.info("training_off_in_game")
            self._stopped.add(run)
        if not receipt.ok and any(row["id"] == ident for row in self._rows(run)):
            self._blocked.add(run)
        return receipt

    def _pending(self):
        rows = self._rows()
        active = {row["id"] for row in rows}
        for ident, receipt in tuple(self._receipts.items()):
            if ident not in active and receipt.task is not None and receipt.task.done():
                self._receipts.pop(ident)
        try:
            return any(not json.loads(row["command"]).get("settled", False) for row in rows)
        except (ValueError, TypeError):
            return True

    async def _activate(self, entry, *, application=True):
        hello = self._hello
        for applied in entry.commands:
            if not self._matches(entry.scope) or self._pending():
                return False, "stale"
            state = self._state(entry.scope["run_key"])
            if (not self._available(state, (entry.key[0],)) or state.members.get(entry.key[0]) != entry.member
                    or any(validate_directive(d, state.vocab) for d in entry.directives)):
                return False, "ownership"
            receipt = await self._send(entry.run, entry.layer, entry.context,
                applied.command, applied.inverse, entry.scope, (entry.key,), application=application)
            entry.receipts.append(receipt.id)
            if (hello != self._hello or not self._matches(entry.scope) or not receipt.settled or not receipt.ok
                    or not self._available(self._state(entry.scope["run_key"]), (entry.key[0],))):
                return False, receipt.reason
            if self._state(entry.scope["run_key"]).members.get(entry.key[0]) != entry.member:
                return False, "stale"
            if receipt.skipped:
                return True, "skipped"
        return True, ""

    async def apply(self, run_key, layer, context, card, members, *, tank_guid, lead_is_human):
        with self._operation(run_key):
            async with self._lock:
                return await self._apply(run_key, layer, context, card, members,
                                         tank_guid=tank_guid, lead_is_human=lead_is_human)

    async def _apply(self, run_key, layer, context, card, members, *, tank_guid, lead_is_human,
                     outside=False):
        state = self._state(None if outside else run_key)
        if (type(run_key) is not str or not run_key or type(context) is not str or not _CONTEXT.fullmatch(context)
                or layer not in _RANK or not isinstance(card, Card) or card.context != context
                or card.status not in {"candidate", "trial", "champion"}
                or not state or not self.acting(self.ports.clock.now())
                or self._blocked or run_key in self._stopped or self._pending()
                or tank_guid != state.tank_guid or lead_is_human != state.lead_is_human):
            return False
        if outside and state.run_key is not None:
            return False
        if any(r["id"] not in self._receipts for r in self._rows()):
            return False  # Restart recovery must precede any fresh mutation.
        if validate_card(card, state.vocab, names={m.name for m in self.ports.cast().values()},
                         human=self.ports.human()):
            return False
        if any(g not in state.members or state.members[g] != m for g, m in members.items()):
            return False
        scope = self._scope(state)
        if run_key in self._scopes and self._scopes[run_key] != scope:
            return False
        self._scopes[run_key] = scope
        rotations = {r["name"]: json.loads(r["lines"]) for r in store.rotation_rows(self.ports.conn)
                     if r["status"] in {"candidate", "dummy", "field", "champion"}}
        grouped = {}
        checks = {}
        names = set()
        try:
            for directive in card.directives:
                for applied in to_commands(directive, state.members, tank_guid=tank_guid,
                        human_guid=self.ports.human_guid, rotations=rotations, lead_is_human=lead_is_human):
                    if isinstance(applied.command, CustomStrategy):
                        names.add(applied.command.name)
                        continue
                    for key in applied.keys:
                        if key[0] not in state.online or key[0] not in members:
                            continue
                        command, inverse = applied.command, applied.inverse
                        if isinstance(command, BotDo):
                            command, inverse = replace(command, guids=(key[0],)), replace(inverse, guids=(key[0],))
                        grouped.setdefault(key, []).append(Applied(command, inverse, (key,)))
                        checks.setdefault(key, []).append(directive)
        except (ValueError, KeyError, TypeError):
            return False
        if any(e.run != run_key and e.key in grouped for e in self._entries):
            return False
        if not self._available(state, tuple(k[0] for k in grouped)):
            return False
        if names and not names <= await self._prime(names, state, run_key, context):
            return False
        added = []
        try:
            for key, commands in grouped.items():
                previous = self._top(run_key, key)
                self._serial += 1
                entry = _Entry(run_key, layer, context, key, tuple(commands), self._serial, scope,
                               state.members[key[0]], tuple(checks[key]))
                self._entries.append(entry)
                added.append(entry)
                if self._top(run_key, key) is not entry:
                    continue
                # Different custom strategy names are not a server sibling group.
                # Clear the previously visible bundle before installing its
                # replacement so precisely one layer owns the logical lever.
                if previous is not None:
                    for row in reversed(self._rows(run_key)):
                        if not any(r["id"] == row["id"] for r in self._rows(run_key)):
                            continue
                        if row["id"] in previous.receipts and not await self._undo_row(row):
                            self._blocked.add(run_key)
                            return False
                ok, reason = await self._activate(entry)
                if reason == "skipped":
                    # A later command in this bundle may have skipped after an
                    # earlier strategy succeeded. Unwind that receipt before
                    # omitting the member; also restore the replaced lower layer.
                    await self._remove([entry])
                    if entry in self._entries or run_key in self._blocked:
                        return False
                    added.remove(entry)
                elif not ok:
                    if reason in _DRIFT and card.id is not None:
                        store.set_card_status(self.ports.conn, card.id, "suspended", ts=self.ports.clock.now())
                    await self._remove(added)
                    return False
            return True
        except asyncio.CancelledError:
            self._blocked.add(run_key)
            raise

    async def _undo_row(self, row):
        try:
            envelope, inverse = json.loads(row["command"]), _decode(json.loads(row["inverse"]))
            scope = envelope["scope"]
            if not envelope["settled"] or not self._matches(scope, epoch=False):
                return False
            if inverse is None:
                store.mark_undone(self.ports.conn, [row["id"]], self.ports.clock.now())
                return True
            state = self._state(scope["run_key"])
            guids = inverse.guids if isinstance(inverse, BotDo) else (inverse.leader_guid,)
            if not self._available(state, guids, cleanup=True) or self._pending():
                return False
            # The restore itself is journaled: cancellation while restoring keeps
            # both the original receipt and this retryable cleanup receipt open.
            receipt = await self._send(row["run"], row["layer"], row["context"], inverse,
                inverse, self._scope(state), envelope.get("keys", ()))
            if receipt.settled and receipt.ok and not receipt.skipped and self._matches(self._scope(state)):
                # One server restore consumes its snapshot. Repeating a value
                # restore can fall back to stock instead of the original value.
                ids = [row["id"], receipt.id]
                for other in self._rows(row["run"]):
                    saved = json.loads(other["command"])
                    saved_scope = saved.get("scope", {})
                    if (all(saved_scope.get(k) == v for k, v in scope.items() if k != "connection_epoch")
                            and saved.get("settled")
                            and json.loads(other["inverse"]) == json.loads(row["inverse"])):
                        ids.append(other["id"])
                store.mark_undone(self.ports.conn, ids, self.ports.clock.now())
                return True
        except (KeyError, TypeError, ValueError):
            log.warning("training cleanup receipt cannot be decoded")
        self._blocked.add(row["run"])
        return False

    async def _remove(self, entries):
        count = 0
        for entry in sorted(entries, key=lambda e: e.serial, reverse=True):
            if entry not in self._entries:
                continue
            top = self._top(entry.run, entry.key) is entry
            rows = [r for r in reversed(self._rows(entry.run)) if r["id"] in entry.receipts]
            if top:
                for row in rows:
                    if not any(r["id"] == row["id"] for r in self._rows(entry.run)):
                        continue
                    if not await self._undo_row(row):
                        self._blocked.add(entry.run)
                        return count
                    count += 1
            else:
                # Its command was superseded; do not disturb the active layer.
                # Open receipts remain until a visible restore confirms baseline.
                pass
            self._entries.remove(entry)
            if top:
                lower = self._top(entry.run, entry.key)
                if lower is not None and entry.run not in self._stopped:
                    ok, reason = await self._activate(lower, application=False)
                    if not ok or reason == "skipped":
                        self._blocked.add(entry.run)
                        return count
        for run in {e.run for e in entries}:
            active = {ident for e in self._entries if e.run == run for ident in e.receipts}
            if all(r["id"] in active and json.loads(r["command"]).get("settled") for r in self._rows(run)):
                self._blocked.discard(run)
        return count

    async def revert(self, run_key, layer, *, context=None):
        with self._operation(run_key):
            async with self._lock:
                return await self._remove([e for e in self._entries if e.run == run_key and e.layer == layer
                                          and (context is None or e.context == context)])

    async def end_run(self, run_key):
        with self._operation(run_key):
            async with self._lock:
                return await self._end_run(run_key)

    async def _end_run(self, run_key):
        self._blocked.add(run_key)
        count = 0
        initial = self._rows(run_key)
        scope = self._scopes.get(run_key)
        had_dc = False
        touched = {e.key[0] for e in self._entries if e.run == run_key and not e.key[1].startswith("dc:")}
        for row in initial:
            try:
                saved = json.loads(row["command"])
                scope = scope or saved["scope"]
                command = _decode(saved["wire"])
                if isinstance(command, BotDo):
                    touched.update(command.guids)
                had_dc = had_dc or isinstance(command, DcSetting)
            except (KeyError, TypeError, ValueError):
                return count
        # Exact per-command restores are safe even if some touched members are
        # currently offline. Never mark the remaining receipts undone by fiat.
        for row in reversed(self._rows(run_key)):
            if not any(r["id"] == row["id"] for r in self._rows(run_key)):
                continue
            if await self._undo_row(row):
                count += 1
            else:
                return count
        if self._rows(run_key):
            return count
        if scope is not None:
            if not self._matches(scope, epoch=False):
                return count
            state = self._state(scope["run_key"])
            commands = [BotDo("", (guid,), "restore", "*") for guid in sorted(touched)]
            if (state.run_key is not None or had_dc) and state.tank_guid and not state.lead_is_human and state.tank_guid != self.ports.human_guid:
                commands.append(DcSetting("", state.tank_guid, "clear"))
            for command in commands:
                guids = command.guids if isinstance(command, BotDo) else (command.leader_guid,)
                if not self._matches(self._scope(state)) or not self._available(self._state(scope["run_key"]), guids):
                    return count
                receipt = await self._send(run_key, "standing", "0:0:0", command, command, self._scope(state))
                if (not receipt.settled or not receipt.ok or receipt.skipped or not self._matches(self._scope(state))
                        or not self._available(self._state(scope["run_key"]), guids)):
                    return count
                store.mark_undone(self.ports.conn, [receipt.id], self.ports.clock.now())
        self._entries = [e for e in self._entries if e.run != run_key]
        self._scopes.pop(run_key, None)
        self._blocked.discard(run_key)
        if run_key in self._claims:
            self.ports.release("training", self._claims.pop(run_key))
        return count

    async def prime(self, names):
        state = self._state(None)
        if state is None:
            return set()
        run = state.run_key or "training:prime"
        with self._operation(run):
            async with self._lock:
                if not self._matches(self._scope(state)) or not self.acting(self.ports.clock.now()) or self._pending():
                    return set()
                return await self._prime(set(names), state, run, "0:0:0")

    async def _prime(self, names, state, run, context):
        hello = self._hello
        result = set()
        if not state.vocab.complete or run in self._stopped or self._blocked:
            return result
        rows = {r["name"]: r for r in store.rotation_rows(self.ports.conn)}
        for name in sorted(names):
            row = rows.get(name)
            current = self._state(state.run_key)
            if (not _ROTATION_NAME.fullmatch(name) or current is None or not current.vocab.complete
                    or row is None or row["status"] not in {"candidate", "dummy", "field", "champion"}
                    or current.vocab.rotations.get(name) != (row["class"], row["spec_tab"])
                    or state.vocab.rotations.get(name) != (row["class"], row["spec_tab"])):
                continue
            probes = sorted(g for g, m in current.members.items() if g in current.online
                and g != self.ports.human_guid and (m.cls, m.spec_tab) == (row["class"], row["spec_tab"]))
            if not probes:
                log.info("training_rotation_no_online_probe")
                continue
            if not self._matches(self._scope(state)) or self._pending():
                break
            try:
                compiled = to_commands(Directive("rotation", name,
                    Targets(specs=((row["class"], row["spec_tab"]),))), current.members,
                    tank_guid=current.tank_guid, human_guid=self.ports.human_guid,
                    rotations={name: json.loads(row["lines"])})
                command = replace(compiled[0].command, probe=probes[0])
            except (ValueError, KeyError, TypeError, IndexError):
                continue
            receipt = await self._send(run, "standing", context, command, None, self._scope(state), application=True)
            if receipt.settled and receipt.ok and hello == self._hello and self._matches(self._scope(state)):
                store.mark_undone(self.ports.conn, [receipt.id], self.ports.clock.now())
                self._primed.add((state.connection_epoch, name))
                result.add(name)
            else:
                break
        return result

    async def replay_on_hello(self):
        # Invalidate in-flight callers before waiting for their serialization lock.
        self._hello += 1
        self._primed.clear()
        async with self._lock:
            count = 0
            for row in reversed(self._rows()):
                if not any(r["id"] == row["id"] for r in self._rows()):
                    continue
                try:
                    scope = json.loads(row["command"])["scope"]
                    if self._matches(scope, epoch=False):
                        self.ledger.mark_unscoreable(row["context"])
                except (KeyError, TypeError, ValueError):
                    pass  # An orphan cannot taint an unrelated current raid.
                if await self._undo_row(row):
                    count += 1
            if self._rows():
                return count
            self._entries.clear()
            self._scopes.clear()
            self._blocked.clear()
            for run, guids in tuple(self._claims.items()):
                self.ports.release("training", guids)
                self._claims.pop(run)
            state = self._state(None)
            if state is None or state.run_key is not None:
                return count
            names = sorted({r["name"] for r in store.rotation_rows(self.ports.conn)
                            if _ROTATION_NAME.fullmatch(r["name"])} | {"tg_smoke_v1"})
            guids = sorted(set(state.members) - {self.ports.human_guid})
            run = "training:hello:" + uuid4().hex
            granted = self.ports.claim("training", guids, until=self.ports.clock.now() + 30)
            self._claims[run] = set(granted)
            for i in range(0, len(guids), 40):
                batch = tuple(g for g in guids[i:i + 40] if g in granted and g in state.online)
                if not batch:
                    continue
                for j in range(0, len(names), 10):
                    for action in ("co", "nc"):
                        if not self._matches(self._scope(state)) or self._pending():
                            return count
                        if not self._available(self._state(None), batch):
                            return count
                        command = BotDo("", batch, action, ",".join("-custom::" + n for n in names[j:j + 10]))
                        receipt = await self._send(run, "standing", "0:0:0", command, command, self._scope(state))
                        if not receipt.settled or not receipt.ok or receipt.skipped:
                            return count
                        store.mark_undone(self.ports.conn, [receipt.id], self.ports.clock.now())
            self.ports.release("training", self._claims.pop(run))
            if not self.acting(self.ports.clock.now()):
                return count
            in_use = {r["name"] for r in store.rotation_rows(self.ports.conn)
                      if r["status"] in {"champion", "field"}}
            setups = store.latest_setups(self.ports.conn)
            used_ids = {r["rotation"] for r in setups.values()}
            in_use.update(r["name"] for r in store.rotation_rows(self.ports.conn) if r["id"] in used_ids)
            await self._prime(in_use, state, "training:prime", "0:0:0")
            await self._standing(state.online, None)
            return count

    async def apply_standing(self, guids, *, run_key=None):
        with self._operation(run_key):
            async with self._lock:
                return await self._standing(guids, run_key)

    async def _standing(self, guids, run_key):
        state = self._state(run_key)
        if state is None or not self.acting(self.ports.clock.now()) or self._rows() and not self._entries:
            return 0
        selected = set(guids) & state.online & set(state.members) - {self.ports.human_guid}
        outside = run_key is None
        if outside:
            # Releasing training bodies requires restoring their journal first.
            # Until Realm's durable defaults integration exists this would be an
            # immediate apply/undo. Preserve desired setups for the next raid.
            return 0
        run = run_key or "training:standing:" + uuid4().hex
        rotations = {r["id"]: r for r in store.rotation_rows(self.ports.conn)}
        count = 0
        for guid, row in store.latest_setups(self.ports.conn).items():
            if guid not in selected:
                continue
            m = state.members[guid]
            directives = []
            try:
                for toggle in json.loads(row["toggles"]):
                    directives.append(Directive(toggle[1:], toggle[0]))
                directives.extend(Directive(k, v) for k, v in json.loads(row["vals"]).items())
                if row["rotation"] is not None:
                    rotation = rotations[row["rotation"]]
                    directives.append(Directive("rotation", rotation["name"], Targets(specs=((m.cls, m.spec_tab),))))
                card = Card(None, "0:0:0", 1, "champion", "standing", tuple(directives))
                ok = await self._apply(run, "standing", card.context, card, {guid: m},
                    tank_guid=state.tank_guid, lead_is_human=state.lead_is_human, outside=outside)
                count += int(ok)
            except (KeyError, TypeError, ValueError):
                log.warning("training standing setup invalid")
        return count

    async def hold(self, tank):
        return await self._control(tank, DcSetting("", tank, "set", "WaitAtBoss", 100),
                                   DcSetting("", tank, "reset", "WaitAtBoss"), applying=True)

    async def release(self, tank):
        return await self._control(tank, RaidControl("", tank, "dc_pause", tank, "resume"), None)

    async def resume_held(self, tank):
        """Resume this held boss while keeping tactic layers and future boss holds.

        Unlike terminal release, this never restores cards or clears WaitAtBoss.
        Only this resume command's confirmed receipt is closed.
        """
        initial = self._state(None)
        if initial is None or initial.run_key is None:
            return False
        run, provider = initial.run_key, self.state_provider
        with self._operation(run):
            async with self._lock:
                state = self._state(run)
                if (provider is not self.state_provider or not self._matches(self._scope(initial))
                        or state is None or tank != state.tank_guid or state.lead_is_human
                        or tank == self.ports.human_guid or not self._resume_allowed(state, run)):
                    return False
                receipt = await self._send(run, "standing", "0:0:0",
                    RaidControl("", tank, "dc_pause", tank, "resume"), None,
                    self._scope(state), resume_held=True)
                if not receipt.settled or not receipt.ok:
                    return False
                store.mark_undone(self.ports.conn, [receipt.id], self.ports.clock.now())
                return (provider is self.state_provider and self._matches(self._scope(state))
                        and self._available(self._state(run), (tank,))
                        and self.acting(self.ports.clock.now()))

    async def clear_run(self, tank):
        return await self._control(tank, DcSetting("", tank, "clear"), DcSetting("", tank, "clear"))

    async def close(self):
        """Stop owned waiters; keep unresolved SQL receipts and body claims.

        Integration must call this before closing the transport/database. False
        means a broken transport swallowed cancellation: it still owns a task
        and the integration must not close its database underneath that task.
        """
        self._closing = True
        self._hello += 1
        tasks = {r.task for r in self._receipts.values() if r.task is not None and not r.task.done()}
        for task in tasks:
            task.cancel()
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=_WAIT_S)
            return not pending
        return True

    async def _control(self, tank, command, inverse, *, applying=False):
        initial = self._state(None)
        if initial is None:
            return False
        with self._operation(initial.run_key or "training:control"):
            return await self._control_locked(tank, command, inverse, applying=applying,
                                              initial=initial, provider=self.state_provider)

    async def _control_locked(self, tank, command, inverse, *, applying, initial, provider):
        async with self._lock:
            state = self._state(None)
            if (provider is not self.state_provider or not self._matches(self._scope(initial))
                    or state is None or tank != state.tank_guid or state.lead_is_human or tank == self.ports.human_guid
                    or self._pending() or not self._available(state, (tank,))
                    or applying and (not self.acting(self.ports.clock.now()) or self._blocked)):
                return False
            run = state.run_key or "training:control"
            if applying and run in self._stopped:
                return False
            if not applying:
                await self._end_run(run)
                if self._rows(run) or run in self._blocked:
                    return False
            if (provider is not self.state_provider or not self._matches(self._scope(state))
                    or not self._available(self._state(state.run_key), (tank,))):
                return False
            receipt = await self._send(run, "standing", "0:0:0", command, inverse, self._scope(state),
                                       application=applying)
            if receipt.settled and receipt.ok and not applying:
                store.mark_undone(self.ports.conn, [receipt.id], self.ports.clock.now())
            return receipt.settled and receipt.ok
