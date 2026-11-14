"""The raid lead: a cast member of his group who calls his raid while he stays the game's leader,
and his orders to his group, understood and carried out by the bodies.

Read README.md. In short: the lead is the member he names (console, his raid or party chat, or
his push-to-talk), else the code-built default (designate.py); it lasts until he changes it or
the group breaks, and persists in meta (KEY). No raid assistant flag is ever set. His orders map
onto a fixed list (orders.py); each runs on his group's bots through mod-leeroy's bot_do "order",
and she or the member he named acknowledges only what the game's job confirms. A refused or
impossible order gets a plain "can't do that one". Native's switch is asked (probe/sync): while
it is off, or the worldserver lacks the feature, nothing here leads or speaks. Nothing runs while
a raid the mind leads, an offline raid or a raid being formed holds the group, and nothing here
logs his words, names or persona text.
"""

import asyncio
from collections import deque
from dataclasses import dataclass, field
import json
import logging
from uuid import uuid4

from leeroy_mind.guild.protocol import BotDo, GuildGroup, RaidControl, register as register_guild
from leeroy_mind.guild.roles import is_raid_lead, role_of
from leeroy_mind.llm.client import GpuPaused
from leeroy_mind.llm.scheduler import Priority
from leeroy_mind.logs import log as log_event
from leeroy_mind.protocol import Boss, Chat, Death, Group, Hello, Resurrect
from leeroy_mind.talk.raid import RaidPhase
from leeroy_mind.talk.turns import Turn, TurnKind
from leeroy_mind.wow import boss_name, faction_known

from . import callouts as C
from .config import load
from .designate import default_lead
from .orders import (SYSTEM, AskPlan, Countdown, Designate, Order, Ready, Unclear, normalize, parse,
                     read_answer, schema)
from .protocol import RaidLead as RaidLeadCmd, register

log = logging.getLogger(__name__)
KEY = "raidlead:state"
FEATURE = "raidlead"  # mod-leeroy's hello: the build has bot_do "order" and raid_lead
COMMAND_CHANNELS = frozenset({"party", "raid", "raid_warning"})
PRIVATE_ROOMS = frozenset({"voice:private1", "voice:private2", "voice:private3"})
JOB_S = 6.0  # the bridge ack, then the job's terminal (one bot_do batch per world tick)
MODEL_S = 10.0
CONFIDENT = 0.7  # below this the model's reading is a guess: she asks instead
SPEECH_S = 30.0
QUEUE = 16
CLAIMED = 32
HOLD_S = 30.0  # native lifts a hold by then too (kRaidHoldMs)
LIFT_RETRY_S = 5.0  # a lift a member skipped is tried again after this, doubling up to LIFT_MAX_S
LIFT_MAX_S = 120.0
PROBE_S = 60.0  # native's switch is asked again this often while he plays
PROBE_UNKNOWN_S = 5.0
SOURCES = ("console", "chat", "default")
# Orders that clear a hold on the members that carried them out (native erases its own record).
UNHOLDS = frozenset({"follow", "come_back", "stay", "stack", "attack", "kill_skull", "lift"})
MAX_GUID = 0xFFFFFFFF


def _guid(value) -> bool:
    return type(value) is int and 0 < value <= MAX_GUID


def damage_range(cls: int, spec_tab: int) -> str:
    """Where a damage dealer stands: hunters, mages, warlocks, shadow priests, elemental shamans
    and balance druids are ranged; everyone else is melee."""
    if cls in (3, 5, 8, 9):
        return "ranged"
    if cls in (7, 11):
        return "melee" if spec_tab == 1 else "ranged"
    return "melee"


@dataclass(slots=True)
class _Held:
    """A line of his held as a possible order while the model reads it."""
    ev: Chat
    line_id: int
    intent: Unclear
    generation: int
    task: asyncio.Task | None = None
    answer: object = None
    done: bool = False
    released: bool = False
    names: dict = field(default_factory=dict)


