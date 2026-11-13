"""Paced Realm admission and receipt delivery, with no neighboring inspection.

The service explicitly recovers ActionStore once before attaching store.holds to
BodyLedger. Its LivePorts on_send must call before_send synchronously immediately
before Bridge writer.write; on_ack/on_send_error describe actual settlement.
Returning/canceling the outer send await alone never settles a transport.
"""
import asyncio
from collections import deque
from dataclasses import dataclass
import json
import logging
import math
import uuid

from .action_policy import classify, integer, members, text, U64
from .action_store import ActionCapacity, ActionSpec, TERMINAL, failed_backoff, leave_refusal_guids, skipped_backoff
from .protocol import GetRealmPvp, LeavePvp, Rearm, RealmActors

log = logging.getLogger(__name__)


class ActionRejected(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ActionContext:
    enabled: bool
    connected: bool
    presence_known: bool
    generation: int
    process_id: str
    connection_epoch: int
    guild: int
    human: int

    def __post_init__(self):
        if any(type(getattr(self, k)) is not bool for k in ("enabled", "connected", "presence_known")):
            raise ValueError("invalid action context")
        integer(self.generation, 0, U64); text(self.process_id, 1, 256)
        integer(self.connection_epoch, 0, U64); integer(self.guild, 0); integer(self.human, 0)
        if self.presence_known and (not self.connected or not self.connection_epoch or not self.guild or not self.human):
            raise ValueError("invalid action presence")


@dataclass(frozen=True, slots=True)
class _Entry:
    command: object
    context: ActionContext
    callback: object = None
    still_valid: object = None


def _live(c):
    return c.enabled and c.connected and c.presence_known


def _same(a, b):
    return (a.generation, a.process_id, a.connection_epoch, a.guild, a.human) == (b.generation, b.process_id, b.connection_epoch, b.guild, b.human)


class ActionQueue:
    def __init__(self, send, bodies, clock, *, store, context, per_s=2.0, actor_probe=None):
        if type(per_s) not in (int, float) or not math.isfinite(per_s) or not 0 < per_s <= 2:
            raise ValueError("per_s must be positive and at most two")
        self.send, self.bodies, self.clock = send, bodies, clock
        self.store, self.context, self.per_s = store, context, float(per_s)
        self.actor_probe = actor_probe  # synchronous () -> full RealmActors | None
        self.off = False; self.closed = False
        self._pending = {}; self._entries = {}; self._started = set(); self._tasks = set()
        self._body_times = deque(maxlen=2); self._query_times = deque(maxlen=2)
        self._cleanup_wanted = set(); self._probe_ids = set(); self._probe_at = 0.0
        self._wake = asyncio.Event(); self._running = False
        self._context()

    def _context(self):
        c = self.context()
        if type(c) is not ActionContext: raise ValueError("ActionContext required")
        return c

    def wake(self):
        self._wake.set()

    def owns_command(self, cmd):
        try: c = classify(cmd, child=True)
        except (ValueError, TypeError, AttributeError): return False
        record = self.store.get(cmd.id)
        return record is not None and record.payload == c.payload

    def _enroll(self, cmd, owner, on_done, *, probe=False, still_valid=None):
        c = self._context(); classified = classify(cmd)
        if self.closed or not _live(c): raise ActionRejected("disabled")
        if self.off and not probe and classified.policy != "defaults": raise ActionRejected("realm_off")
        rank = self.bodies.rank_for(owner)
        if rank is None: raise ActionRejected("owner_rank_unavailable")
        current = self._context()
        if self.closed or not _live(current) or not _same(c, current): raise ActionRejected("stale_context")
        if self.off and not probe and classified.policy != "defaults": raise ActionRejected("realm_off")
        c = current
        now = self.clock.now()
        if self._backed_off(owner, classified.kind, classified.reserved_manifest, now):
            raise ActionRejected("backoff")
        record = self.store.prepare(ActionSpec(cmd.id, None, "root", classified.policy, owner, rank,
                 classified.kind, classified.payload, classified.reserved_manifest, c.guild, c.human), now=now)
        entry = _Entry(cmd, c, on_done, still_valid)
        self._entries[cmd.id] = entry; self._pending[cmd.id] = entry
        self.wake()
        return record.command_id

    def _backed_off(self, owner, kind, scope, now):
        if not scope: return False
        for old in self.store.records():
            failed_at = json.loads(old.evidence).get("failed_at")
            # A named leave ack refusal changed nobody: only the members it names are backed off.
            # Nor does a failed leave back off what native released and did not skip (failed_backoff).
            if (old.role == "root" and old.owner == owner and old.kind == kind
                    and old.result_ok is False and failed_at is not None and now < failed_at + 600
                    and failed_backoff(old) & set(scope)):
                return True
            # Nor did a completed leave change the members it skipped.
            if old.role == "root" and old.owner == owner and old.kind == kind and set(skipped_backoff(old, now)) & set(scope):
                return True
        return False

    def leave_backed_off(self, owner):
        """The members a recent ack refusal of this owner's leaves named, or a recent completed
        leave of his skipped: a leave naming any of them is rejected for 600 s, so the recall
        leaves them out and sends the rest."""
        now = self.clock.now()
        named = set()
        for old in self.store.records():
            if old.role != "root" or old.owner != owner or old.kind != "leave_pvp": continue
            failed_at = json.loads(old.evidence).get("failed_at")
            if old.result_ok is False and failed_at is not None and now < failed_at + 600:
                named |= set(leave_refusal_guids(old.result_reason, old.reserved_manifest) or ())
            named |= set(skipped_backoff(old, now))
        return named

    def submit(self, cmd, *, owner, guids=(), on_done=None, freeing=False, still_valid=None):
        classified = classify(cmd)  # Reject malformed business fields before journaling.
        supplied = members(guids, 0)
        if supplied and tuple(sorted(supplied)) != classified.reserved_manifest:
            raise ValueError("explicit guids disagree with command")
        if type(freeing) is not bool: raise ValueError("invalid freeing flag")
        if freeing and type(cmd) not in {LeavePvp, Rearm}: raise AssertionError("freeing requires LeavePvp or Rearm")
        if on_done is not None and not callable(on_done): raise ValueError("invalid action callback")
        # freeing grants no scheduling or retained-ownership bypass.
        if still_valid is not None and not callable(still_valid): raise ValueError('invalid action predicate')
        return self._enroll(cmd, owner, on_done, still_valid=still_valid)

    def request_cleanup(self, origin_id):
        c = self._context()
        if self.closed or not _live(c): raise ActionRejected("disabled")
        root = self.store.get(origin_id)
        if (root is None or root.policy != "wg" or root.state in TERMINAL or root.state == "conflict"
                or root.guild != c.guild or root.human != c.human): raise ActionRejected("invalid_cleanup_origin")
        self._cleanup_wanted.add(origin_id)
        self.wake()

    def _deliver(self, transition):
        if not transition.result_ready: return
        entry = self._entries.pop(transition.command_id, None)
        self._pending.pop(transition.command_id, None)
        if entry is None or entry.callback is None: return
        c = self._context()
        if self.closed or not _live(c) or not _same(entry.context, c): return
        try:
            result = entry.callback(transition.result_ok, transition.reason, transition.value)
            # Async callbacks would be outside the synchronous commit boundary.
            if asyncio.iscoroutine(result):
                result.close(); log.warning("realm_action_callback_failed")
        except Exception:
            log.warning("realm_action_callback_failed")

    def _after(self, transition):
        self._deliver(transition)
        self.wake()
        return transition.matched

    def context_changed(self, *, _wake=True):
        c = self._context()
        for command_id, entry in tuple(self._entries.items()):
            if not _live(c) or not _same(entry.context, c):
                self._entries.pop(command_id, None)
                self._pending.pop(command_id, None)
                self._probe_ids.discard(command_id)
                self.store.predispatch_failed(command_id, reason="stale_context", now=self.clock.now())
        if not _live(c): self._cleanup_wanted.clear()
        if _wake: self.wake()

    def drop_owner(self, owner):
        text(owner, 1, 256); count = 0
        for command_id, entry in tuple(self._pending.items()):
            record = self.store.get(command_id)
            if record.owner != owner: continue
            transition = self.store.predispatch_failed(command_id, reason="owner_dropped", now=self.clock.now())
            self._pending.pop(command_id, None); self._entries.pop(command_id, None)
            count += transition.matched
        self.wake()
        return count

    def _dispatch_entry(self, cmd, entry):
        """Read current admission truth without invoking the body callback pump."""
        c = self._context(); record = self.store.get(cmd.id)
        if self.closed or not _live(c) or not _same(entry.context, c): raise ActionRejected("stale_context")
        if (self._pending.get(cmd.id) is not entry or self._entries.get(cmd.id) is not entry
                or record is None or record.state != "prepared"
                or record.payload != classify(cmd, child=True).payload):
            raise ActionRejected("not_enrolled")
        if (record.guild, record.human) != (c.guild, c.human): raise ActionRejected("stale_identity")
        if self.off and (record.role == "cleanup" or record.role == "root" and record.policy != "defaults" and cmd.id not in self._probe_ids):
            raise ActionRejected("realm_off")
        return c, record

    def before_send(self, cmd):
        """The synchronous, committing final transport barrier. Never await here."""
        entry = self._pending.get(cmd.id)
        if entry is None: raise ActionRejected("not_enrolled")
        c, record = self._dispatch_entry(cmd, entry)
        if sum(r.transport_state == "inflight" for r in self.store.records()) >= 8: raise ActionRejected("transport_capacity")
        eligible = True
        if record.role == "root" and record.reserved_manifest:
            eligible = self.bodies.may_move_all(record.owner, record.reserved_manifest, rank=record.rank)
        elif record.role == "cleanup":
            root = self.store.get(record.parent_id); data = json.loads(root.evidence)
            if (root.state in TERMINAL or root.state == "conflict" or data.get("cleanup_id") != cmd.id
                    or root.native_manifest != tuple(cmd.guids) or data["binding"]["pvp_owner"] != cmd.pvp_owner):
                raise ActionRejected("cleanup_proof_unavailable")
        elif record.role == "query":
            root = self.store.get(record.parent_id)
            if root is None or root.state in TERMINAL or root.state == "conflict":
                raise ActionRejected("query_origin_unavailable")
        # Ledger settlement can deliver callbacks that invalidate context or
        # cancel this intent, and can commit a newly relevant failure receipt.
        # Re-read all local admission truth after that callback boundary.
        c, record = self._dispatch_entry(cmd, entry)
        now = self.clock.now()
        if record.role == "root" and self._backed_off(record.owner, record.kind, record.reserved_manifest, now):
            raise ActionRejected("backoff")
        if not eligible: raise ActionRejected("held")
        if sum(r.transport_state == "inflight" for r in self.store.records()) >= 8: raise ActionRejected("transport_capacity")
        times = self._query_times if record.role == "query" else self._body_times if record.reserved_manifest else None
        if times is not None and not self._paced(times, now): raise ActionRejected("paced")
        if entry.still_valid is not None and not entry.still_valid(): raise ActionRejected('intent_expired')
        c, record = self._dispatch_entry(cmd, entry)
        # The successful commit must precede the direct transport. Commit errors
        # escape this hook and prevent writer.write.
        self.store.begin_dispatch(cmd.id, payload=classify(cmd, child=True).payload,
             process_id=c.process_id, connection_epoch=c.connection_epoch, now=now)
        if times is not None: times.append(now)
        self._pending.pop(cmd.id, None)

    def on_ack(self, cmd, ack):
        if not self.owns_command(cmd) or ack.id != cmd.id: return False
        transition = self.store.observe_ack(ack, now=self.clock.now())
        record = self.store.get(cmd.id)
        if transition.matched and record.role != "root": self._entries.pop(cmd.id, None)
        if transition.matched and not ack.ok and ack.error == "realm_off":
            self.off = True; self._probe_at = self.clock.now() + 600
            for command_id in tuple(self._pending):
                r = self.store.get(command_id)
                if r.role == "root": self._deliver(self.store.predispatch_failed(command_id, reason="realm_off", now=self.clock.now()))
        if transition.matched and cmd.id in self._probe_ids:
            self._probe_ids.discard(cmd.id)
            if ack.ok: self.off = False
        return self._after(transition)

    def on_send_error(self, cmd, safe_type):
        if not self.owns_command(cmd): return False
        transition = self.store.observe_send_error(cmd.id, safe_type, now=self.clock.now())
        if transition.matched and self.store.get(cmd.id).role != "root": self._entries.pop(cmd.id, None)
        self._probe_ids.discard(cmd.id)
        return self._after(transition)

    def on_realm_job(self, ev):
        return self._after(self.store.observe_realm_job(ev, now=self.clock.now()))

    def on_job(self, ev):
        return self._after(self.store.observe_bot_job(ev, now=self.clock.now()))

    def on_realm_pvp(self, ev):
        return self._after(self.store.observe_pvp(ev, now=self.clock.now()))

    def on_hello(self, ev):
        c = self._context()
        if (_live(c) and type(ev.guild_id) is int and type(ev.human_guid) is int
                and ev.guild_id == c.guild and ev.human_guid in (0, c.human)):
            self.off = False
            try:
                self.store.restart_release(started_ms=getattr(ev, 'started_ms', 0),
                    guild=c.guild, human=c.human, now=self.clock.now())
            except Exception as e:  # never cost the rest of the hello (Recall, speech, the actor set)
                log.warning("realm_action_restart_release_failed: %s", type(e).__name__)
        self.context_changed()

    def _paced(self, times, now):
        while times and now - times[0] >= 1.0: times.popleft()
        if len(times) >= 2: return False
        return self.per_s == 2 or not times or now - times[-1] >= 1 / self.per_s

    def _child(self, record, cmd, c):
        entry = _Entry(cmd, c)
        self._entries[record.command_id] = entry; self._pending[record.command_id] = entry

    def _recovery(self, c, now):
        candidates = []
        for root in self.store.unresolved():
            if root.policy != "wg" or root.transport_state == "not_entered" or (root.guild, root.human) != (c.guild, c.human): continue
            data = json.loads(root.evidence)
            if not self.off and root.command_id in self._cleanup_wanted and root.native_manifest and not data.get("cleanup_id") and root.state != "conflict":
                status = data.get("status")
                if status and status["stage"] not in {"unknown", "clean"}:
                    cmd = LeavePvp(uuid.uuid4().hex, root.native_manifest, data["binding"]["pvp_owner"])
                    try:
                        record = self.store.prepare_cleanup(root.command_id, cmd, now=now)
                    except ActionCapacity:
                        # Exhausted controller history is not retryable by the
                        # worker. Keep ownership and reject this exact request.
                        self._cleanup_wanted.discard(root.command_id)
                        log.warning("realm_action_cleanup_capacity")
                    else:
                        self._child(record, cmd, c); self._cleanup_wanted.discard(root.command_id)
            if root.state == "conflict": continue
            query = self.store.get(data["query_id"]) if data.get("query_id") else None
            if query and query.transport_state != "settled" and query.state != "not_sent": continue
            if now < data.get("last_query", -1e30) + 10: continue
            candidates.append((data.get("last_query", -1e30), root.seq, root.command_id))
        # Oldest actual query first, never-queried roots first, stable sequence
        # only as a tie-breaker. The bounded root list needs no persistent cursor.
        if (candidates and self._paced(self._query_times, now)
                and not any(self.store.get(i).role == "query" for i in self._pending)):
            origin_id = min(candidates)[2]
            cmd = GetRealmPvp(uuid.uuid4().hex, origin_id=origin_id)
            record = self.store.prepare_query(origin_id, cmd, now=now)
            self._child(record, cmd, c)

    def tick(self):
        """Synchronous bounded scheduling; receipts only wake, never call tick."""
        self.context_changed(_wake=False)
        c = self._context(); now = self.clock.now()
        if self.closed or not _live(c): return
        self.store.prune(now=now)
        self._cleanup_wanted.intersection_update(r.command_id for r in self.store.unresolved())
        self._recovery(c, now)
        if self.off and self.actor_probe is not None and now >= self._probe_at and not self._probe_ids:
            self._probe_at = now + 600
            cmd = self.actor_probe()
            if cmd is not None:
                if type(cmd) is not RealmActors: raise ValueError("actor_probe must return complete RealmActors")
                try:
                    command_id = self._enroll(cmd, "realm_day", None, probe=True)
                except ActionCapacity:
                    # Probe admission retries at the next600-second deadline;
                    # no uncertain ownership is removed to create space.
                    log.warning("realm_action_probe_capacity")
                except ActionRejected:
                    log.warning("realm_action_probe_deferred")
                else:
                    self._probe_ids.add(command_id)
        records = self.store.records()
        starting = [r for r in records if r.command_id in self._started and r.state == "prepared"]
        available = 8 - sum(r.transport_state == "inflight" for r in records) - len(starting)
        body_slots = 2 - len(self._body_times) if self._paced(self._body_times, now) else 0
        query_slots = 2 - len(self._query_times) if self._paced(self._query_times, now) else 0
        if self.per_s < 2:
            body_slots = min(body_slots, 1)
            query_slots = min(query_slots, 1)
        body_slots -= sum(bool(r.reserved_manifest) for r in starting)
        query_slots -= sum(r.role == "query" for r in starting)
        for command_id, entry in tuple(self._pending.items()):
            if available <= 0: break
            if command_id in self._started: continue
            record = self.store.get(command_id)
            if self.off and (record.role == "cleanup" or record.role == "root" and record.policy != "defaults" and command_id not in self._probe_ids): continue
            if record.role == "query":
                if query_slots <= 0: continue
                query_slots -= 1
            elif record.reserved_manifest:
                if body_slots <= 0: continue
                body_slots -= 1
            self._started.add(command_id); available -= 1
            task = asyncio.create_task(self._dispatch(entry))
            self._tasks.add(task)
            task.add_done_callback(self._task_done)

    def _task_done(self, task):
        self._tasks.discard(task)
        if not task.cancelled(): task.exception()
        self.wake()

    async def _dispatch(self, entry):
        cmd = entry.command
        try:
            ack = await self.send(cmd)
            # Adapters may return the same ACK after their settlement callback;
            # duplicate evidence is idempotent. A missing barrier never sends.
            record = self.store.get(cmd.id)
            if record.transport_state != "not_entered" and ack is not None: self.on_ack(cmd, ack)
            elif record.state == "prepared": self._after(self.store.predispatch_failed(cmd.id, reason="barrier_missing", now=self.clock.now()))
        except asyncio.CancelledError:
            # The actual shielded transport may still be active. Only callbacks
            # or explicit startup recovery can change transport_state.
            raise
        except ActionRejected as exc:
            record = self.store.get(cmd.id)
            if record.state == "prepared": self._after(self.store.predispatch_failed(cmd.id, reason=str(exc), now=self.clock.now()))
        except Exception:
            record = self.store.get(cmd.id)
            if record.state == "prepared": self._after(self.store.predispatch_failed(cmd.id, reason="predispatch_rejected", now=self.clock.now()))
            log.warning("realm_action_send_failed")
        finally:
            self._started.discard(cmd.id)

    async def run(self):
        if self._running: raise RuntimeError("action queue already running")
        self._running = True
        try:
            while not self.closed:
                self._wake.clear(); self.tick()
                timer = asyncio.create_task(self.clock.sleep(0.05))
                wake = asyncio.create_task(self._wake.wait())
                try: await asyncio.wait((timer, wake), return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for task in (timer, wake):
                        if not task.done(): task.cancel()
                    await asyncio.gather(timer, wake, return_exceptions=True)
        finally:
            self._running = False

    async def close(self):
        self.closed = True; self._cleanup_wanted.clear(); self.wake()
        for command_id in tuple(self._pending):
            self.store.predispatch_failed(command_id, reason="queue_closed", now=self.clock.now())
        self._pending.clear(); self._entries.clear()
        # Do not cancel queue sends or ports' already-entered settlements.
        if self._tasks: await asyncio.gather(*tuple(self._tasks), return_exceptions=True)
