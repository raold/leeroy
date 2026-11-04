"""Owner-requested raid controller, with clock-driven timers and bounded work.

Event hooks only record input. The app's serial event dispatcher must remain free
to deliver jobs/logout while the independent ticker awaits bridge commands.
"""

import asyncio
from collections import deque
from dataclasses import asdict, dataclass
from enum import StrEnum
import logging
import math
from typing import Protocol
from uuid import uuid4

from leeroy_mind.clock import night_of
from leeroy_mind.talk.turns import TurnKind
from leeroy_mind.wow import is_raid_fight

from .comp import TARGETS, NoTankError, candidates_from, choose_leads, select_comp
from .lines import instruction, public_text, template
from .loot import _get, _put
from .pick import Decline, Hangout, Lead, LeadOnly, Pick, is_end, parse
from .protocol import ClearLockout, FormRaid, RaidControl, SetLootMaster, Summon
from .raids import CATALOG, validate
from .session import RaidPlan, RaidSession
from .store import attendance_counts, bench_streaks, last_led


log = logging.getLogger(__name__)
JOB_TIMEOUT_S = 60.0
STOP_TIMEOUT_S = 5.0
EVENT_LIMIT = 256
QUIET_NIGHT_KEY = "guild:raid-night:quiet-night"
CONSOLE_ACTIONS = ("call", "form", "summon", "start", "stop", "end")
CONSOLE_LOCK_S = 10.0  # how long a console dungeon-clear switch waits for a busy tick


class State(StrEnum):
    IDLE = "idle"
    WAITING_CALL = "waiting_call"
    CALLED = "called"
    FORMING = "forming"
    ASSEMBLING = "assembling"
    RUNNING = "running"
    ENDED = "ended"


@dataclass(frozen=True, slots=True)
class RaidRun:
    plan: RaidPlan
    session: RaidSession
    human_in_raid: bool
    lead_is_human: bool


class RaidListener(Protocol):
    async def on_start(self, run: RaidRun): ...
    async def on_ready(self, run: RaidRun): ...
    async def on_boss(self, ev): ...
    async def on_death(self, ev): ...
    async def on_dc_status(self, ev): ...
    async def on_end(self, outcome: str): ...


class _Stale(Exception):
    pass


class _Failed(Exception):
    pass


class ConsoleRefused(Exception):
    """A console raid action that was not taken; its text says why, in true words."""


_STATE_WORDS = {
    State.IDLE: "no raid night is open yet (it opens when you log in)",
    State.WAITING_CALL: "the raid call has not gone out yet",
    State.CALLED: "the raid call is out, waiting for your pick",
    State.FORMING: "the raid is forming",
    State.ASSEMBLING: "the raid is assembling",
    State.RUNNING: "the raid is under way",
    State.ENDED: "tonight's raid night has ended",
}


def _code(error) -> str:
    """A server's error code as it may be shown: identifier characters only, short."""
    text = "".join(c for c in str(error) if c.isascii() and (c.isalnum() or c in "_:.-"))
    return text[:60] or "no reason given"