class RaidLeadService:
    def __init__(self, app):
        self.app = app
        self.supported = False
        self.connected = False
        self.native: bool | None = None  # Leeroy.RaidLead.Enable as native last answered; None unknown
        self.lead_guid: int | None = None
        self.source: str | None = None
        self.his = False  # he said he leads it himself: no lead, no default
        self.members: frozenset[int] = frozenset()  # his group's cast, him excluded
        self.raid = False
        self.leader = 0
        self.held: dict[int, float] = {}  # member -> when we put them on hold
        self._load()
        self._claimed: dict[int, tuple[object, int]] = {}
        self._queue: deque = deque(maxlen=QUEUE)
        self._pending: deque[_Held] = deque()
        self._generation = 0
        self._wake = asyncio.Event()
        self._dead: set[int] = set()
        self._bosses: dict[int, tuple[int, str]] = {}  # map -> the last boss his group engaged there
        self._last: tuple[str, str, float] | None = None  # (order words, who words, when)
        self._countdown: asyncio.Task | None = None
        self._probe_at = float("-inf")
        self._lift_next = float("-inf")
        self._lift_wait = LIFT_RETRY_S
        self._lines_done: deque[int] = deque(maxlen=64)  # his lines already carried out
        self.stopping = False

    # -- state ---------------------------------------------------------------------------------

    def _load(self):
        try:
            value = json.loads(self.app.store.get_meta(KEY) or "{}")
        except (TypeError, ValueError, RecursionError):
            value = {}
        if not isinstance(value, dict):
            value = {}
        members = value.get("members", [])
        if isinstance(members, list) and all(_guid(g) for g in members) and len(members) <= 40:
            self.members = frozenset(members)
        self.raid = value.get("raid") is True
        self.leader = value.get("leader") if _guid(value.get("leader")) else 0
        lead = value.get("lead")
        if _guid(lead) and lead in self.members and value.get("source") in SOURCES:
            self.lead_guid, self.source = lead, value["source"]
        self.his = value.get("his") is True and self.lead_guid is None
        held = value.get("held", {})
        if isinstance(held, dict) and len(held) <= 40:
            for key, since in held.items():
                if (isinstance(key, str) and key.isascii() and key.isdigit() and _guid(int(key))
                        and type(since) in (int, float)):
                    self.held[int(key)] = float(since)

    def _save(self):
        state = {"lead": self.lead_guid, "source": self.source, "his": self.his,
                 "members": sorted(self.members), "raid": self.raid, "leader": self.leader,
                 "held": {str(g): t for g, t in sorted(self.held.items())}}
        with self.app.store.transaction():
            self.app.store.set_meta(KEY, json.dumps(state, separators=(",", ":")))

    def _set(self, guid: int | None, source: str | None, *, his: bool = False):
        old = (self.lead_guid, self.his)
        self.lead_guid, self.source, self.his = guid, source if guid is not None else None, his
        self._save()
        self._sync_talk()
        if old != (guid, his):
            log_event("raidlead_set", lead=guid or 0, source=source or ("his" if his else "-"))

    def _break(self, why: str):
        """His group broke (or she left it): no lead, nothing queued for the old group. Native lifts
        any hold of a bot no longer in his group itself."""
        if self.lead_guid is not None or self.his or self.held:
            log_event("raidlead_cleared", reason=why)
        self.lead_guid = self.source = None
        self.his = False
        self.held.clear()
        self._generation += 1
        self._queue.clear()
        self._save()
        self._sync_talk()

    def lead(self) -> int | None:
        """The member who calls his group's raid; None while he leads it himself, while native's
        switch is off or unknown, or while the worldserver has no raid lead."""
        if self.his or not (self.connected and self.supported and self.native is True):
            return None
        return self.lead_guid

    # -- when it may act ----------------------------------------------------------------------------

    def blocked(self) -> str | None:
        """None when the raid lead may act now, else why not (a code)."""
        app = self.app
        if self.stopping or app.stopping:
            return "stopping"
        if not self.connected or not self.supported:
            return "unsupported"
        if self.native is not True:
            return "native_off"
        if not app.world.human_online:
            return "offline"
        if self.leader != app.human_guid or not self.members:
            return "no_group"
        guild = getattr(app, "guild", None)
        if guild is None:
            return "no_guild"
        if guild.offline.active:
            return "offline_raid"
        night = guild.raid_night
        if night.state.value == "forming":
            return "forming"
        if night.run is not None and not night.run.lead_is_human:
            return "raid_led"
        return None

    def _sync_talk(self):
        """The talk's raid lead: hers while the group is his to lead. A raid he drives whose lead
        she is goes back to him at once when he takes it back or the raid lead stops (P2-5)."""
        why = self.blocked()
        lead = self.lead() if why in (None, "offline", "stopping") else None
        self.app.raid.set_designated(lead)
        guild = getattr(self.app, "guild", None)
        run = getattr(getattr(guild, "raid_night", None), "run", None)
        human = self.app.human_guid
        if (run is not None and run.lead_is_human and run.plan.lead_guid != human
                and (lead is None or lead not in run.plan.members)):
            self.app.raid.set_lead(human)

    # -- his group -----------------------------------------------------------------------------------

    def _names(self) -> dict[str, int]:
        cast = self.app.store.cast()
        out = {}
        for guid in self.members:
            member = cast.get(guid) or self.app.world.members.get(guid)
            if member is not None and member.name:
                out[member.name.lower()] = guid
        return out

    def _speakable(self, guid) -> bool:
        return (_guid(guid) and guid in self.members and guid != self.app.human_guid
                and guid in self.app.world.online and self.app.personas.get(guid) is not None)

    def _roles(self) -> dict[int, tuple[str, str, int, int]]:
        """Each member of his group online with a persona: (role, damage range, class, race)."""
        cast = self.app.store.cast()
        out = {}
        for guid in sorted(self.members):
            member, persona = cast.get(guid), self.app.personas.get(guid)
            if member is None or persona is None or guid not in self.app.world.online:
                continue
            role = role_of(member.class_, member.spec_tab, persona.play.raid_role)
            out[guid] = (role, damage_range(member.class_, member.spec_tab), member.class_, member.race)
        return out

    def addressees(self, order: Order) -> list[int]:
        roles = self._roles()
        who = order.who if order.who != "everyone" or order.action != "taunt" else "tanks"
        if who == "member":
            guids = [order.member] if order.member in roles else []
        elif who == "everyone":
            guids = sorted(roles)
        elif who in ("tanks", "healers"):
            guids = [g for g, r in roles.items() if r[0] == who[:-1]]
        elif who == "damage":
            guids = [g for g, r in roles.items() if r[0] == "damage"]
        else:
            guids = [g for g, r in roles.items() if r[0] == "damage" and r[1] == who]
        if order.action == "hold":  # tanks and healers never go passive (native refuses them too)
            guids = [g for g in guids if roles[g][0] == "damage"]
        return guids

    def _who_words(self, order: Order) -> str:
        if order.who == "member":
            return self._name(order.member)
        if order.action == "taunt" and order.who == "everyone":
            return C.WHO_WORDS["tanks"]
        if order.action == "hold" and order.who == "everyone":
            return C.WHO_WORDS["damage"]
        return C.WHO_WORDS[order.who]

    def _name(self, guid) -> str:
        if guid == self.app.human_guid:
            return self.app.guild.ports.human_name()
        member = self.app.store.cast().get(guid) or self.app.world.members.get(guid)
        return member.name if member is not None else f"member {guid}"

    def _channel(self) -> str:
        return "raid" if self.raid else "party"

    # -- events ---------------------------------------------------------------------------------

    async def connection(self, connected):
        self.connected = connected
        if not connected:
            self.supported = False
            self.native = None
            self._claimed.clear()
            self._queue.clear()
            self._sync_talk()

    async def event(self, ev):
        if self.app.is_backlog(ev) or self.stopping:
            return
        human = self.app.human_guid
        if isinstance(ev, Hello):
            self.supported = FEATURE in ev.features
            self.native = None
            self._probe_at = float("-inf")  # a new worldserver: ask its switch, and for his group
            self._lift_wait, self._lift_next = LIFT_RETRY_S, float("-inf")
            if self.held:
                self._enqueue(("lift", "hello"))
        elif isinstance(ev, Group):
            await self._group(ev.leader_guid, ev.raid, ev.members)
        elif isinstance(ev, GuildGroup):
            if ev.leader_guid == human:
                await self._group(ev.leader_guid, ev.raid, ev.members)
        elif isinstance(ev, Chat):
            row = self._claimed.pop(id(ev), None)
            if row is not None:
                intent, line_id = row
                if isinstance(intent, Unclear):
                    self._hold_line(ev, line_id, intent)
                else:
                    self._enqueue(("line", intent, line_id, ev))
        elif isinstance(ev, Death):
            if ev.guid in self.members:
                self._dead.add(ev.guid)
        elif isinstance(ev, Resurrect):
            self._dead.discard(ev.guid)
        elif isinstance(ev, Boss):
            self._boss(ev)
        self._sync_talk()

    async def _group(self, leader, raid, members):
        human = self.app.human_guid
        everyone = set(members) | ({leader} if leader else set())
        if (not leader and not members) or human not in everyone:
            self.members, self.leader, self.raid = frozenset(), 0, False
            self._break("group_left")
            await self._release_pending()
            return
        self.members = frozenset(g for g in members if _guid(g) and g != human)
        self.leader, self.raid = leader, bool(raid)
        if self.lead_guid is not None and self.lead_guid not in self.members:
            self._break("lead_left")
            await self._release_pending()
            return
        self._dead &= self.members
        for guid in [g for g in self.held if g not in self.members]:
            del self.held[guid]  # native lifts a hold of a bot that left his group
        self._save()
        self._wake.set()

    def _boss(self, ev):
        mine = set(ev.members) & (self.members | {self.app.human_guid})
        if not mine:
            return
        if ev.state == "engaged":
            self._bosses[ev.map] = (ev.boss_id, boss_name(ev.map, ev.boss_id, ev.boss_name))
            if self.held:
                self._enqueue(("lift", "engage"))

    def _enqueue(self, item):
        self._queue.append((self._generation, item))
        self._wake.set()

    def claims(self, ev: Chat, line_id: int) -> str | bool:
        """Whether his line (already stored) is the raid lead's, not talk: "order" for a
        designation, an order, a ready check, a pull countdown or a question about the plan;
        "held" for an addressed order attempt the model reads first; False for anything else.
        Only his raid or party chat, or a push-to-talk room that is not private (never a whisper,
        Discord or a DM). Synchronous and model-free; any failure is False (talk)."""
        try:
            if (ev.from_guid != self.app.human_guid or ev.via_bridge or self.blocked() is not None
                    or not (ev.channel in COMMAND_CHANNELS
                            or ev.channel.startswith("voice:") and ev.channel not in PRIVATE_ROOMS)):
                return False
            intent = parse(ev.text, self._names())
            if intent is None:
                return False
            self._claimed[id(ev)] = (intent, line_id)
            while len(self._claimed) > CLAIMED:
                del self._claimed[next(iter(self._claimed))]
            return "held" if isinstance(intent, Unclear) else "order"
        except Exception as exc:
            log_event("raidlead_claim_failed", level=logging.WARNING, error=type(exc).__name__)
            return False

    # -- held lines: the model reads them beside the worker, and they go to talk in order --------

    def _hold_line(self, ev, line_id, intent):
        held = _Held(ev, line_id, intent, self._generation, names=self._names())
        held.task = asyncio.get_running_loop().create_task(self._read(held), name="raidlead-read")
        self._pending.append(held)

    async def _read(self, held: _Held):
        try:
            held.answer = await self._classify(held.ev.text, held.names)
        finally:
            held.done = True
            self._wake.set()

    async def before_his_line(self):
        """His next line is about to be handled: a line still held for the model goes to talk
        first, so his lines reach talk in the order he wrote them (P3-3). Any failure here leaves
        his new line to go on as usual."""
        try:
            if any(not h.done for h in self._pending):
                await self._release_pending(unread_only=True)
        except Exception as exc:
            log_event("raidlead_release_failed", level=logging.WARNING, error=type(exc).__name__)

    async def _release_pending(self, *, unread_only=False):
        keep = deque()
        while self._pending:
            held = self._pending.popleft()
            if unread_only and held.done:
                keep.append(held)
                continue
            if held.task is not None and not held.task.done():
                held.task.cancel()
            if not held.released:
                held.released = True
                log_event("raidlead_unclear", reason="talk_first")
                await self.app.release_to_talk(held.ev, held.line_id)
        self._pending = keep

    async def _settle_pending(self):
        while self._pending and self._pending[0].done:
            held = self._pending.popleft()
            if held.released:
                continue
            held.released = True
            if held.generation != self._generation or self.blocked() is not None:
                await self.app.release_to_talk(held.ev, held.line_id)
                continue
            await self._unclear(held)

    # -- the worker -------------------------------------------------------------------------------

    async def run(self):
        while True:
            try:
                await asyncio.wait_for(self._wake.wait(), 1.0)
            except TimeoutError:
                pass
            self._wake.clear()
            try:
                await self._tick()
                await self._settle_pending()
                while self._queue:
                    generation, item = self._queue.popleft()
                    await self._handle(generation, item)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log_event("raidlead_failed", level=logging.WARNING, error=type(exc).__name__)

    async def _tick(self):
        await self._probe()
        self._sync_talk()
        await self._lift_due()
        if self.blocked() is not None:
            return
        if self.lead_guid is None and not self.his:
            officers, titled = [], []
            for guid in self.members:
                persona = self.app.personas.get(guid)
                if (persona is not None and persona.play.guild_role == "officer"
                        and guid in self.app.world.online):
                    officers.append(guid)
                    if is_raid_lead(persona):
                        titled.append(guid)
            pick = default_lead(self.app.store.conn, officers, titled)
            if pick is not None:
                self._set(pick, "default")

    async def _probe(self):
        """Native's switch, asked (P2-4): at each hello a sync (which also has native publish his
        group, P3-10), then a probe a minute while he plays. Off, nothing here leads or speaks."""
        if not (self.connected and self.supported and self.app.world.human_online):
            return
        now = self.app.clock.now()
        if now - self._probe_at < (PROBE_UNKNOWN_S if self.native is None else PROBE_S):
            return
        self._probe_at = now
        action = "probe" if self.native is True else "sync"
        ok, error = await self._native(RaidLeadCmd(uuid4().hex, action, 0))
        self._native_answer(ok, error)

    def _native_answer(self, ok, error):
        before = self.native
        if ok:
            self.native = True
        elif error == "raidlead_off":
            self.native = False
        if self.native != before and self.native is not None:
            log_event("raidlead_native", on=int(self.native))
            self._sync_talk()

    async def _lift_due(self):
        """Holds older than HOLD_S, and lifts a member skipped, tried again: after 5 s, doubling up
        to 2 min, never while the bridge is down (native lifts them itself as well)."""
        if not (self.connected and self.supported):
            return
        now = self.app.clock.now()
        due = sorted(g for g, since in self.held.items() if now - since >= HOLD_S)
        if due and now >= self._lift_next:
            await self._lift(due, "timer")

    async def _lift(self, guids, why):
        if not guids or not (self.connected and self.supported):
            return  # the bridge is down: the next hello lifts them
        result = await self._job(BotDo(uuid4().hex, tuple(guids), "order", "lift"), quiet=True)
        now = self.app.clock.now()
        if result is None:
            self._back_off(now)
            return
        done, missed = result
        for guid in done:
            self.held.pop(guid, None)
        for guid, reason in missed:
            if reason in ("not_in_group", "offline", "raidlead_off", "not_cast", "not_bot"):
                self.held.pop(guid, None)  # native keeps its own record and lifts it when it can
                log_event("raidlead_lift_dropped", guid=guid, reason=_code(reason))
        if any(g in self.held for g in guids):
            self._back_off(now)
        else:
            self._lift_wait, self._lift_next = LIFT_RETRY_S, float("-inf")
        self._save()
        log_event("raidlead_lift", why=why, count=len(guids), done=len(done))

    def _back_off(self, now):
        self._lift_next = now + self._lift_wait
        self._lift_wait = min(self._lift_wait * 2, LIFT_MAX_S)

    async def _handle(self, generation, item):
        kind = item[0]
        if kind == "lift":
            await self._lift(sorted(self.held), item[1])
            return
        if generation != self._generation or self.blocked() is not None:
            return  # queued for a group that broke, or the raid lead stopped (P3-2)
        if kind == "line":
            _, intent, line_id, ev = item
            if line_id in self._lines_done:
                return  # one line of his is carried out once
            self._lines_done.append(line_id)
            await self._line(intent, line_id, ev)

    async def _line(self, intent, line_id, ev):
        human = self.app.guild.ports.human_name()
        if isinstance(intent, Designate):
            if intent.guid is None:
                self._set(None, None, his=True)
                return
            if not self._speakable(intent.guid):
                return
            self._set(intent.guid, "chat")
            self._propose(intent.guid, C.designated(human), line_id)
        elif isinstance(intent, Order):
            await self._order(intent, line_id)
        elif isinstance(intent, Ready):
            await self._ready(intent, line_id)
        elif isinstance(intent, Countdown):
            self._start_countdown(intent)
        elif isinstance(intent, AskPlan):
            self._plan(intent, line_id)

    # -- orders --------------------------------------------------------------------------------------

    async def _order(self, order: Order, line_id):
        human_name = self.app.guild.ports.human_name()
        targets = self.addressees(order)
        speaker = self._ack_speaker(order)
        if order.action == "lust":
            await self._lust(order, targets, speaker, line_id)
            return
        if not targets:
            await self._refuse(speaker, self._nobody(order),
                               name=self._name(order.member) if order.who == "member" else "")
            return
        if order.action == "kill_skull":
            marker = self.lead() if self._speakable(self.lead()) else self.app.human_guid
            ok, error = await self._native(RaidLeadCmd(uuid4().hex, "mark", marker, "skull"))
            if not ok:
                self._native_answer(ok, error)
                await self._refuse(speaker, error)
                return
        on = ""
        if order.action == "stack":
            on = self._name(order.on) if order.on is not None else human_name
        result = await self._job(BotDo(uuid4().hex, tuple(targets), "order", order.action,
                                       order.on or 0))
        if result is None:
            return
        done, missed = result
        if order.action == "release":
            # playerbots reads his typed "release ..." as its own command too: one already released
            # did what he asked, so there is no second "already released" line
            done = [*done, *(g for g, r in missed if r == "released")]
            missed = [(g, r) for g, r in missed if r != "released"]
        if order.action == "hold":
            now = self.app.clock.now()
            for guid in done:
                self.held[guid] = now
            self._save()
        elif order.action in UNHOLDS and set(done) & set(self.held):
            for guid in done:
                self.held.pop(guid, None)
            self._save()
        if not done:
            await self._refuse(speaker, missed[0][1] if missed else "failed")
            return
        words = C.order_words(order.action, on=on)
        self._last = (words, self._who_words(order), self.app.clock.now())
        speaker = self._ack_speaker(order, done)
        self._propose(speaker, C.ack(human_name, words, self._who_words(order),
                                     [self._name(g) for g in done],
                                     [(self._name(g), C.reason_words(r)) for g, r in missed]),
                      line_id)

    def _nobody(self, order: Order) -> str:
        if order.who == "member" and order.member not in self._roles():
            return "not_here"
        if order.action == "hold":
            return "not_damage"
        return "nobody"

    async def _lust(self, order, targets, speaker, line_id):
        """One shaman casts her faction's spell through the raid lead's own order (so native's
        switch covers it). An addressee he named who is not here is refused, never replaced (P3-1)."""
        roles = self._roles()
        if order.who == "member" and order.member not in roles:
            await self._refuse(speaker, "not_here", name=self._name(order.member))
            return
        pool = targets if order.who != "everyone" else sorted(roles)
        shamans = [g for g in pool if g in roles and roles[g][2] == 7 and g not in self._dead]
        if not shamans:
            await self._refuse(speaker, "no_shaman")
            return
        shaman = shamans[0]
        result = await self._job(BotDo(uuid4().hex, (shaman,), "order", "lust"))
        if result is None:
            return
        done, missed = result
        if not done:
            await self._refuse(speaker, missed[0][1] if missed else "failed")
            return
        faction = faction_known(roles[shaman][3])
        spell = "Heroism" if faction == "alliance" else "Bloodlust"
        words = C.order_words("lust", spell=spell)
        self._last = (words, self._name(shaman), self.app.clock.now())
        self._propose(self._ack_speaker(order, done), C.ack(
            self.app.guild.ports.human_name(), words, self._name(shaman), [self._name(shaman)], []),
            line_id)

    def _in_fight(self) -> bool:
        return self.app.raid.phase(self.app.clock.now()) in (RaidPhase.COMBAT, RaidPhase.WIPE_SILENCE)

    def _ack_speaker(self, order: Order, done=()) -> int | None:
        """The member he named answers for herself, out of a fight; else the lead; else whoever
        carried it out."""
        if (order.who == "member" and self._speakable(order.member)
                and (not done or order.member in done) and not self._in_fight()):
            return order.member
        if self._speakable(self.lead()):
            return self.lead()
        return next((g for g in done if self._speakable(g)), None)

    async def _unclear(self, held: _Held):
        """An order attempt no pattern resolved, once the model has read it. It maps it onto the
        list when it is sure; when it is not, she asks; when the model is not loaded (or paused),
        cannot answer, or is sure it is no order, the line goes back to talk as if never held."""
        intent, answer = held.intent, held.answer
        speaker = self._ack_speaker(Order("follow", intent.who, intent.member))
        if answer is None or answer[0] is None and answer[1] >= CONFIDENT:
            log_event("raidlead_unclear", reason="talk" if answer else "no_model")
            await self.app.release_to_talk(held.ev, held.line_id)
            return
        order, confidence = answer
        if order is None or confidence < CONFIDENT:
            log_event("raidlead_unclear", reason="confidence")
            if speaker is not None:
                self.app.signal_his_line(held.ev, held.line_id, command=True)
                self._propose(speaker, C.clarify(self.app.guild.ports.human_name()), held.line_id)
            else:
                await self.app.release_to_talk(held.ev, held.line_id)
            return
        if intent.who != "everyone" and order.who == "everyone":
            order = Order(order.action, intent.who, intent.member, order.on, by_model=True)
        log_event("raidlead_model_order", action=order.action, who=order.who)
        self.app.signal_his_line(held.ev, held.line_id, command=True)
        await self._order(order, held.line_id)

    async def _classify(self, text: str, names: dict[str, int]):
        """The live model's reading of an order attempt, restricted to ORDERS by its schema; None
        when the model is not already loaded, the GPU is paused or the answer is not valid."""
        if not await self._model_ready():
            return None
        model = self.app.cfg.models.live
        messages = [{"role": "system", "content": SYSTEM},
                    {"role": "user", "content": normalize(text)}]
        fmt = schema(names)

        async def job(llm):
            return await llm.chat(model, messages, options={"temperature": 0.0, "num_predict": 80,
                                                             "num_ctx": 4096},
                                  think=False, format=fmt)
        try:
            result = await self.app.scheduler.submit(Priority.REPLY_HUMAN, job,
                                                     deadline=self.app.clock.now() + MODEL_S)
        except GpuPaused:
            return None
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log_event("raidlead_model_failed", level=logging.WARNING, error=type(exc).__name__)
            return None
        return read_answer(result.text, names)

    async def _model_ready(self) -> bool:
        from leeroy_mind.nightly.gpu import norm
        arbiter = getattr(self.app, "arbiter", None)
        if arbiter is None or getattr(arbiter, "paused", False):
            return False
        try:
            loaded = {norm(n) for n in await asyncio.wait_for(arbiter.admin.loaded_models(), 2.0)}
        except Exception:  # Ollama away or slow: it cannot be known to be resident
            return False
        return norm(self.app.cfg.models.live) in loaded

    # -- callouts ----------------------------------------------------------------------------------

    async def _ready(self, intent: Ready, line_id):
        """His ready check: native's raid_control with him as its leader (no assistant flag)."""
        human = self.app.human_guid
        speaker = intent.member if self._speakable(intent.member) else (
            self.lead() if self._speakable(self.lead()) else None)
        ok, error = await self._native(RaidControl(uuid4().hex, human, "ready_check", human))
        if not ok:
            await self._refuse(speaker, error)
            return
        if speaker is not None:
            self._propose(speaker, C.ready(self.app.guild.ports.human_name()), line_id,
                          kind=TurnKind.CALLOUT)

    def _start_countdown(self, intent: Countdown):
        speaker = intent.member if self._speakable(intent.member) else (
            self.lead() if self._speakable(self.lead()) else None)
        if speaker is None:
            return
        if self._countdown is not None and not self._countdown.done():
            self._countdown.cancel()
        self._countdown = asyncio.get_running_loop().create_task(
            self._count(speaker, intent.seconds), name="raidlead-countdown")

    async def _count(self, speaker, seconds):
        ports, channel = self.app.guild.ports, self._channel()
        if not await ports.say(speaker, channel, C.COUNTDOWN.format(seconds=seconds), kind=TurnKind.CALLOUT):
            return
        await self.app.clock.sleep(seconds)
        if self.blocked() is None:
            await ports.say(speaker, channel, C.GO, kind=TurnKind.CALLOUT)

    def _plan(self, intent: AskPlan, line_id):
        speaker = intent.member if self._speakable(intent.member) else (
            self.lead() if self._speakable(self.lead()) else None)
        if speaker is None:
            return
        human = self.app.guild.ports.human_name()
        roles = self._roles()
        count = {r: sum(1 for v in roles.values() if v[0] == r) for r in ("tank", "healer", "damage")}
        facts = []
        if self.lead() is not None:
            facts.append(f"{self._name(self.lead())} calls the raid; {human} keeps the group.")
        facts.append(f"The group: {count['tank']} tanks, {count['healer']} healers, "
                     f"{count['damage']} damage dealers.")
        if self._last is not None and self.app.clock.now() - self._last[2] < 600:
            facts.append(f"The last order: {self._last[0]}, for {self._last[1]}.")
        his = self.app.world.members.get(self.app.human_guid)
        boss = self._bosses.get(his.map) if his is not None else None
        if boss is not None:
            plan = C.boss_plan(C.BossContext(his.map, boss[0], boss[1]))
            facts.append(f"The boss here is {boss[1]}: {plan}." if plan
                         else f"The boss here is {boss[1]}; there is no plan on file for it.")
        else:
            facts.append("No boss plan on file for here yet.")
        self._propose(speaker, C.plan(human, facts), line_id)

    # -- speech and the bridge --------------------------------------------------------------------

    def _propose(self, guid, instruction, line_id, *, kind=None):
        """A model-written line in her voice around code-built facts, through guild life's speech
        lane (its GPU lease and the talk pipeline's guards, raid rules and typing)."""
        if not self._speakable(guid):
            return False
        if kind is None:
            kind = (TurnKind.URGENT if self.app.raid.phase(self.app.clock.now()) is RaidPhase.WIPE_SILENCE
                    else TurnKind.REPLY_HUMAN)
        reply = kind is TurnKind.REPLY_HUMAN
        turn = Turn(kind=kind, speaker=guid, channel=self._channel(), instruction=instruction,
                    addressee=self.app.human_guid if reply else None,
                    priority=Priority.REPLY_HUMAN if reply else Priority.RAID_LEAD,
                    deadline=self.app.clock.now() + SPEECH_S, max_lines=1,
                    trigger_line=line_id if reply else None)
        self.app.guild.source.add(turn)
        return True

    async def _refuse(self, guid, code, *, name=""):
        """A plain "can't do that one": no model, so nothing can turn it into an "on it"."""
        code = code or "failed"
        log_event("raidlead_refused", reason=_code(code))
        if code in ("raidlead_off", "disabled", "unsupported") or not self._speakable(guid):
            return  # native's switch is off: the operator's to fix, nothing for him to hear
        kind = TurnKind.URGENT if self._in_fight() else TurnKind.CALLOUT
        await self.app.guild.ports.say(guid, self._channel(), C.refusal(code, name), kind=kind)

    async def _native(self, cmd) -> tuple[bool, str]:
        """A command native answers in its ack alone (raid_lead, raid_control)."""
        try:
            ack = await asyncio.wait_for(self.app.bridge.send(cmd, preserve_id=True), JOB_S)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return False, type(exc).__name__
        return (True, "") if ack.ok else (False, ack.error or "refused")

    async def _job(self, cmd: BotDo, *, quiet=False):
        """A bot_do and its terminal: (done guids, [(guid, reason)]) once the game's job confirms;
        a refusal at the ack is ([], [(each guid, error)]); None when nothing came back, so
        nothing is said either way."""
        ports = self.app.guild.ports
        try:
            ack = await asyncio.wait_for(ports.send(cmd), JOB_S)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await ports.wait_job(cmd.id, 0)
            log_event("raidlead_order", action=cmd.param, count=len(cmd.guids), outcome="transport",
                      error=type(exc).__name__)
            return None
        if not ack.ok:
            await ports.wait_job(cmd.id, 0)
            self._native_answer(False, ack.error)
            log_event("raidlead_order", action=cmd.param, count=len(cmd.guids), outcome="refused",
                      error=_code(ack.error))
            return [], [(g, ack.error or "failed") for g in cmd.guids]
        job = await ports.wait_job(cmd.id, JOB_S)
        if job is None or job.id != cmd.id or job.kind != "bot_do":
            log_event("raidlead_order", action=cmd.param, count=len(cmd.guids), outcome="no_job")
            return None
        if job.state != "done":
            return [], [(g, job.reason or "failed") for g in cmd.guids]
        wanted = set(cmd.guids)
        done = [g for g in job.guids if g in wanted]
        missed = [(s.guid, s.reason) for s in job.skipped if s.guid in wanted]
        if not quiet or missed:
            log_event("raidlead_order", action=cmd.param, count=len(cmd.guids), done=len(done),
                      outcome="done")
        return done, missed

    # -- the console ---------------------------------------------------------------------------------

    def console_status(self) -> dict:
        cast = self.app.store.cast()
        candidates = []
        for guid in sorted(self.members):
            persona = self.app.personas.get(guid)
            if persona is None or guid not in cast:
                continue
            candidates.append({"guid": guid, "name": cast[guid].name,
                               "officer": persona.play.guild_role == "officer",
                               "online": guid in self.app.world.online})
        return {"lead": self.lead_guid, "source": self.source, "his": self.his,
                "native": self.native, "raid": self.raid, "blocked": self.blocked(),
                "candidates": candidates}

    async def console_action(self, args: dict) -> dict:
        why = self.blocked()
        if why is not None:
            return {"ok": False, "error": BLOCKED.get(why, why)}
        if args["action"] == "clear":
            self._set(None, None)
            return {"ok": True, "message": "The guild's pick leads your group now."}
        guid = args["guid"]
        if guid == self.app.human_guid:
            self._set(None, None, his=True)
            return {"ok": True, "message": "You lead: nobody calls it for you."}
        if not self._speakable(guid):
            return {"ok": False, "error": "Only a member of your group who is online can lead it."}
        self._set(guid, "console")
        self._propose(guid, C.designated(self.app.guild.ports.human_name()), None,
                      kind=TurnKind.CALLOUT)
        return {"ok": True, "message": f"{self._name(guid)} leads your group's raid now."}

    async def close(self):
        self.stopping = True
        for held in self._pending:
            if held.task is not None and not held.task.done():
                held.task.cancel()
        tasks = [h.task for h in self._pending if h.task is not None]
        if self._countdown is not None and not self._countdown.done():
            self._countdown.cancel()
            tasks.append(self._countdown)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


