"""Guild ticker lifecycle and a bounded fast-event outbox.

Only the independent guild ticker may await lifecycle methods. Inline app hooks
(pull storage, chat and pulse) never await transport. Integration must tick this
listener independently and honor on_before_end before releasing raid bodies.
"""

import asyncio
from collections import deque
from dataclasses import replace
import re

from leeroy_mind.guild.boss_calls import call_for, parse_dc_status
from leeroy_mind.wow import BOSS_NAMES

from . import store
from .cards import Card, LEVERS, stock_card
from .ledger import combat_role, context_key
from .lines import template
from .playbook import next_change
from .score import is_farm


def held_at_boss(ev):
    state = parse_dc_status(ev.payload)
    fields = ev.payload.split("\t") if isinstance(ev.payload, str) else ()
    return bool(ev.paused and state and state.state == "paused" and len(fields) > 7
                and fields[7].startswith("waiting at "))


class TrainingListener:
    CAPACITY = 64
    APPLY_TIMEOUT = 10.0
    CLEANUP_TIMEOUT = 15.0

    def __init__(self, ports, ledger, gate, applier=None, *, submit=None,
                 lust_provider=None, members_provider=None, farm_basis="deaths_time", prepare_run=None):
        if farm_basis not in {"deaths_time", "mechanics_deaths_time"}:
            raise ValueError("unsupported farm score basis")
        self.ports, self.ledger, self.gate, self.applier = ports, ledger, gate, applier
        self.submit = submit
        self.lust_provider, self.members_provider = lust_provider, members_provider
        self.prepare_run = prepare_run
        self.farm_basis = farm_basis
        self.run = None
        self._muster = None
        self._pull = False
        self.experimenter = None
        self._generation = 0
        self._lock = asyncio.Lock()
        self._outbox = deque()
        self._ending = {}
        self._standing_due = None
        self._closed = False
        self._reset_night()

    def _reset_night(self):
        self._changes, self._wipes, self._applied = {}, {}, {}
        self._last_result_end = {}
        self._farm_at_entry = {}
        self._used, self._old, self._benched = set(), set(), set()
        self._seen, self._drafted = set(), set()
        self._boss = self._fight = self._lust_due = self._pulse_id = None
        self._lust_sent = False
        self._lust_active = False
        self._prepared = self._release_ok = False
        self._old_next = False
        self._start_ready = False
        self._start_waiting = self._start_attempted = False
        self._start_retry_at = 0
        self._effective_context = None

    @staticmethod
    def run_key(run):
        return f"training:raid:{run.session.raid_id}"

    def retained_run(self, key):
        """State providers must retain this exact run through terminal cleanup."""
        if key in self._ending:
            return self._ending[key]
        return self.run if self.run is not None and self.run_key(self.run) == key else None

    def _token(self):
        run = self.run
        return (run, self._generation, getattr(self.ports, "connection_epoch", 0),
                run.session.ctx.instance_id if run else None)

    def _valid(self, token):
        run, generation, epoch, instance = token
        return (not self._closed and run is not None and self.run is run
                and self._generation == generation and not self._ending
                and getattr(self.ports, "connection_epoch", 0) == epoch
                and run.session.ctx.instance_id == instance
                and not getattr(run.session, "_closing", False))

    def _acting(self):
        return bool(self.applier is not None and self.applier.acting(self.ports.clock.now()))

    def _context(self, boss, run=None):
        run = run or self.run
        return context_key(run.session.ctx.map_id, boss,
                           int(run.plan.size == 25) + 2 * int(run.plan.heroic))

    def _members(self, run):
        if self.members_provider is not None:
            return self.members_provider(run)
        from .levers import MemberInfo
        members = {}
        for guid in run.plan.members:
            member = self.ports.cast().get(guid)
            if member is None:
                continue
            persona = self.ports.persona(guid)
            role = combat_role(member.class_, member.spec_tab,
                               persona.play.raid_role if persona else None,
                               tank=guid == run.plan.tank_guid)
            members[guid] = MemberInfo(guid, role, member.class_, member.spec_tab)
        return members

    def set_experimenter(self, experimenter):
        self.experimenter = experimenter

    def _farm(self, context, *, before_ms=None):
        rows = store.pull_rows(self.ports.conn, context=context, scoreable=True)
        return is_farm([r["outcome"] for r in rows if r["complete"] and r["score"] is not None
                        and r["score_basis"] == self.farm_basis and r["kind"] in {"his", "offline"}
                        and (before_ms is None or r["start_ms"] < before_ms)])

    def _run_farm(self, context, run, *, before_ms=None):
        """Freeze eligibility before this context's first pull of the run.

        A farm wipe must not turn the next pull into an untested progression
        experiment. The callback fallback excludes its just-stored pull.
        """
        key = (self.run_key(run), context)
        if key not in self._farm_at_entry:
            cutoff = int(self.ports.clock.now() * 1000) if before_ms is None else before_ms
            self._farm_at_entry[key] = self._farm(context, before_ms=cutoff)
        return self._farm_at_entry[key]

    def card_for(self, context, run):
        stock = stock_card(context)
        if not self._acting():
            return stock, None
        row = store.champion(self.ports.conn, context)
        champion = Card.from_row(row) if row else stock
        farm = self._run_farm(context, run)
        if run.human_in_raid and (context in self._old or farm and self._wipes.get(context, 0) >= 2):
            previous = self.ports.conn.execute("SELECT * FROM tg_card WHERE id=? AND context=?",
                (champion.prev_champion, context)).fetchone()
            if previous is not None and previous["status"] in {"retired", "champion"}:
                # The exact previous champion is tested; 'retired' describes its
                # global slot, not a safety suspension. Applier still validates.
                return replace(Card.from_row(previous), status="champion"), None
            return champion, None
        if farm:
            if (not run.human_in_raid and self.ports.cfg.experiments and self.experimenter is not None
                    and context not in self._benched):
                choice = self.experimenter.arm_for(context, run)
                if choice is not None and self._arm_allowed(context, run, *choice):
                    return choice
            return champion, None
        change = self._changes.get(context)
        if change:
            directive, text = change
            # Champion baseline plus ONE replacement lever. Keep a runtime
            # champion status so candidate cardinality validation remains strict.
            champion = replace(champion, status="champion", directives=tuple(
                d for d in champion.directives if d.lever != directive.lever) + (directive,), call=text)
        return champion, None

    def _arm_allowed(self, context, run, card, arm):
        """Only a persisted comparison can identify an omitted tested lever."""
        if not isinstance(card, Card) or card.context != context or type(arm) is not int or arm <= 0:
            return False
        valid = getattr(self.experimenter, "arm_valid", None)
        if not callable(valid) or not valid(context, arm):
            return False
        row = self.ports.conn.execute(
            "SELECT t.lever,t.scope,t.status AS trial_status,a.status AS arm_status,a.card "
            "FROM tg_arm a JOIN tg_trial t ON t.id=a.trial WHERE a.id=?", (arm,)).fetchone()
        pooled = f"{context.split(':')[0]}:*:{context.rsplit(':', 1)[1]}"
        if (row is None or row["trial_status"] != "open" or row["arm_status"] != "live"
                or row["scope"] not in {context, pooled} or row["card"] != card.id
                or row["lever"] not in {*LEVERS, "stock"}):
            return False
        # Full stock has no surviving directive from which to infer the removed
        # key. Refuse any standing tactic, rather than guessing that key.
        levers = set(LEVERS) if row["lever"] == "stock" else {row["lever"]}
        # An authoritative arm also raises its baseline directives above the
        # standing layer. Refuse every overlap, even an equal value, so moving
        # an unrelated pooled baseline cannot introduce a second treatment.
        levers.update(d.lever for d in card.directives)
        return not self.applier.standing_conflicts(self.run_key(run), self._members(run), levers)

    def change_for(self, context):
        return self._changes.get(context, (None, ""))[1] if self._acting() and self.run is not None and not self._ending else ""

    def set_applier(self, applier):
        self.applier = applier

    async def _call(self, name, *args, **kwargs):
        if self.submit is not None:
            self.submit(name, args, kwargs, None if name == "on_end" else self.run)
        else:
            await getattr(self.gate, name)(*args, **kwargs)

    async def on_start(self, run):
        if self.run is run:
            return
        if self.run is not None:
            self.invalidate(self.run)
        self.run, self._muster, self._pull = run, None, False
        self._generation += 1
        self._reset_night()
        self.ledger.set_run(run)
        await self._call("on_start", run)
        if not self._ending:
            self._start_waiting = True
            async with self._lock:
                await self._start(self._token())

    async def _start(self, token):
        if (not self._valid(token) or not self._acting() or not self._start_waiting
                or self._start_attempted or self._pull or self.ports.clock.now() < self._start_retry_at):
            return
        run = token[0]
        try:
            async with asyncio.timeout(self.APPLY_TIMEOUT):
                if self.prepare_run is not None and not await self.prepare_run(run):
                    self._start_retry_at = self.ports.clock.now() + 5
                    return  # No mutation yet: independent ticker may retry proof lookup.
                if not self._valid(token) or not self._start_waiting or self._pull:
                    return
                # Once a hold may have been sent, ordinary cleanup owns failure;
                # retrying startup would duplicate partially applied journals.
                self._start_attempted = True
                if not run.human_in_raid and not await self.applier.hold(run.plan.tank_guid):
                    return
                if not self._valid(token) or not self._start_waiting or self._pull:
                    return
                await self.applier.apply_standing(run.plan.members, run_key=self.run_key(run))
                if self._valid(token) and self._start_waiting and not self._pull:
                    self._start_ready = await self._pooled(token, self._context(0))
                    self._start_waiting = False
        except TimeoutError:
            self._start_retry_at = self.ports.clock.now() + 5

    async def _pooled(self, token, context):
        run = token[0]
        scope = f"{run.session.ctx.map_id}:*:{context.rsplit(':', 1)[1]}"
        for row in store.card_rows(self.ports.conn, scope, "champion"):
            card = Card.from_row(row)
            if context in card.vetoes or not all(LEVERS.get(d.lever) and LEVERS[d.lever].pooled for d in card.directives):
                continue
            if not self._valid(token):
                return False
            if not await self._apply(run, "pooled", replace(card, context=context)):
                return False
        return self._valid(token)

    async def _apply(self, run, layer, card):
        return await self.applier.apply(self.run_key(run), layer, card.context, card,
            self._members(run), tank_guid=run.plan.tank_guid, lead_is_human=run.lead_is_human)

    def invalidate(self, run):
        self.gate.invalidate(run)
        if self.run is run:
            if self.applier is not None and run is not None:
                self._ending[self.run_key(run)] = run
            self._generation += 1
            self._lust_due = self._fight = None
            self._outbox.clear()
            self.run = None
            self.ledger.set_run(None)
            self._muster, self._pull = None, False

    async def on_end(self, outcome, *, run=None):
        ended = run if run is not None else self.run
        if self.applier is not None:
            if ended is None:
                return True
            if not await self.on_before_end(ended, outcome):
                return False
            if self.run is ended:
                self.invalidate(ended)
            if self.run is None:
                self._reset_night()
            self._ending.pop(self.run_key(ended), None)
            self._standing_due = (self.ports.clock.now() + 15, tuple(ended.plan.members),
                                  self._generation, getattr(self.ports, "connection_epoch", 0))
            # End is scoped explicitly; it must not be dropped as a stale queue
            # item merely because the active run has been cleared.
            await self._call("on_end", outcome, run=ended)
            return True
        if self.run is not None and ended is not self.run:
            return
        self.invalidate(ended)
        await self._call("on_end", outcome, run=ended)

    async def on_before_end(self, run, outcome="ended"):
        """Mandatory release barrier: false means retain bodies and retry.

        Called by the controller before session.close/group reset/body release.
        Unlike normal dispatch, cleanup may target a retained ended run.
        """
        if self.applier is None or run is None:
            return True
        key = self.run_key(run)
        if key not in self._ending:
            self._ending[key] = run
            if self.run is run:
                self._generation += 1
                self._lust_due = self._fight = None
                self._outbox.clear()
        try:
            async with asyncio.timeout(self.CLEANUP_TIMEOUT):
                async with self._lock:
                    await self.applier.end_run(key)
                    return not self.applier.cleanup_pending(key)
        except TimeoutError:
            return False

    async def on_ready(self, run):
        pass

    async def on_death(self, ev):
        pass

    def _fresh(self, ev):
        return (self.run is not None and not getattr(self.run.session, "_closing", False)
            and type(ev.ts) is int and self.run.session.started * 1000 <= ev.ts
            and 0 <= self.ports.clock.now() * 1000 - ev.ts <= 60_000)

    async def on_dc_status(self, ev):
        if self.applier is not None:
            return await self._prepare(ev)
        if (not self._fresh(ev) or self._pull or ev.tank_guid != self.run.plan.tank_guid
                or not ev.enabled or ev.paused):
            return
        state = parse_dc_status(ev.payload)
        if state is None or state.state != "resting" or not state.next_entry:
            return
        map_id = self.run.session.ctx.map_id
        name, _ = call_for(map_id, state.next_name)
        indexes = [index for (map_, index), known in BOSS_NAMES.items() if map_ == map_id and known == name]
        if len(indexes) != 1:
            return  # Creature entries are not encounter indexes; unknown names fail closed.
        key = (indexes[0], state.next_entry)
        if key != self._muster:
            self._muster = key
            await self._call("on_muster", self.run, indexes[0])

    async def on_boss(self, ev):
        if (not self._fresh(ev) or ev.map != self.run.session.ctx.map_id
                or ev.instance_id != self.run.session.ctx.instance_id
                or not set(self.run.plan.members).intersection(ev.members)
                or ev.state not in {"engaged", "killed", "wiped"}):
            return
        if self.applier is not None:
            return await self._boss_event(ev)
        self._pull = ev.state == "engaged"
        if not self._pull:
            self._muster = None  # Same-boss retries require a new resting boundary.
        await self._call("on_boss", ev)

    async def _prepare(self, ev):
        if (not self._fresh(ev) or self._pull or ev.tank_guid != self.run.plan.tank_guid
                or not ev.enabled or self._ending):
            return
        state = parse_dc_status(ev.payload)
        held = held_at_boss(ev)
        if state is None or not state.next_entry or not (state.state == "resting" and not ev.paused or held):
            return
        map_id = self.run.session.ctx.map_id
        name, _ = call_for(map_id, state.next_name)
        indexes = [i for (m, i), known in BOSS_NAMES.items() if m == map_id and known == name]
        if len(indexes) != 1:
            return
        boss = indexes[0]
        token = self._token()
        async with self._lock:
            if not self._valid(token):
                return
            run = token[0]
            if (boss, state.next_entry) != self._muster:
                self._boss, self._muster = boss, (boss, state.next_entry)
                self._prepared = self._release_ok = False
                context = self._context(boss)
                if self._acting() and not self._start_ready:
                    self.ledger.mark_unscoreable(context)
                    return
                if self._old_next:
                    self._old.add(context)
                    self._old_next = False
                    self._queue("old_plan", boss)
                if self._acting():
                    card, arm = self.card_for(context, run)
                    ok = False
                    try:
                        async with asyncio.timeout(self.APPLY_TIMEOUT):
                            await self.applier.revert(self.run_key(run), "arm")
                            if not self._valid(token):
                                return
                            await self.applier.revert(self.run_key(run), "boss")
                            if not self._valid(token):
                                return
                            await self.applier.revert(self.run_key(run), "pooled")
                            if not self._valid(token):
                                return
                            if arm is not None and not self._arm_allowed(context, run, card, arm):
                                row = store.champion(self.ports.conn, context)
                                card, arm = Card.from_row(row) if row else stock_card(context), None
                            # Experimenter cards are the complete merged
                            # baseline. Reinstalling pooled beneath an omitted
                            # trial key would silently turn stock into champion.
                            baseline_ok = arm is not None or await self._pooled(token, context)
                            if baseline_ok and self._valid(token):
                                ok = await self._apply(run, "arm" if arm is not None else "boss", card)
                            if (ok and arm is not None and self._valid(token)
                                    and not self._arm_allowed(context, run, card, arm)):
                                await self.applier.revert(self.run_key(run), "arm")
                                ok = False
                    except TimeoutError:
                        pass
                    if not self._valid(token):
                        return
                    if ok:
                        self._effective_context = context
                        self._applied[context] = (card, arm, self.change_for(context))
                        self.ledger.set_in_play(context, card_id=card.id, arm_id=arm,
                                                change=self.change_for(context))
                        self._release_ok = True
                    else:
                        self._effective_context = None
                        self._applied.pop(context, None)
                        self.ledger.set_in_play(context)
                        self.ledger.mark_unscoreable(context)
                        # Never resume an uncertain/partially mutated apply.
                        # Terminal cleanup is the controller's mandatory barrier.
                else:
                    self.ledger.set_in_play(context)
                self._prepared = True
                await self._call("on_muster", run, boss)
            if held and self._prepared and self._release_ok and self._valid(token) and self._acting():
                if await self.applier.resume_held(run.plan.tank_guid) and self._valid(token):
                    self._release_ok = False

    async def _boss_event(self, ev):
        token = self._token()
        if not self._valid(token):
            return
        context = self._context(ev.boss_id)
        if ev.state == "engaged":
            # Startup may wait for vocabulary/experiment/native proof, but it
            # must never restart a whole setup after the first combat boundary.
            self._start_waiting = False
            if self._pull:
                return
            self._run_farm(context, token[0], before_ms=ev.ts)
            self._pull, self._boss = True, ev.boss_id
            self._fight = (token, ev.boss_id, ev.ts, f"{ev.ts}-{ev.instance_id}-{ev.boss_id}")
            self._lust_sent = False
            self._pulse_id = None
            self._lust_active = self.lust_held(ev.map, ev.boss_id)
            self._lust_due = self.ports.clock.now() + 10 if self._lust_active else None
        else:
            if self._fight is None or self._fight[1] != ev.boss_id or ev.ts < self._fight[2]:
                return
            self._pull, self._muster = False, None
            self._fight = self._lust_due = None
            self._lust_active = False
            self._effective_context = None
            self._prepared = self._release_ok = False
            if ev.state == "wiped":
                self._wipes[context] = self._wipes.get(context, 0) + 1
                if self._applied.get(context, (None, None))[1] is not None:
                    self._benched.add(context)
            async with self._lock:
                if self._valid(token) and (self._acting() or context in self._applied):
                    await self.applier.revert(self.run_key(token[0]), "arm")
                if self._valid(token) and (self._acting() or context in self._applied):
                    await self.applier.revert(self.run_key(token[0]), "boss", context=context)
        if self._valid(token):
            await self._call("on_boss", ev)

    def lust_held(self, map_id, boss_id):
        if (not self._acting() or self.lust_provider is None or self.run is None
                or map_id != self.run.session.ctx.map_id or not self.gate.recording()):
            return False
        applied = self._applied.get(self._context(boss_id))
        return bool(self._effective_context == self._context(boss_id) and applied
                    and any(d.lever == "lust_at" and d.value > 0 for d in applied[0].directives))

    def _queue(self, kind, value):
        if len(self._outbox) >= self.CAPACITY:
            self.invalidate(self.run)
            return False
        self._outbox.append((self._token(), kind, value))
        return True

    async def on_pulse(self, ev):
        fight = self._fight
        if (not self._lust_active or fight is None or not self._valid(fight[0]) or not self._fresh(ev)
                or ev.map != self.run.session.ctx.map_id or ev.instance_id != fight[0][3]
                or ev.boss_id != fight[1] or ev.ts < fight[2] or ev.at_ms < 0):
            return
        # Events and tap sample separate wall-clock calls in their ordered
        # before-state hooks. Pin the first compatible recorder ID, never adopt
        # an earlier pull's pulse or change identity after the first pulse.
        parts = ev.pull.split("-")
        if len(parts) != 3 or any(not p.isdecimal() for p in parts):
            return
        start, instance, boss = map(int, parts)
        if (instance != ev.instance_id or boss != ev.boss_id or not fight[2] <= start <= fight[2] + 1000
                or start > ev.ts or self._pulse_id not in {None, ev.pull}):
            return
        self._pulse_id = ev.pull
        self._lust_due = None
        applied = self._applied.get(self._context(ev.boss_id))
        threshold = next((d.value for d in applied[0].directives if d.lever == "lust_at"), 0) if applied else 0
        if threshold > 0 and ev.boss_hp <= threshold and not self._lust_sent:
            self._lust_sent = self._queue("lust", fight)

    async def on_human_chat(self, ev):
        if (not self._acting() or not self._valid(self._token()) or not self._fresh(ev) or not self.run.human_in_raid
                or self.run.lead_is_human or ev.from_guid != self.ports.human_guid
                or ev.via_bridge or ev.map != self.run.session.ctx.map_id
                or ev.channel not in {"raid", "raid_leader"} or ev.to_guid != 0
                or re.search(r"\bold plan\b", ev.text, re.IGNORECASE) is None):
            return
        if self._boss is None:
            self._old_next = True
            return
        self._old.add(self._context(self._boss))
        if not self._pull:
            self._muster = None  # Next validated status re-prepares the held boss.
        self._queue("old_plan", self._boss)

    async def after_pull(self, rec, ctx, result):
        """After scoring/commit; only local deterministic work on the app lane."""
        run = self.run
        if not self._acting() or run is None or self._ending or not self._valid(self._token()):
            return
        h = rec.header
        if (ctx.raid_id != run.session.raid_id or h.instance_id != run.session.ctx.instance_id
                or h.map != run.session.ctx.map_id or h.start_ms < run.session.started * 1000
                or h.size != run.plan.size or h.difficulty != int(run.plan.size == 25) + 2 * int(run.plan.heroic)
                or h.end_ms <= h.start_ms
                or h.end_ms > self.ports.clock.now() * 1000 or h.id in self._seen):
            return
        row = self.ports.conn.execute("SELECT * FROM tg_pull WHERE id=?", (h.id,)).fetchone()
        if (row is None or not rec.complete or not row["complete"] or not row["scoreable"]
                or row["score"] is None or row["score_basis"] not in {"deaths_time", "mechanics_deaths_time"}
                or row["raid_id"] != ctx.raid_id or row["end_ms"] != h.end_ms
                or row["outcome"] != h.outcome or h.outcome not in {"killed", "wiped"}):
            return
        self._seen.add(h.id)
        context = self._context(h.boss_id)
        if h.end_ms <= self._last_result_end.get(context, -1):
            return
        self._last_result_end[context] = h.end_ms
        if self._run_farm(context, run, before_ms=h.start_ms):
            return
        if h.outcome == "wiped":
            roles = {r["guid"]: r["role"] for r in store.member_rows(self.ports.conn, h.id)}
            applied = self._applied.get(context)
            lust = next((d.value for d in applied[0].directives if d.lever == "lust_at"), 0) if applied else 0
            change = next_change(result, roles, used=self._used, lust_at=lust)
            if change is not None:
                self._changes[context] = change
                self._used.add(change[0].lever)
        elif context in self._changes and context not in self._drafted:
            directive, text = self._changes[context]
            applied = self._applied.get(context)
            # Exact recorded in-play change, not whatever a later wipe chose.
            if (ctx.change != text or row["change"] != text or applied is None
                    or applied[2] != text or directive not in applied[0].directives
                    or row["card_id"] != applied[0].id or row["arm_id"] != applied[1]):
                return
            versions = store.card_rows(self.ports.conn, context)
            now = self.ports.clock.now()
            card = Card(None, context, max((r["version"] for r in versions), default=0) + 1,
                "candidate", "progression", (directive,), call=text, evidence=(h.id,), created=now, changed=now)
            store.put_card(self.ports.conn, card.to_row())
            self._drafted.add(context)

    async def tick(self):
        """Independent owned lane; deadlines use ports.clock, no orphan timers."""
        async with self._lock:
            if self._closed:
                return
            await self._start(self._token())
            fight = self._fight
            if (self._lust_due is not None and self.ports.clock.now() >= self._lust_due
                    and fight is not None and self._valid(fight[0]) and not self._lust_sent):
                self._lust_due = None
                self._lust_sent = self._queue("lust", fight)
            # One operation per tick keeps the lane bounded even under chat.
            if self._outbox:
                token, kind, value = self._outbox.popleft()
                if self._valid(token) and self._acting():
                    try:
                        async with asyncio.timeout(10):
                            if kind == "lust" and self._fight is value and self.lust_provider is not None:
                                await self.lust_provider(token[0])
                            elif kind == "old_plan":
                                await self.ports.say(token[0].plan.lead_guid, "raid",
                                    template("old_plan_ok", boss=BOSS_NAMES.get((token[0].session.ctx.map_id, value), "boss")))
                    except TimeoutError:
                        pass  # A possibly delivered call is never retried.
            if self._standing_due is not None and self.ports.clock.now() >= self._standing_due[0]:
                _, members, generation, epoch = self._standing_due
                self._standing_due = None
                if (self.run is None and not self._ending and self._generation == generation
                        and getattr(self.ports, "connection_epoch", 0) == epoch and self._acting()):
                    await self.applier.apply_standing(members)  # Explicitly deferred (0) until Realm defaults.

    async def close(self):
        """Fence timers/outbox; false requires draining the integration ticker.

        Integration owns and cancels/joins the task calling tick(), then drains
        Applier.close() before releasing its database/transport.
        """
        self._closed = True
        self._generation += 1
        self._lust_due = self._fight = self._standing_due = None
        self._outbox.clear()
        try:
            async with asyncio.timeout(self.CLEANUP_TIMEOUT):
                async with self._lock:
                    return True
        except TimeoutError:
            return False