class RaidNight:
    def __init__(self, ports, cfg, armory, rng, *, summon_plan=None):
        self.ports, self.cfg, self.armory, self.rng = ports, cfg, armory, rng
        self.summon_plan = summon_plan or (lambda members: (list(members), []))
        self.state = State.IDLE
        self.session = None
        self.run = None
        self._listeners = []
        self._lock = asyncio.Lock()
        self._epoch = 0
        self._present = ports.human_online()
        self._login_seen = False
        self._last_logout = None
        self._disband = None
        self._call_due = self._call_deadline = None
        self._pick = self._lead = None
        self._pick_return_state = State.IDLE
        self._zones = {}
        self._zone_revision = 0
        self._outside_since = None
        self._assemble_due = self._ready_due = self._dc_due = None
        self._countdown_sent = False
        self._assembly_pulls = {}
        self._assembly_ended = {}
        self._assembly_revision = 0
        self._inside = False
        self._arrived = set()
        self._on_time = []
        self._late = []
        self._started = False
        self._forming_group = False
        self._dc_attempted = False
        self._dc_stopped = set()
        self._events = deque()
        self._closing = deque()
        self._votes = []

    @property
    def active(self):
        return bool(self._closing) or self.state in {State.FORMING, State.ASSEMBLING, State.RUNNING}

    def add_listener(self, listener):
        self._listeners.append(listener)

    def _check(self, epoch):
        if epoch != self._epoch or not self._present or not self.ports.human_online():
            raise _Stale

    def _candidates(self):
        members = {g: m for g, m in self.ports.cast().items() if g != self.ports.human_guid}
        personas = {g: self.ports.persona(g) for g in members}
        return candidates_from(members, personas, bench_streaks(self.ports.conn, members),
                               attendance_counts(self.ports.conn, members, self.ports.clock.now() - 30 * 86400),
                               away=set())

    def _leaders(self, candidates, lead=None):
        return choose_leads(candidates, requested=lead.guid if lead else None,
                            rotate=bool(lead and lead.rotate), human_leads=bool(lead and lead.human),
                            human_guid=self.ports.human_guid, last_led=last_led(self.ports.conn))

    def _speaker(self):
        if self.run:
            return self.run.plan.tank_guid if self.run.lead_is_human else self.run.plan.lead_guid
        try:
            return self._leaders(self._candidates()).lead_guid
        except NoTankError:
            return next((g for g, m in sorted(self.ports.cast().items())
                         if g != self.ports.human_guid and m.online and self.ports.persona(g)), None)

    def _propose(self, key, *, guid=None, channel="guild", kind=TurnKind.CALLOUT, **facts):
        guid = guid if guid is not None else self._speaker()
        if guid is not None and guid != self.ports.human_guid:
            self.ports.propose(guid, channel, public_text(self.ports, instruction(key, **facts)), kind=kind)

    async def _notify(self, name, *args, epoch=None, ended_run=None):
        for listener in tuple(self._listeners):
            if epoch is not None:
                self._check(epoch)
            hook = getattr(listener, name, None)
            if hook is not None:
                try:
                    if name == "on_end" and hasattr(listener, "invalidate"):
                        await hook(*args, run=ended_run)
                    else:
                        await hook(*args)
                except Exception:
                    # A listener's exception text can contain chat/model inputs.
                    log.warning("guild raid listener failed: %s", name)
        if epoch is not None:
            self._check(epoch)

    async def on_hello(self, ev):
        if not self._login_seen and not self.active:
            self.state = State.IDLE
            self._call_due = None

    async def on_login(self, ev):
        if ev.guid != self.ports.human_guid or not ev.human:
            return
        if self._login_seen and self._present:
            return
        self._epoch += 1
        self._present = self._login_seen = True
        now = self.ports.clock.now()
        self._disband = None
        if self._last_logout is None or now - self._last_logout > self.cfg.disband_after_logout_min * 60:
            self._disband = (now, self._epoch)
        self._call_due = now + self.cfg.raid_call_delay_s + self.rng.uniform(0, self.cfg.raid_call_jitter_s)
        self._call_deadline = None
        self._lead = None
        self.state = State.WAITING_CALL
        if _get(self.ports.conn, QUIET_NIGHT_KEY) == night_of(self.ports.clock.local()):
            self.state = State.ENDED
            self._call_due = None

    def _end(self, outcome):
        forming = self.state is State.FORMING and self._forming_group
        if self.run is not None and hasattr(self.ports, "invalidate_run"):
            self.ports.invalidate_run(self.run)
        for listener in tuple(self._listeners):
            invalidate = getattr(listener, "invalidate", None)
            if invalidate is not None and self.run is not None:
                invalidate(self.run)
        self._epoch += 1
        self.state = State.ENDED
        self._pick = None
        self._call_due = self._call_deadline = None
        self._ready_due = self._dc_due = None
        self._countdown_sent = False
        self._assembly_pulls.clear()
        self._assembly_ended.clear()
        self._late = []
        if self.session is not None:
            # Preserve inputs received before the end event, in delivery order.
            # New event hooks no longer enqueue because state is already ENDED.
            self._closing.append((self.session, self.run, outcome, self._events, self._dc_attempted))
            self.session = self.run = None
        self._events = deque()
        self._started = False
        if forming:
            self._disband = (self.ports.clock.now(), self._epoch)
        self._forming_group = False
        self._dc_attempted = False

    async def on_logout(self, ev):
        if ev.guid != self.ports.human_guid or not ev.human or not self._present:
            return
        self._present = False
        self._last_logout = self.ports.clock.now()
        self._end("logout")
        self._disband = (self._last_logout + self.cfg.disband_after_logout_min * 60, self._epoch)

    def _owner_chat(self, ev):
        return (ev.from_guid == self.ports.human_guid and not ev.via_bridge
                and self._present and self.ports.human_online()
                and (ev.channel in {"guild", "officer", "party", "raid"} and ev.to_guid == 0
                     or ev.channel == "whisper" and ev.to_guid in self.ports.cast()))

    def _lapse_due(self):
        if (self.state is State.CALLED and self._call_deadline is not None
                and self.ports.clock.now() >= self._call_deadline):
            self._propose("call_lapsed")
            self._end("lapsed")

    def _choose(self, choice):
        self._epoch += 1
        self._pick_return_state = self.state
        self._pick = Pick(choice.raid_key, choice.size, choice.heroic, choice.lead or self._lead)
        self.state = State.FORMING

    async def on_chat(self, ev):
        if not self._owner_chat(ev):
            return
        self._lapse_due()
        if self.active and is_end(ev.text):
            self._end("called")
            return
        if self.active:
            if self.session is not None:
                self._enqueue("chat", ev)
            return
        officers = {m.name: g for g, m in self.ports.cast().items()
                    if self.ports.persona(g) is not None
                    and self.ports.persona(g).play.guild_role == "officer"}
        choice = parse(ev.text, officers, require_intent=self.state is not State.CALLED)
        if isinstance(choice, Decline) and self.state is State.CALLED:
            self._propose("raid_declined")
            self._end("declined")
        elif isinstance(choice, Hangout) and self.state in {State.CALLED, State.IDLE, State.WAITING_CALL}:
            with self.ports.conn:
                _put(self.ports.conn, QUIET_NIGHT_KEY, night_of(self.ports.clock.local()))
            self._propose("hangout_called", human=self.ports.human_name())
            self._end("hangout")
        elif isinstance(choice, LeadOnly):
            self._lead = choice.lead
        elif isinstance(choice, Pick):
            self._choose(choice)

    async def on_zone(self, ev):
        if ev.guid == self.ports.human_guid and self._zones.get(ev.guid) != ev.zone_id:
            self._zone_revision += 1
        if ev.guid == self.ports.human_guid or self.run and ev.guid in self.run.plan.members:
            self._zones[ev.guid] = ev.zone_id
        if self.run and ev.guid == self.ports.human_guid:
            inside = ev.zone_id == CATALOG[self.run.plan.raid_key].zone_id
            if inside:
                self._outside_since = None
            elif self._outside_since is None:
                self._outside_since = self.ports.clock.now()
            if not inside and self.state is State.ASSEMBLING:
                self._inside = False
                self._ready_due = self._dc_due = None

    def _enqueue(self, name, ev):
        item = (self.run, name, ev)
        if item in self._events:
            return
        if len(self._events) >= EVENT_LIMIT:
            cosmetic = next((old for old in self._events if old[1] in {"death", "dc_status"}), None)
            if cosmetic is not None:
                self._events.remove(cosmetic)
            elif name in {"death", "dc_status"}:
                return
            else:
                # Stop accepting new work rather than discard an earned kill or
                # loot/claim already delivered. The close queue is bounded +1.
                self._events.append(item)
                self._end("event_overflow")
                return
        self._events.append(item)

    def _event(self, name, ev):
        if self.state in {State.ASSEMBLING, State.RUNNING} and self.run is not None:
            session, plan = self.run.session, self.run.plan
            assembling = self.state is State.ASSEMBLING
            if assembling and (type(session.ctx.instance_id) is not int or session.ctx.instance_id <= 0
                    or _get(self.ports.conn, f"guild:raid:{session.raid_id}:instance") != session.ctx.instance_id
                    or type(ev.ts) is not int or ev.ts <= 0
                    or not -5 <= self.ports.clock.now() - ev.ts / 1000 <= 60):
                return
            if ev.ts and ev.ts / 1000 < session.started - 5:
                return
            if name in {"boss", "loot_drop"}:
                if ev.map != session.ctx.map_id or ev.instance_id != session.ctx.instance_id:
                    return
                if name == "boss" and (ev.state not in {"engaged", "killed", "wiped"}
                        or not (set(plan.members) | {self.ports.human_guid}).intersection(ev.members)):
                    return
                if assembling and name == "boss" and not is_raid_fight(ev.map, ev.boss_id):
                    return
            elif name == "death" and (ev.map != session.ctx.map_id or ev.guid not in {*plan.members, self.ports.human_guid}):
                return
            elif name == "resurrect" and ev.guid not in {*plan.members, self.ports.human_guid}:
                return
            elif name == "dc_status" and ev.tank_guid != plan.tank_guid:
                return
            if assembling and name == "boss":
                self._assembly_phase(ev)
            self._enqueue(name, ev)

    def _assembly_phase(self, ev):
        """A proved manual pull owns this interval; duplicates never extend it."""
        prior = self._assembly_pulls.get(ev.boss_id)
        ended = self._assembly_ended.get(ev.boss_id, 0)
        if ev.state == "engaged":
            if prior is not None or ev.ts <= ended:
                return
            if not self._assembly_pulls:
                self._assemble_due = self.ports.clock.now() + self.cfg.assemble_timeout_min * 60
            self._assembly_pulls[ev.boss_id] = ev.ts
            self._assembly_revision += 1
            self._ready_due = self._dc_due = None
            self._countdown_sent = False
            for listener in self._listeners:
                cancel = getattr(listener, "cancel_ready", None)
                if cancel is not None:
                    cancel(self.run)
        elif ev.ts > ended and (prior is None or ev.ts >= prior):
            self._assembly_revision += 1
            self._assembly_ended[ev.boss_id] = ev.ts
            self._assembly_pulls.pop(ev.boss_id, None)
            if not self._assembly_pulls:
                self._ready_due = self._dc_due = None
                self._countdown_sent = False
                self._assemble_due = self.ports.clock.now() + self.cfg.assemble_timeout_min * 60

    async def on_boss(self, ev):
        self._event("boss", ev)

    async def on_death(self, ev):
        self._event("death", ev)

    async def on_resurrect(self, ev):
        self._event("resurrect", ev)

    async def on_dc_status(self, ev):
        self._event("dc_status", ev)

    async def on_loot_drop(self, ev):
        self._event("loot_drop", ev)

    async def _ack(self, command, epoch):
        self._check(epoch)
        try:
            ack = await self.ports.send(command)
        except Exception:
            self._check(epoch)
            raise _Failed(type(command).__name__ + " acknowledgement was unavailable") from None
        self._check(epoch)
        return ack

    async def _send(self, command, epoch):
        ack = await self._ack(command, epoch)
        if ack is None or ack.id != command.id or ack.ok is not True:
            raise _Failed("server rejected " + type(command).__name__)
        return ack

    async def _job(self, command, kind, epoch):
        await self._send(command, epoch)
        try:
            result = await self.ports.wait_job(command.id, JOB_TIMEOUT_S)
        except Exception:
            self._check(epoch)
            raise _Failed(kind + " completion was unavailable") from None
        self._check(epoch)
        if result is None or result.id != command.id or result.kind != kind or result.state != "done":
            raise _Failed(kind + " did not complete")
        requested = set(command.members if isinstance(command, FormRaid) else command.guids)
        accepted, skipped = set(result.guids), [row.guid for row in result.skipped]
        if (len(accepted) != len(result.guids) or len(set(skipped)) != len(skipped)
                or accepted & set(skipped) or accepted | set(skipped) != requested
                or any(type(g) is not int for g in (*result.guids, *skipped))):
            raise _Failed(kind + " returned invalid membership")
        return result

    def _waves(self, members):
        try:
            on_time, late = self.summon_plan(list(members))
            on_time, late = list(on_time), list(late)
            ids = on_time + [g for g, _ in late]
            if (len(ids) != len(set(ids)) or set(ids) != set(members)
                    or any(type(g) is not int for g in ids)
                    or any(type(delay) not in (int, float) or not math.isfinite(delay) or delay < 0 for _, delay in late)):
                raise ValueError
        except Exception:
            raise _Failed("invalid arrival plan") from None
        return on_time, late

    async def _form(self, epoch):
        pick = self._pick
        raid = CATALOG[pick.raid_key]
        formed = False
        try:
            candidates = self._candidates()
            size = pick.size or (25 if sum(c.available for c in candidates) >= 22 else 10)
            # The real armory runs a MySQL subprocess. Keep that blocking read
            # off both the event dispatcher and the event loop.
            try:
                tier = await asyncio.to_thread(self.armory.human_tier, self.ports.human_guid)
            except Exception:
                self._check(epoch)
                tier = None
            self._check(epoch)
            error = validate(raid, size, pick.heroic, tier)
            if error:
                self._propose("pick_invalid", raid=raid.name, reason=error if tier is not None else "progression could not be checked")
                self.state = self._pick_return_state
                self._pick = None
                return
            pick = Pick(pick.raid_key, size, pick.heroic, pick.lead)
            self._call_due = self._call_deadline = None
            # Availability may have changed during the off-thread armory read.
            candidates = self._candidates()
            leaders = self._leaders(candidates, pick.lead)
            required = {leaders.tank_guid, leaders.lead_guid, leaders.loot_guid} - {self.ports.human_guid}
            comp = select_comp(pick.size, candidates, must_include=sorted(required), human_in_raid=True,
                               seed=self.rng.getrandbits(64))
            members, bench = list(comp.members), list(comp.bench)
            by_guid = {c.guid: c for c in candidates}
            clear = ClearLockout(uuid4().hex, raid.map_id, tuple([*members, self.ports.human_guid]))
            ack = await self._ack(clear, epoch)
            if ack is None or ack.id != clear.id:
                raise _Failed("invalid lockout acknowledgement")
            if ack.ok is not True:
                if not ack.error.startswith("in_instance:"):
                    raise _Failed("server rejected ClearLockout")
                await self._send(ClearLockout(uuid4().hex, raid.map_id, tuple(members)), epoch)

            # FormRaid reuses owned groups and does not remove unrequested cast.
            # Clear that group once so accepted requested GUIDs are the full raid.
            disband = RaidControl(uuid4().hex, self.ports.human_guid, "disband")
            ack = await self._ack(disband, epoch)
            if (ack is None or ack.id != disband.id
                    or ack.ok is not True and ack.error != "no_group"):
                raise _Failed("could not clear the previous group")

            for attempt in range(2):
                assistants = tuple(g for g in members if by_guid[g].guild_role == "officer" or g in required)
                command = FormRaid(uuid4().hex, self.ports.human_guid, tuple(members), pick.size,
                                   raid.map_id, leaders.tank_guid, assistants, heroic=pick.heroic)
                # An accepted command can form a group even if its later job is malformed.
                formed = True
                self._forming_group = True
                result = await self._job(command, "form_raid", epoch)
                accepted = set(result.guids)
                if not required <= accepted:
                    raise _Failed("a required officer could not join")
                missing = [g for g in members if g not in accepted]
                members = [g for g in members if g in accepted]
                if attempt == 0 and missing:
                    additions = []
                    for missing_guid in missing:
                        replacement = next((g for g in bench if by_guid[g].role == by_guid[missing_guid].role), None)
                        if replacement is not None:
                            members.append(replacement)
                            additions.append(replacement)
                            bench.remove(replacement)
                    if set(members) != accepted:
                        await self._send(ClearLockout(uuid4().hex, raid.map_id, tuple(additions)), epoch)
                        continue
                break
            await self._send(SetLootMaster(uuid4().hex, self.ports.human_guid, leaders.loot_guid), epoch)
            on_time, late = self._waves(members)
            summoner = next((g for g in members if by_guid[g].cls == 9), leaders.lead_guid)
            if on_time:
                entrance = await self._job(Summon(uuid4().hex, raid.map_id, tuple(on_time),
                                                  summon_human=True, summoner_guid=summoner), "summon", epoch)
                if set(entrance.guids) != set(on_time):
                    raise _Failed("arrival summon skipped members")
            else:
                raise _Failed("arrival plan has no on-time members")
            speaker = leaders.tank_guid if leaders.lead_guid == self.ports.human_guid else leaders.lead_guid
            if pick.lead and not pick.lead.human:
                self._propose("taking_lead", guid=speaker, human=self.ports.human_name())
            shortfall = {role: max(0, target - sum(by_guid[g].role == role for g in members)
                                  - int(role == "damage"))
                         for role, target in zip(("tank", "healer", "damage"), TARGETS[pick.size])}
            self._propose("raid_forming", guid=speaker, channel="raid", raid=raid.name, size=pick.size,
                          short=", ".join(f"{n} {r}" for r, n in shortfall.items() if n) or "none")
            plan = RaidPlan(raid.key, pick.size, pick.heroic, False, leaders.lead_guid, leaders.tank_guid,
                            leaders.loot_guid, members, bench, True)
            session = await self._open_session(plan, epoch)
            if epoch != self._epoch or not self._present or not self.ports.human_online():
                self._closing.append((session, None, "logout" if not self._present else "cancelled", deque(), False))
                raise _Stale
            self.session = session
            self.run = RaidRun(session.plan, session, True, leaders.lead_guid == self.ports.human_guid)
            now = self.ports.clock.now()
            self._on_time, self._late = on_time, [(g, now + delay) for g, delay in late]
            self._arrived, self._inside, self._started = set(), False, False
            self._dc_attempted = False
            self._ready_due = self._dc_due = self._outside_since = None
            self._assemble_due = now + self.cfg.assemble_timeout_min * 60
            self.state = State.ASSEMBLING
            self._forming_group = False
        except (NoTankError, ValueError, _Failed) as error:
            self._check(epoch)
            reason = str(error) if isinstance(error, _Failed) else "the available roster cannot fill this raid"
            self._propose("pick_invalid", raid=raid.name, reason=reason)
            self._end("failed")
            if formed:
                self._disband = (self.ports.clock.now(), self._epoch)

    async def _open_session(self, plan, epoch):
        before = {r[0] for r in self.ports.conn.execute("SELECT raid_id FROM guild_raid WHERE ended IS NULL")}
        try:
            return await RaidSession.open(self.ports, self.cfg, self.armory, plan)
        except BaseException as error:
            # Task11 commits attendance before its awaited bench-memory copies.
            # Recover only a newly created row with our exact saved plan; never
            # adopt somebody else's active raid or resend formation on retry.
            for row in self.ports.conn.execute("SELECT raid_id FROM guild_raid WHERE ended IS NULL").fetchall():
                raid_id = row[0]
                if raid_id not in before and _get(self.ports.conn, f"guild:raid:{raid_id}:plan") == asdict(plan):
                    session = RaidSession(self.ports, self.cfg, self.armory, plan, raid_id)
                    self._closing.append((session, None, "cancelled" if isinstance(error, asyncio.CancelledError)
                                          else "failed", deque(), False))
            if isinstance(error, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                raise
            self._check(epoch)
            raise _Failed("raid attendance could not be opened") from None

    async def _summon_inside(self, members, epoch):
        run = self.run
        raid = CATALOG[run.plan.raid_key]
        result = await self._job(Summon(uuid4().hex, raid.map_id, tuple(members), where="inside"), "summon", epoch)
        if (set(result.guids) != set(members) or type(result.instance_id) is not int
                or not 0 < result.instance_id <= 0xFFFFFFFF or not run.session.bind_instance(result.instance_id)):
            raise _Failed("inside summon did not verify this instance")
        self._arrived.update(result.guids)

    async def _assemble(self, epoch):
        raid = CATALOG[self.run.plan.raid_key]
        now = self.ports.clock.now()
        revision = self._zone_revision
        phase_revision = self._assembly_revision
        if now >= self._assemble_due:
            self._end("assemble_timeout")
            return
        if self._assembly_pulls:
            # Existing assembly timeout, refreshed once at the observed pull,
            # still bounds a lost terminal event. Session/loot keep ticking.
            return
        if self._zones.get(self.ports.human_guid) != raid.zone_id:
            self._ready_due = self._dc_due = None
            return
        if not self._inside:
            # Even a zone event for every bot cannot establish instance identity.
            # Zone alone cannot prove a bot is in this group's instance either.
            await self._summon_inside(self._on_time, epoch)
            if self._zone_revision != revision or self._assembly_revision != phase_revision:
                return
            self._inside = True
        plan = self.run.plan
        required = {plan.tank_guid, plan.lead_guid, plan.loot_guid} - {self.ports.human_guid}
        if not required <= self._arrived:
            # A late officer must actually arrive before readiness can advance.
            return
        if self.run.lead_is_human:
            if not self._started:
                self._started = True
                await self._notify("on_start", self.run, epoch=epoch)
            if self._zone_revision != revision or self._assembly_revision != phase_revision:
                return
            self.ports.set_lead(self.ports.human_guid)
            self.state = State.RUNNING
            return
        if self._dc_due is not None:
            if not self._countdown_sent and now >= self._dc_due - 5:
                self._countdown_sent = True
                await self._notify("on_ready", self.run, epoch=epoch)
                if self._zone_revision != revision or self._assembly_revision != phase_revision:
                    return
                # The countdown's actual delivery may take time. Never enable
                # DC before its five seconds have elapsed.
                self._dc_due = max(self._dc_due, self.ports.clock.now() + 5)
            if now >= self._dc_due:
                # Deliver the timed 'go' before DC; independent listener ticks
                # may already have done so, and both paths are idempotent.
                await self._notify("on_go", self.run, epoch=epoch)
                if self._zone_revision != revision or self._assembly_revision != phase_revision:
                    return
                # An uncertain acknowledgement can still mean DC was enabled.
                self._dc_attempted = True
                self.state = State.RUNNING
                await self._send(RaidControl(uuid4().hex, self.ports.human_guid, "dc_on", plan.tank_guid), epoch)
                if self._zone_revision != revision:
                    return
                self.ports.set_lead(plan.lead_guid)
                self.state = State.RUNNING
            return
        if self._ready_due is None:
            self._ready_due = now + self.cfg.ready_check_delay_s
        if now >= self._ready_due:
            ready_key = f"guild:raid:{self.session.raid_id}:ready-attempt"
            with self.ports.conn:
                _put(self.ports.conn, ready_key, int(_get(self.ports.conn, ready_key) or 0) + 1)
            await self._send(RaidControl(uuid4().hex, self.ports.human_guid, "ready_check", plan.lead_guid), epoch)
            if self._zone_revision != revision or self._assembly_revision != phase_revision:
                self._ready_due = None
                return
            await self.ports.say(plan.lead_guid, "raid", template("ready_check"))
            self._check(epoch)
            if self._assembly_revision != phase_revision:
                return
            if not self._started:
                self._started = True
                await self._notify("on_start", self.run, epoch=epoch)
            if self._zone_revision != revision or self._assembly_revision != phase_revision:
                return
            timed_countdown = any(hasattr(listener, "on_go") for listener in self._listeners)
            self._countdown_sent = not timed_countdown
            if not timed_countdown:
                # Preserve the standalone controller's lifecycle contract for
                # listeners which do not implement a clock-driven countdown.
                await self._notify("on_ready", self.run, epoch=epoch)
                if self._zone_revision != revision or self._assembly_revision != phase_revision:
                    return
            self._dc_due = self.ports.clock.now() + max(5 if timed_countdown else 0, self.cfg.dc_start_delay_s)

    async def _arrivals(self, epoch):
        phase_revision = self._assembly_revision
        for guid, due in tuple(self._late):
            self._check(epoch)
            if self._assembly_pulls or self._assembly_revision != phase_revision:
                return
            if due > self.ports.clock.now():
                continue
            self._late.remove((guid, due))  # A failed/unknown job is never automatically resent.
            raid = CATALOG[self.run.plan.raid_key]
            if self._zones.get(self.ports.human_guid) == raid.zone_id:
                await self._summon_inside([guid], epoch)
            else:
                result = await self._job(Summon(uuid4().hex, raid.map_id, (guid,)), "summon", epoch)
                if result.guids != (guid,):
                    raise _Failed("late arrival did not complete")
                # This wave reached the entrance, not this raid's instance.
                # Include it in the owner's eventual inside summon as well.
                self._on_time.append(guid)
                self._inside = False
            # Keep the completed arrival's accounting, but a manual pull which
            # began during its job wait owns the rest of this assembly interval.
            # Unsent members stay queued; the already-dispatched member never retries.
            if self._assembly_pulls or self._assembly_revision != phase_revision:
                return
            self._propose("late_arrival", guid=guid, channel="raid", kind=TurnKind.ASIDE)

    async def _drain_events(self, epoch, *, events=None, current=None):
        events = self._events if events is None else events
        current = self.run if current is None else current
        while events:
            item = events[0]
            run, name, ev = item
            if run is not current:
                events.popleft()
                continue
            session, plan = run.session, run.plan
            if ev.ts and ev.ts / 1000 < session.started - 5:
                events.popleft()
                continue
            members = set(plan.members) | {self.ports.human_guid}
            if name == "chat":
                await session.on_chat(ev)
            elif name in {"boss", "loot_drop"}:
                if ev.map != session.ctx.map_id or ev.instance_id != session.ctx.instance_id:
                    events.popleft()
                    continue
                if name == "boss" and (not members.intersection(ev.members) or ev.state not in {"engaged", "killed", "wiped"}):
                    events.popleft()
                    continue
                if name == "boss":
                    await session.on_boss(ev, can_announce=lambda: current is self.run and self.active)
                elif current is self.run and self.active:
                    await session.on_loot_drop(ev)
                if name == "boss":
                    # Finish delivery of this already accepted event in order,
                    # even when an end hook ran while its bookkeeping awaited.
                    if current is self.run and self.active:
                        await self._notify("on_boss", ev)
            elif name == "death":
                if current is self.run and self.active and ev.map == session.ctx.map_id and ev.guid in members:
                    await self._notify("on_death", ev)
            elif name == "resurrect" and ev.guid in members and current is self.run and self.active:
                await self._notify("on_resurrect", ev)
            elif name == "dc_status" and ev.tank_guid == plan.tank_guid and current is self.run and self.active:
                await self._notify("on_dc_status", ev)
            events.popleft()
            if epoch is not None:
                self._check(epoch)

    async def _close_pending(self):
        while self._closing:
            session, run, outcome, events, stop_dc = self._closing[0]
            # A retained run still owns its applied tactics. Ordinary listener
            # notification ignores return values and is not a cleanup barrier.
            if run is not None and not await self._before_end(run, outcome):
                return
            if stop_dc and run is not None and not run.lead_is_human:
                await self._stop_dc(run)
            await self._drain_events(None, events=events, current=run)
            vote = await session.close(outcome)
            if vote is not None and vote not in self._votes:
                self._votes.append(vote)
            self._closing.popleft()
            if run is not None:
                await self._notify("on_end", outcome, ended_run=run)

    async def _before_end(self, run, outcome):
        for listener in tuple(self._listeners):
            hook = getattr(listener, "on_before_end", None)
            if hook is None:
                continue
            try:
                async with asyncio.timeout(20):
                    if await hook(run, outcome) is not True:
                        return False
            except Exception:
                log.warning("guild raid cleanup remains pending")
                return False
        return True

    async def _stop_dc(self, run):
        raid_id = run.session.raid_id
        if raid_id in self._dc_stopped:
            return
        self._dc_stopped.add(raid_id)  # A close retry never repeats an uncertain stop.
        if self.run is not None and self.run is not run:
            return
        if self.ports.conn.execute("SELECT 1 FROM guild_raid WHERE ended IS NULL AND raid_id<>?", (raid_id,)).fetchone():
            return
        tank = self.ports.cast().get(run.plan.tank_guid)
        if tank is None or not tank.online:
            log.warning("guild raid DC stop skipped: tank unavailable")
            return
        # The owner may have logged out; the owned tank still identifies the old
        # group. This cleanup never adopts a new generation's selected leader.
        command = RaidControl(uuid4().hex, run.plan.tank_guid, "dc_off", run.plan.tank_guid)
        try:
            ack = await asyncio.wait_for(self.ports.send(command), timeout=STOP_TIMEOUT_S)
            if ack is None or ack.id != command.id or ack.ok is not True:
                log.warning("guild raid DC stop was not acknowledged")
        except Exception:
            log.warning("guild raid DC stop could not complete")

    async def _disband_due(self):
        if self._closing:
            return
        pending = self._disband
        if pending is None or pending[0] > self.ports.clock.now():
            return
        self._disband = None
        if pending[1] != self._epoch:
            return
        try:
            await self.ports.send(RaidControl(uuid4().hex, self.ports.human_guid, "disband"))
        except Exception:
            log.warning("guild raid disband failed")

    async def tick(self):
        if self._lock.locked():
            return
        async with self._lock:
            epoch = self._epoch
            try:
                await self._close_pending()
                if self._closing:
                    return  # No replacement formation or disband before cleanup.
                await self._disband_due()
                for vote in tuple(self._votes):
                    if await vote.tick(self.ports.clock.now()):
                        self._votes.remove(vote)
                self._check(epoch)
                now = self.ports.clock.now()
                if self.state is State.WAITING_CALL and now >= self._call_due:
                    candidates = self._candidates()
                    counts = {role: sum(c.available and c.role == role for c in candidates)
                              for role in ("tank", "healer", "damage")}
                    self._propose("raid_call", human=self.ports.human_name(), suggest="ICC",
                                  tanks=counts["tank"], healers=counts["healer"], damage=counts["damage"])
                    self.state = State.CALLED
                    self._call_deadline = now + self.cfg.pick_window_min * 60
                elif self.state is State.CALLED and now >= self._call_deadline:
                    self._propose("call_lapsed")
                    self._end("lapsed")
                elif self.state is State.FORMING:
                    await self._form(epoch)
                elif self.state in {State.ASSEMBLING, State.RUNNING}:
                    if self.state is State.ASSEMBLING and self._events:
                        if not self._started:
                            self._started = True
                            await self._notify("on_start", self.run, epoch=epoch)
                        await self._drain_events(epoch)
                    if not self._assembly_pulls:
                        await self._arrivals(epoch)
                    if self.state is State.ASSEMBLING:
                        await self._assemble(epoch)
                    elif self._outside_since is not None and now - self._outside_since >= 600:
                        self._end("called")
                    if self.session is not None:
                        await self._drain_events(epoch)
                        await self.session.tick()
                await self._close_pending()
                await self._disband_due()
            except _Stale:
                # Logout/login hooks own the new state; never write it back here.
                return
            except _Failed as error:
                if epoch == self._epoch:
                    self._propose("pick_invalid", raid=CATALOG[self.run.plan.raid_key].name if self.run else "raid",
                                  reason=str(error))
                    self._end("failed")
                    self._disband = (self.ports.clock.now(), self._epoch)
                    await self._close_pending()
                    await self._disband_due()
            except asyncio.CancelledError:
                if epoch == self._epoch:
                    self._end("cancelled")
                # A cancelled open may already own a committed attendance row.
                # Settle only local/session cleanup, never resume game commands.
                try:
                    await self._close_pending()
                except Exception:
                    log.warning("guild raid cancellation cleanup remains pending")
                raise

    # -- the owner's console (leeroy_mind.endpoint) --------------------------------------------
    # Each action enters this controller where his own chat or one of its timers would, with the
    # same guards, on the same event loop: call brings the call's timer forward, form is his
    # pick, summon brings the late arrivals' timers forward, start brings the ready check (or
    # the countdown) forward, end is his "calling it" (or, before a pick, his decline). Only
    # stop and a later start talk to the game themselves: the dungeon-clear switch the raid's
    # own start and close send. Nothing runs while he is not in game.

    @staticmethod
    def _left(due, now):
        return None if due is None else max(0.0, round(due - now, 1))

    def console_refusal(self, action):
        """Why `action` cannot run now, in true words; None when it can."""
        if not (self._present and self.ports.human_online()):
            return "you're not in game: guild life runs raid nights only while you are"
        state, run = self.state, self.run
        busy = state in {State.FORMING, State.ASSEMBLING, State.RUNNING}
        if action == "call":
            return None if state is State.WAITING_CALL else f"no call to make: {_STATE_WORDS[state]}"
        if action == "form":
            if busy:
                return f"{_STATE_WORDS[state]}: end it first"
            return "the last raid is still closing" if self._closing else None
        if action == "end":
            if busy or state is State.CALLED:
                return None
            return ("the last raid is already closing" if self._closing
                    else f"nothing to end: {_STATE_WORDS[state]}")
        if run is None or state not in {State.ASSEMBLING, State.RUNNING}:
            return f"no raid is under way: {_STATE_WORDS[state]}"
        if action == "summon":
            if self._assembly_pulls:
                return "a pull is under way: late members are summoned once it ends"
            return None if self._late else "nobody is still on the way: everyone has been summoned"
        if run.lead_is_human:
            return "you lead tonight: dungeon-clear runs only under a bot lead"
        held = run.session.raid_id in self._dc_stopped
        if action == "stop":
            if state is State.ASSEMBLING:
                return "dungeon-clear has not started yet"
            return "dungeon-clear is already stopped" if held else None
        if state is State.RUNNING:
            return None if held else "dungeon-clear is already running"
        if self._assembly_pulls:
            return "a pull is under way: the ready check waits for it to end"
        if self._dc_due is not None:
            return "the pull countdown is under way" if self._countdown_sent else None
        if self._ready_due is not None:
            return None
        raid = CATALOG[run.plan.raid_key]
        if self._zones.get(self.ports.human_guid) != raid.zone_id:
            return f"you're not in {raid.name} yet: the ready check follows once you are"
        if not self._inside:
            return "the raid is being summoned inside"
        required = {run.plan.tank_guid, run.plan.lead_guid, run.plan.loot_guid} - {self.ports.human_guid}
        if not required <= self._arrived:
            return "waiting for the lead, the main tank and the loot officer to arrive inside"
        return "the ready check is being scheduled: try again in a moment"

    def console_pick(self, raid_key, size, heroic, lead):
        """His pick as his chat would make it: lead None (guild life chooses), "me", "rotate"
        or an officer's guid (as parse resolves only officers' names)."""
        if lead is None:
            chosen = None
        elif lead == "me":
            chosen = Lead(True)
        elif lead == "rotate":
            chosen = Lead(False, None, True)
        else:
            persona = self.ports.persona(lead)
            if (lead == self.ports.human_guid or lead not in self.ports.cast() or persona is None
                    or persona.play.guild_role != "officer"):
                raise ConsoleRefused("only an officer can be asked to lead")
            chosen = Lead(False, lead)
        return Pick(raid_key, size, heroic, chosen)

    async def console_act(self, action, pick=None):
        """Run one console action; what it did, or ConsoleRefused."""
        if action not in CONSOLE_ACTIONS:
            raise ConsoleRefused("unknown raid action")
        if self._present and self.ports.human_online():
            self._lapse_due()  # as any of his own lines would first
        why = self.console_refusal(action)
        if why is not None:
            raise ConsoleRefused(why)
        now = self.ports.clock.now()
        if action == "call":
            self._call_due = now if self._call_due is None else min(self._call_due, now)
            return "the raid call goes out in a moment"
        if action == "form":
            self._choose(pick)
            raid = CATALOG[pick.raid_key]
            mode = (f" {pick.size}" if pick.size else "") + (" heroic" if pick.heroic else "")
            return f"forming {raid.name}{mode}: the raid is checked, formed and summoned now"
        if action == "end":
            if self.state is State.CALLED:
                self._propose("raid_declined")
                self._end("declined")
                return "no raid tonight: the guild has been told"
            self._end("called")
            return "tonight's raid is ending: the standings and the wrap-up follow"
        if action == "summon":
            count = len(self._late)
            self._late = [(guid, min(due, now)) for guid, due in self._late]
            return f"summoning {count} late member{'s' if count != 1 else ''} now"
        if action == "start" and self.state is State.ASSEMBLING:
            if self._dc_due is not None:
                self._dc_due = min(self._dc_due, now)
                return "the pull countdown starts now; dungeon-clear follows it"
            self._ready_due = min(self._ready_due, now)
            return "the ready check goes out now; dungeon-clear starts after the countdown"
        return await self._console_dc("dc_on" if action == "start" else "dc_off")

    async def _console_dc(self, command_action):
        """Dungeon-clear off (stop) or on again (start after a stop), as the raid's own start
        and close switch it, while no tick is talking to the game."""
        action = "start" if command_action == "dc_on" else "stop"
        try:
            await asyncio.wait_for(self._lock.acquire(), CONSOLE_LOCK_S)
        except TimeoutError:
            raise ConsoleRefused("the raid is busy with the game: try again in a moment") from None
        try:
            why = self.console_refusal(action)
            if why is not None:
                raise ConsoleRefused(why)
            run = self.run
            raid_id = run.session.raid_id
            command = RaidControl(uuid4().hex, self.ports.human_guid, command_action, run.plan.tank_guid)
            if command_action == "dc_on":
                # From here the raid's close stops dungeon-clear again, whatever the answer
                self._dc_stopped.discard(raid_id)
            try:
                ack = await self.ports.send(command)
            except Exception:
                ack = None
            if ack is None or ack.id != command.id:
                raise ConsoleRefused(f"the game did not confirm it: dungeon-clear may or may not "
                                     f"have {'started' if action == 'start' else 'stopped'}")
            if ack.ok is not True:
                raise ConsoleRefused(f"the server refused it ({_code(ack.error)})")
            if command_action == "dc_off":
                self._dc_stopped.add(raid_id)  # its close need not stop it again
                return "dungeon-clear stopped: the raid goes on, and Start resumes it"
            return "dungeon-clear resumed"
        finally:
            self._lock.release()

    def console_status(self):
        """The state the console shows, with each action's refusal (None: it can run)."""
        now = self.ports.clock.now()
        run, state = self.run, self.state
        status = {"state": state.value, "closing": bool(self._closing),
                  "present": bool(self._present), "online": bool(self.ports.human_online()),
                  "call_in": self._left(self._call_due, now) if state is State.WAITING_CALL else None,
                  "pick_left": self._left(self._call_deadline, now) if state is State.CALLED else None,
                  "forming": None, "run": None}
        if state is State.FORMING and self._pick is not None:
            status["forming"] = {"raid": self._pick.raid_key, "size": self._pick.size,
                                 "heroic": self._pick.heroic}
        if run is not None:
            plan, raid = run.plan, CATALOG[run.plan.raid_key]
            status["run"] = {
                "raid_id": run.session.raid_id, "raid": plan.raid_key, "size": plan.size,
                "heroic": plan.heroic, "lead": plan.lead_guid, "tank": plan.tank_guid,
                "loot": plan.loot_guid, "lead_is_human": run.lead_is_human,
                "members": list(plan.members), "bench": list(plan.bench),
                "inside": sorted(self._arrived),
                "late": [[guid, self._left(due, now)] for guid, due in self._late],
                "in_zone": self._zones.get(self.ports.human_guid) == raid.zone_id,
                "pull": bool(self._assembly_pulls),
                "ready_in": self._left(self._ready_due, now), "dc_in": self._left(self._dc_due, now),
                "countdown": self._countdown_sent,
                "dc": ("held" if run.session.raid_id in self._dc_stopped
                       else "on" if state is State.RUNNING and not run.lead_is_human else "off"),
                "kills": run.session.kills, "wipes": run.session.wipes}
        status["actions"] = {action: self.console_refusal(action) for action in CONSOLE_ACTIONS}
        return status