BLOCKED = {
    "stopping": "The mind is stopping.",
    "unsupported": "The game server has no raid lead yet (it needs the native rebuild) or is not connected.",
    "native_off": "The game server's raid lead is off (Leeroy.RaidLead.Enable).",
    "offline": "You're not in game.",
    "no_group": "You're not leading a group.",
    "no_guild": "Guild life is not running in this mind.",
    "offline_raid": "An offline raid holds the guild right now.",
    "forming": "A raid is being formed.",
    "raid_led": "Tonight's raid has its own lead under dungeon-clear.",
}


def _code(value) -> str:
    text = "".join(c for c in str(value or "") if c.isascii() and (c.isalnum() or c in "_:.-"))
    return text[:64] or "-"


def attach(app):
    """The raid lead, or None while [raidlead] is off (then nothing is registered and nothing
    changes)."""
    if not load(app.data_dir / "mind.toml").enabled or getattr(app, "guild", None) is None:
        return None
    register_guild()
    register()
    service = RaidLeadService(app)
    for kind in ("hello", "group", "guild_group", "chat", "death", "resurrect", "boss"):
        app.register_event_handler(kind, service.event)
    app.register_connection_hook(service.connection)
    app.register_background(service.run)
    app.register_shutdown_drain(service.close)
    return service
