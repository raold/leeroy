"""Coalesced Realm recall and defaults; all external work enters ActionQueue.

Local intents are disposable generation-bound scheduling wishes. The ledger and
journal alone retain native ownership, including bodies no longer online.
"""

from dataclasses import dataclass
import logging
import uuid

from leeroy_mind.bodies import BodyEvidenceUnavailable
from leeroy_mind.guild.protocol import BotDo

from .actions import ActionRejected
from .action_store import STEP_REFUSED, ActionCapacity, defaults_partial, leave_refusal_guids, leave_skipped_guids, leave_released_guids
from .protocol import LeavePvp, Rearm

log = logging.getLogger(__name__)
# A rearm's results that settle it holding nobody: refused at its ack, or at one body's step.
REARM_REFUSED = frozenset({"refused", STEP_REFUSED})
# How long the end of a leave's hold on a member its leave did not take out (refused, skipped,
# released unrun) is kept from handing that member back to defaults: the ledger's next poll.
KEEP_S = 60


@dataclass(slots=True)
class _Chain:
    key: str
    ticket: tuple
    owner: str
    members: tuple[int, ...]
    steps: tuple
    index: int = 0
    mode: str = "defaults"
    waiting: bool = False
    awaiting_terminal: bool = False


class Recall:
    def __init__(self, ports, queue, bodies, clock):
        self.ports, self.queue, self.bodies, self.clock = ports, queue, bodies, clock
        self._serial = 0
        self._desired = {}
        self._second = set()
        self._fallback = set()
        self._chains = {}
        self._busy = {}
        self._cleanup = {}
        self._handback = set()
        self._kept = {}  # guid -> until when a leave's release of it hands it no defaults
        self._last_recall = None
        self._second_at = None
        self._last_defaults = clock.now()
        self._off = queue.off
        bodies.on_release(self._released)

    def _ticket(self):
        c = self.queue.context()
        return (self._serial, c.generation, c.process_id, c.connection_epoch, c.guild, c.human)

    def _live(self):
        c = self.queue.context()
        return (not self.queue.closed and c.enabled and c.connected and c.presence_known)

    def _valid_ticket(self, ticket):
        return self._live() and ticket == self._ticket()

    def _current(self, chain):
        return self._chains.get(chain.key) is chain and self._valid_ticket(chain.ticket)

    def invalidate(self):
        """Discard only local wishes. Never remove a native reservation."""
        self._serial += 1
        self._desired.clear()
        self._second.clear()
        self._fallback.clear()
        self._chains.clear()
        self._busy.clear()
        self._cleanup.clear()
        self._handback.clear()
        # Cooldown survives hello/generation invalidation; intents do not.
        self._second_at = None
        self._last_defaults = self.clock.now()
        self._off = self.queue.off

    def _online(self):
        try:
            cast = self.ports.cast()
            if any(type(m.guid) is not int or not 0 < m.guid <= 0xFFFFFFFF
                   or type(m.online) is not bool for m in cast):
                raise ValueError("invalid cast")
            return {m.guid for m in cast if m.online}
        except Exception:
            log.warning("realm_recall_cast_unavailable")
            return None

    def _eligible(self, owner, guids):
        try:
            return self.bodies.movable(owner, guids, besides={owner, "realm_day", "realm_event"})
        except BodyEvidenceUnavailable:
            return frozenset()

    def _released(self, owner, guids):
        # Never enqueue inline: a provider refresh can call this during queue
        # admission. Busy chains consume their own temporary reservation release.
        if not self._live():
            return
        selected = set(guids) - self._busy.keys()
        if owner in {"realm_day", "realm_event"}:
            selected &= self._handback
        # A leave that did not take these members out ended its hold on them: they may still be in
        # a battleground or LFG, so the release hands them no rearm or defaults.
        now = self.clock.now()
        self._kept = {g: until for g, until in self._kept.items() if until > now}
        selected -= self._kept.keys()
        for guid in selected:
            self._desired.setdefault(guid, "defaults")
        self._handback.difference_update(selected)
        if selected:
            self.queue.wake()

    def restore_defaults(self, guids=None):
        if not self._live():
            return
        if guids is None:
            guids = self._online()
            if guids is None:
                return
            self._last_defaults = self.clock.now()
        guids = tuple(guids)
        if self._off and not self.queue.off:
            self._desired = {g: "defaults" for g in self._desired}
            self._fallback.clear()
        self._off = self.queue.off
        for guid in guids:
            busy = self._chains.get(self._busy.get(guid))
            if busy is None or busy.mode == "fallback":
                self._desired.setdefault(guid, "defaults")
        if self.queue.off:
            self._fallback.update(g for g in guids if g not in self._busy)
        self._flush()

    def _drop_realm_intent(self):
        ticket = self._ticket()
        for owner in ("realm_day", "realm_event"):
            self.queue.drop_owner(owner)
            self.bodies.release(owner)
            if not self._valid_ticket(ticket):
                return
        # drop_owner deliberately has no completion callbacks. Discard chains
        # belonging to its canceled application wishes, retaining dispatched rows.
        for key, chain in tuple(self._chains.items()):
            if chain.owner in {"realm_day", "realm_event"}:
                self._finish(chain)

    def _reconcile(self, *, all_roots=False):
        c = self.queue.context()
        try:
            roots = self.queue.store.unresolved()
            for root in roots:
                if (root.policy != "wg" or (root.guild, root.human) != (c.guild, c.human)
                        or not all_roots and root.command_id not in self._cleanup):
                    continue
                self._cleanup[root.command_id] = root.reserved_manifest
                self._handback.update(root.reserved_manifest)
                try:
                    self.queue.request_cleanup(root.command_id)
                except ActionRejected:
                    # Conflicted/unknown roots remain held; no guessed token or
                    # current-online intersection can repair their authority.
                    pass
            for origin_id, guids in tuple(self._cleanup.items()):
                root = self.queue.store.get(origin_id)
                if root is not None and root.state == "completed" and root.result_ok is True:
                    for guid in guids:
                        if guid not in self._busy:
                            self._desired.setdefault(guid, "defaults")
                    self._handback.difference_update(guids)
                    del self._cleanup[origin_id]
                elif root is None or root.state in {"completed", "not_sent"}:
                    self._handback.difference_update(guids)
                    del self._cleanup[origin_id]
        except BodyEvidenceUnavailable:
            log.warning("realm_recall_ownership_unavailable")

    def on_login(self, now):
        if not self._live():
            return
        ticket = self._ticket()
        if self._last_recall is not None and now - self._last_recall < 120:
            self._reconcile(all_roots=True)
            return
        self._last_recall = now
        self._off = self.queue.off
        self._second_at = now + 45
        self._drop_realm_intent()
        if not self._valid_ticket(ticket):
            return
        self._reconcile(all_roots=True)
        online = self._online()
        if online is not None:
            available = self.bodies.rank_for("raid") is not None
            if not self._valid_ticket(ticket):
                return
            candidates = online - self._busy.keys() - self._handback
            eligible = self._eligible("raid", candidates)
            if not self._valid_ticket(ticket):
                return
            for guid in candidates:
                self._desired[guid] = "recall" if not available or guid in eligible else "defaults"
            if self.queue.off:
                self._fallback.update(online - self._busy.keys())
        if self._valid_ticket(ticket):
            self._flush()

    def on_hello(self, ev):
        if not self._live():
            return
        ticket = self._ticket()
        if ev.human_guid:
            self.on_login(self.clock.now())
        if self._valid_ticket(ticket):
            self.restore_defaults()

    def on_raid_state(self, old, new):
        old, new = getattr(old, "name", old), getattr(new, "name", new)
        if old in {"IDLE", "ENDED"} and new == "FORMING":
            self.on_login(self.clock.now())

    def _finish(self, chain):
        self._chains.pop(chain.key, None)
        for guid in chain.members:
            if self._busy.get(guid) == chain.key:
                del self._busy[guid]

    def _advance(self, chain, ok=True):
        if not self._current(chain):
            self._finish(chain)
            return
        if chain.awaiting_terminal:
            return
        online = self._online()
        # Rank availability may refresh sources, so it precedes the final
        # selection. Keep the complete chain busy while both reads consume its
        # own effective reservation release, including failed/default results.
        available = self.bodies.rank_for(chain.owner) is not None
        if not self._current(chain):
            self._finish(chain)
            return
        eligible = self._eligible(chain.owner, chain.members)
        if not self._current(chain):
            self._finish(chain)
            return
        if ok and (online is None or not available):
            chain.waiting = True
            return
        chain.waiting = False
        if not ok or not set(chain.members) <= online:
            self._finish(chain)
            return
        if eligible != frozenset(chain.members):
            # A denied selection never dispatches. This later rank read only
            # distinguishes unavailable evidence (defer) from an actual holder
            # (stop); no selected set is used for dispatch after that refresh.
            available = self.bodies.rank_for(chain.owner) is not None
            if not self._current(chain):
                self._finish(chain)
            elif not available:
                chain.waiting = True
            else:
                self._finish(chain)
            return
        if chain.index == len(chain.steps):
            self._finish(chain)
            return
        if self.queue.off and chain.mode != "fallback":
            for guid in chain.members:
                self._desired.setdefault(guid, chain.mode)
            self._fallback.update(chain.members)
            self._finish(chain)
            return
        if not self.queue.off and chain.mode == "fallback":
            self._finish(chain)
            return
        kind, members = chain.steps[chain.index]
        command_id = uuid.uuid4().hex
        cmd = (LeavePvp(command_id, members) if kind == "leave" else
               Rearm(command_id, members) if kind == "rearm" else
               BotDo(command_id, members, "nc", "-bg,-lfg", target_guid=0, training_run=""))
        if not self._current(chain):
            self._finish(chain)
            return
        chain.index += 1
        # A rearm native refused holds nobody (one body in combat, say): its defaults still follow.
        past = REARM_REFUSED if kind == "rearm" else frozenset()
        try:
            self.queue.submit(cmd, owner=chain.owner,
                              on_done=lambda ok, reason, value: self._done(chain, kind, ok, reason, past, command_id))
            if not self._current(chain):
                # Queue enrollment has its own rank refresh. If that callback
                # invalidated us, cancel the stale prepared entry immediately;
                # the real queue/LivePorts transport barrier remains mandatory.
                self.queue.context_changed()
        except (ActionCapacity, BodyEvidenceUnavailable):
            if self._current(chain):
                chain.index -= 1
                chain.waiting = True
        except ActionRejected:
            self._finish(chain)

    def _done(self, chain, kind, ok, reason, past, command_id=None):
        if reason == "transport_unknown":
            chain.waiting = True
            chain.awaiting_terminal = True
            return
        chain.awaiting_terminal = False
        named = leave_refusal_guids(reason, chain.members) if kind == "leave" and not ok else None
        if not named:
            if kind == "leave":
                try:
                    record = self.queue.store.get(command_id)
                except BodyEvidenceUnavailable:
                    self._finish(chain)
                    return
                skipped = set(leave_skipped_guids(record)) & set(chain.members) if record else set()
                if not ok:
                    self._retry_released(chain, record, skipped)
                    return
                if skipped:
                    self._leave_out(chain, skipped)
            if kind == "defaults" and not ok:
                # Native ran the batch and skipped a member or two (dead, busy): the others took their
                # defaults and nobody is held, so the chain's later batches still go, and the skipped
                # get theirs on a later pass. An aborted batch (cancelled, timed out) stops it as before.
                try:
                    record = self.queue.store.get(command_id)
                except BodyEvidenceUnavailable:
                    self._finish(chain)
                    return
                ok = record is not None and defaults_partial(record)
            self._advance(chain, ok or reason in past)
            return
        # Native refused the whole leave at its ack, before changing anyone, because these members
        # hold private Wintergrasp state, have cleanup outside this manifest or cannot leave their
        # battleground group alone. Nothing is held: leave them for a later pass and recall the
        # others as a new command.
        current = self._current(chain)
        self._finish(chain)
        self._keep(named)
        rest = tuple(g for g in chain.members if g not in named)
        # Validated nonempty, distinct manifest members guarantee a strictly smaller retry.
        log.info("realm_recall_left_out reason=%s named=%d rest=%d", reason.partition(":")[0], len(named), len(rest))
        if current and rest:
            self._start(rest, chain.mode)

    def _retry_released(self, chain, record, skipped):
        """A failed leave. Native's release proof says only that it no longer holds those bodies,
        not that they left PvP: a member whose step never ran is released too. So none of them
        gets this chain's rearm or defaults; the released members it did not skip get a new leave
        as a new command, when they are strictly fewer than this chain's (a no-op for those that
        already left). The changed members stay held by the unknown root; the skipped back off."""
        released = (set(leave_released_guids(record)) & set(chain.members)) - skipped if record else set()
        current = self._current(chain)
        self._finish(chain)
        self._keep(chain.members)
        rest = tuple(g for g in chain.members if g in released)
        if not rest or len(rest) >= len(chain.members):
            return  # Every retry strictly shrinks its manifest: a whole-job refusal ends here.
        log.info("realm_recall_released held=%d skipped=%d rest=%d",
                 len(chain.members) - len(rest) - len(skipped), len(skipped), len(rest))
        if current:
            self._start(rest, chain.mode)

    def _keep(self, guids):
        until = self.clock.now() + KEEP_S
        for guid in guids:
            self._kept[guid] = until

    def _leave_out(self, chain, skipped):
        """Native took the others out and left these as they were (one leading its battleground
        group, say), holding them no longer. They are backed off like named refusals; the chain's
        rearm and defaults go on for the rest."""
        self._keep(skipped)
        rest = tuple(g for g in chain.members if g not in skipped)
        for guid in skipped:
            if self._busy.get(guid) == chain.key:
                del self._busy[guid]
        later = ((k, tuple(g for g in members if g not in skipped)) for k, members in chain.steps[chain.index:])
        chain.members = rest
        chain.steps = chain.steps[:chain.index] + tuple((k, members) for k, members in later if members)
        log.info("realm_recall_skipped named=%d rest=%d", len(skipped), len(rest))

    def _start(self, members, mode):
        members = tuple(members)
        owner = "raid" if mode in {"recall", "second"} else "realm_day"
        if mode in {"recall", "second"}:
            # Members a recent leave refusal named are backed off; a leave naming one is
            # rejected whole, so they wait for a later pass and the rest go.
            blocked = self.queue.leave_backed_off(owner)
            members = tuple(g for g in members if g not in blocked)
            if not members:
                return
        steps = []
        if mode in {"recall", "second"}:
            steps.append(("leave", members))
        if mode in {"recall", "defaults"}:
            steps.append(("rearm", members))
        if mode != "second":
            steps.extend(("defaults", members[i:i + 40]) for i in range(0, len(members), 40))
        chain = _Chain(uuid.uuid4().hex, self._ticket(), owner, members, tuple(steps), mode=mode)
        self._chains[chain.key] = chain
        for guid in members:
            self._busy[guid] = chain.key
        self._advance(chain)

    def _flush(self):
        if not self._live():
            return
        ticket = self._ticket()
        online = self._online()
        if online is None:
            return
        if self.queue.off:
            candidates = (self._fallback & online) - self._busy.keys()
            if not candidates:
                return
            eligible = sorted(self._eligible("realm_day", candidates))
            if not self._valid_ticket(ticket):
                return
            for i in range(0, len(eligible), 40):
                if not self._valid_ticket(ticket):
                    return
                batch = [g for g in eligible[i:i + 40] if g not in self._busy]
                if batch:
                    self._fallback.difference_update(batch)
                    self._start(batch, "fallback")
            return
        for mode in ("recall", "defaults", "second"):
            source = self._second if mode == "second" else {g for g, wish in self._desired.items() if wish == mode}
            owner = "raid" if mode in {"recall", "second"} else "realm_day"
            candidates = (set(source) & online) - self._busy.keys()
            if not candidates:
                continue
            eligible = sorted(self._eligible(owner, candidates))
            if not self._valid_ticket(ticket):
                return
            for i in range(0, len(eligible), 150):
                if not self._valid_ticket(ticket):
                    return
                batch = [g for g in eligible[i:i + 150] if g not in self._busy]
                if not batch:
                    continue
                if mode == "second":
                    self._second.difference_update(batch)
                else:
                    for guid in batch:
                        self._desired.pop(guid, None)
                self._start(batch, mode)

    def tick(self, now):
        if not self._live():
            return
        ticket = self._ticket()
        if self.queue.off != self._off:
            self._off = self.queue.off
            if not self._off:
                self._desired = {g: "defaults" for g in self._desired}
            self.restore_defaults()
            if not self._valid_ticket(ticket):
                return
            if not self._off:
                self._fallback.clear()
        self._reconcile()
        for chain in tuple(self._chains.values()):
            if chain.waiting:
                self._advance(chain)
                if not self._valid_ticket(ticket):
                    return
        if self._second_at is not None and now >= self._second_at:
            self._second_at = None
            self._reconcile(all_roots=True)
            online = self._online()
            if online is not None:
                eligible = self._eligible("raid", online - self._busy.keys())
                if not self._valid_ticket(ticket):
                    return
                self._second.update(eligible - self._busy.keys())
        if now - self._last_defaults >= 1800 and not self.ports.human_online():
            self.restore_defaults()
        if self._valid_ticket(ticket):
            self._flush()
