"""His invitations in game, carried out: "come to the inn", "meet me at the bank", "let's go
fishing in Dalaran". Read from his whisper, party, raid, guild, officer and say lines (parse.py;
the live model only classes a line the patterns could not settle, into a fixed list), decided per
member (policy.py), carried out by native string_do marked "via": "invite" (come_to_me, go_to,
sit, fish, follow, join_group: Guild/Strings.cpp), and answered by talk: the members it asked
answer it (talk.engine.on_human_chat asks reply_turns), each turn waits for what her body did
(TurnMeta.ready) and its instruction tells exactly that (words.py). What changes after her answer
was written gets one short follow-up line. His Discord DM never moves a body: her answer there
only agrees to meet up later.

[talk] invites (default true; anything but true is off) is read at each line. Logs carry line
ids, guids, kinds and codes only, never his words."""
import asyncio
import json
import logging
import re
import uuid
from collections import OrderedDict, deque
from dataclasses import dataclass, field

from leeroy_mind.bridge import BridgeError, NotConnected
from leeroy_mind.guild.ports import JobWaiter
from leeroy_mind.logs import log as log_event
from leeroy_mind.talk.select import ReplyTurn
from leeroy_mind.talk.turns import Turn, TurnKind
from leeroy_mind.wow import DUNGEONS, RAID_INSTANCES, RAID_MAPS

from . import parse as P
from . import policy
from . import spots as S
from . import words as W

log = logging.getLogger(__name__)

GAME_CHANNELS = frozenset({"whisper", "party", "raid", "raid_warning", "guild", "officer", "say"})
FEATURE = "invites"  # the hello feature: go_to, fish and the "via" mark (LeeroyProtocol.h)
# Instances and battlegrounds: nobody is moved into, out of or inside one (native refuses too).
BG_MAPS = frozenset({30, 489, 529, 559, 562, 566, 572, 607, 617, 618, 628})
INSTANCE_MAPS = frozenset(DUNGEONS) | frozenset(RAID_INSTANCES) | RAID_MAPS | BG_MAPS
# His raid's quiet: no invitation in its fight, its wipe silence or its debrief.
RAID_QUIET = frozenset({"combat", "wipe_silence", "wipe_debrief"})
MOVES = frozenset({"come_to_me", "go_to", "join_group"})  # a body put somewhere else
JOB_WAIT_S = 2.0   # native publishes the done job before its ack; this only covers reordering
ARRIVE_S = 4.0     # after a move, before what she does there (a teleport takes a moment)
STEP_TRIES = 4     # what she does there, tried again while she is still landing
LANDING = frozenset({"travelling", "too_far", "not_online"})
MODEL_S = 8.0
# Native's switch at 0 (or an older worldserver without go_to and fish): his lines are plain talk
# again until the next hello or this long, so one refusal is said, not every invitation after it.
NATIVE_OFF_S = 600.0
NATIVE_OFF = frozenset({"invites_off", "unsupported"})
SURE = 0.7         # the model's reading below this is talk
KEPT = 64          # invitations kept, newest first out
_CODE = re.compile(r"[a-z_]{1,40}(:[a-z_0-9]{1,40})?")
SYSTEM = (
    "You read one line a player typed in World of Warcraft chat to the members of his guild. Say "
    "whether it invites them to do something with him in the game right now, and what: come (come "
    "to where he is), meet (meet him at a place he names), fish (go fishing together), sit (sit or "
    "have a drink together), follow (come along with him), join (join his group), later (an "
    "invitation for another time), none (anything else: talk about himself, a question about "
    "them, his own plans, a joke). Give how sure you are, 0 to 1. Answer as JSON.")
SCHEMA = {"type": "object",
          "properties": {"what": {"type": "string", "enum": ["none", "later", *P.WHAT]},
                         "sure": {"type": "number", "minimum": 0, "maximum": 1}},
          "required": ["what", "sure"]}


def native_code(error: str) -> str:
    """Native's refusal as one code: its own policy codes (combat, in_instance, busy:string), an
    older worldserver's unknown kind or field as `unsupported`, the switch at 0 as `invites_off`;
    anything else `refused` (never its text)."""
    error = (error or "").strip()
    if error.startswith(("unknown type", "bad field")):
        return "unsupported"
    if error == "off":
        return "invites_off"
    return error if _CODE.fullmatch(error) else "refused"


def _done(job) -> str:
    reason = getattr(job, "reason", "") or ""
    return reason if _CODE.fullmatch(reason) else "done"


@dataclass(eq=False)
class Member:
    guid: int
    accept: bool
    reason: str = ""                  # why she said no ("" for yes)
    steps: list = field(default_factory=list)   # ("come_to_me",), ("go_to", spot, slot), ...
    note: str = ""                    # with_him, in_group, no_fish
    answers: bool = True              # she answers his line
    outcomes: list = field(default_factory=list)  # (step, "done"|"refused"|..., code)
    failed: str = ""                  # the code that stopped her steps
    done: bool = False
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    told: int | None = None           # steps done when her answer was written (None: not yet)
    moved_at: float | None = None     # her move's place in the hour's count (given back if refused)


@dataclass(eq=False)
class Invite:
    line: int
    ask: P.Ask
    channel: str
    members: dict[int, Member]


@dataclass(eq=False)
class _Held:
    ev: object
    line_id: int
    task: asyncio.Task | None = None
    ask: P.Ask | None = None
    done: bool = False
    released: bool = False


class Invites:
    def __init__(self, app):
        self.app = app
        self.jobs = JobWaiter(capacity=64)
        self._invites: OrderedDict[int, Invite] = OrderedDict()
        self._moves: list[float] = []          # when members were moved, the last hour
        self._pending: dict[int, float] = {}   # member -> until (one pending invitation each)
        self._held: deque[_Held] = deque()
        self._tasks: set[asyncio.Task] = set()
        self._closed = False
        self._off_until = 0.0  # native refused as off: plain talk until then, or the next hello

    # -- switches and facts ---------------------------------------------------------------------

    def enabled(self) -> bool:
        return getattr(getattr(self.app.cfg, "talk", None), "invites", False) is True

    def _feature(self) -> bool:
        hello = getattr(self.app, "hello", None)
        return hello is not None and FEATURE in getattr(hello, "features", ())

    def _quiet(self) -> bool:
        raid = getattr(self.app, "raid", None)
        try:
            return raid is not None and str(raid.phase(self.app.clock.now())) in RAID_QUIET
        except Exception:
            return True

    def _forms(self) -> tuple[str, str, str]:
        human = getattr(self.app.engine, "human", None)
        forms = getattr(human, "forms", None)
        return tuple(forms) if forms else ("he", "him", "his")

    def _names(self) -> dict[int, str]:
        """The cast's names, online or not: naming a member who is away still names her (and
        nobody else goes in her place)."""
        world, hg = self.app.world, self.app.human_guid
        return {g: m.name for g, m in world.members.items() if g != hg and m.name}

    # -- his line -------------------------------------------------------------------------------

    def claims(self, ev, line_id: int) -> str | None:
        """His line (stored) as an invitation: "invite" when it is one and the members it asks
        are decided and their bodies sent (talk then plans their answers), "held" while the model
        reads a line the patterns could not settle, None for plain talk. Synchronous; any failure
        is None (talk)."""
        try:
            app = self.app
            if (self._closed or not self.enabled() or ev.from_guid != app.human_guid
                    or getattr(ev, "via_bridge", False) or ev.channel not in GAME_CHANNELS
                    or not app.world.human_online or self._quiet()
                    or app.clock.now() < self._off_until):
                return None
            ask = P.parse(ev.text, channel=ev.channel, names=self._names(),
                          his_zone=ev.zone or app.world.human_zone)
            if ask is None:
                return None
            if isinstance(ask, P.Maybe):
                if not self._may_read():
                    return None
                held = _Held(ev, line_id)
                held.task = asyncio.get_running_loop().create_task(self._read(held),
                                                                    name="invites-read")
                self._held.append(held)
                return "held"
            return "invite" if self._open(ev, line_id, ask) else None
        except Exception as exc:
            log_event("invite_failed", level=logging.WARNING, error=type(exc).__name__)
            return None

    def _audience(self, ev) -> set[int]:
        world = self.app.world
        if ev.channel == "whisper":
            return {ev.to_guid}
        try:
            heard = set(world.audience("raid" if ev.channel == "raid_warning" else ev.channel,
                                       from_guid=ev.from_guid, to_guid=ev.to_guid, zone=ev.zone))
        except Exception:
            heard = set()
        return {g for g in heard if g in world.online and g != self.app.human_guid}

    def _facts(self, guid: int, ev, now: float) -> policy.Facts:
        app, world = self.app, self.app.world
        member = world.members.get(guid)
        blocked = ""
        if guid not in world.online or member is None:
            blocked = "not_online"
        elif guid in world.afk:
            blocked = "afk"
        elif member.map in INSTANCE_MAPS:
            blocked = "in_instance"
        elif ev.map in INSTANCE_MAPS:
            blocked = "his_instance"
        elif self._pending.get(guid, 0.0) > now:
            blocked = "pending"
        persona = app.personas.get(guid)
        stance = getattr(getattr(persona, "romance", None), "stance", "none") or "none"
        try:
            affinity = app.store.get_relationship(guid, app.human_guid, lens=True).affinity
        except Exception:
            affinity = 0.0
        life = app.engine.life
        attention, away = None, False
        if life.availability > 0:
            lp = life.presence(guid, medium="game", in_his_group=world.in_group(guid))
            attention = int(lp.attention) if lp is not None else None
            away = life.is_away(guid)
        return policy.Facts(affinity=affinity, stance=stance, attention=attention, away=away,
                            blocked=blocked)

    def _open(self, ev, line_id: int, ask: P.Ask) -> bool:
        """Decide who goes along and send their bodies; False when nobody it asks can answer
        (his line is then plain talk)."""
        app, world, now = self.app, self.app.world, self.app.clock.now()
        heard = self._audience(ev)
        if ask.who == "you":
            asked = [ev.to_guid] if ev.to_guid in heard else []
        elif ask.who == "named":
            asked = [g for g in ask.named if g in heard or (ev.channel == "guild" and g in world.online)]
        elif ask.who == "group":
            group = world.group
            asked = sorted(g for g in heard if group is not None and g in group.members)
        else:
            asked = sorted(heard)
        asked = [g for g in asked if app.personas.get(g) is not None]
        if not asked:
            return False
        facts = {g: self._facts(g, ev, now) for g in asked}
        members: dict[int, Member] = {}
        if ask.who in ("you", "named"):
            for g in asked[:policy.MAX_MEMBERS]:
                yes, why = policy.decide(facts[g], line=line_id, guid=g)
                members[g] = Member(g, yes, why)
        else:
            # an open invitation: the most willing first, one or two of them going along
            want = policy.MAX_MEMBERS if ask.who == "group" else policy.how_many(line_id)
            ranked = sorted(asked, key=lambda g: (-policy.willingness(facts[g]), g))
            for g in ranked[:6]:
                if facts[g].blocked in ("not_online", "afk"):
                    continue
                yes, why = policy.decide(facts[g], line=line_id, guid=g)
                if yes:
                    members[g] = Member(g, True)
                    if len(members) >= want:
                        break
            if not members:  # nobody goes: the most willing says why
                first = next((g for g in ranked if facts[g].blocked not in ("not_online", "afk")), None)
                if first is None:
                    return False
                members[first] = Member(first, False, policy.reason(facts[first]))
        self._plan(ev, ask, members, now)
        inv = Invite(line_id, ask, "raid" if ev.channel == "raid_warning" else ev.channel, members)
        self._invites[line_id] = inv
        while len(self._invites) > KEPT:
            self._invites.popitem(last=False)
        log_event("invite_read", line=line_id, what=ask.what, who=ask.who,
                  where=getattr(ask.place, "name", "unknown" if ask.place is S.UNKNOWN else "him"),
                  later=int(ask.later), members=len(members))
        for m in members.values():
            log_event("invite_member", line=line_id, guid=m.guid, accept=int(m.accept),
                      reason=m.reason or "-", steps=",".join(s[0] for s in m.steps) or "-")
            if not (m.accept and m.steps):
                m.done = True
                m.ready.set()
        if any(m.accept and m.steps for m in members.values()):
            self._spawn(self._carry(inv))
        return True

    def _plan(self, ev, ask: P.Ask, members: dict[int, Member], now: float) -> None:
        """Each member's steps, the hour's moves and her pending mark; a member the moves or the
        place cannot take says no."""
        world, feature = self.app.world, self._feature()
        his_zone = ev.zone or world.human_zone
        slots: dict[str, int] = {}
        for m in members.values():
            if not m.accept or ask.later:
                continue
            if ask.place is S.UNKNOWN:
                m.accept, m.reason = False, "unknown_place"
                continue
            steps, note = _steps(ask, in_group=world.in_group(m.guid), feature=feature,
                                 his_zone=his_zone)
            if steps is None:
                m.accept, m.reason = False, note
                continue
            for i, step in enumerate(steps):
                if step[0] == "go_to":
                    slot = slots.get(step[1], 0)
                    slots[step[1]] = slot + 1
                    steps[i] = ("go_to", step[1], slot % S.SLOTS)
            if any(s[0] in MOVES for s in steps):
                self._moves = [t for t in self._moves if now - t < policy.HOUR_S]
                if policy.moves_left(self._moves, now) <= 0:
                    m.accept, m.reason = False, "cap"
                    continue
                self._moves.append(now)
                m.moved_at = now
            m.steps, m.note = steps, note
            if steps:
                self._pending[m.guid] = now + policy.PENDING_S

    # -- carrying it out ------------------------------------------------------------------------

    async def _carry(self, inv: Invite) -> None:
        await asyncio.gather(*(self._run(inv, m) for m in inv.members.values()
                               if m.accept and m.steps and not m.done),
                             return_exceptions=True)

    async def _run(self, inv: Invite, m: Member) -> None:
        try:
            for i, step in enumerate(m.steps):
                if i:
                    await self.app.clock.sleep(ARRIVE_S)
                for attempt in range(STEP_TRIES):
                    outcome, code = await self._send(m.guid, step)
                    if not (i and outcome == "refused" and code in LANDING) or attempt + 1 == STEP_TRIES:
                        break
                    await self.app.clock.sleep(ARRIVE_S)
                m.outcomes.append((step[0], outcome, code))
                log_event("invite_body", line=inv.line, guid=m.guid, kind=step[0], outcome=outcome,
                          code=code or "-")
                if outcome != "done":
                    m.failed = code or outcome
                    if (step[0] in MOVES and outcome in ("refused", "no_link")
                            and m.moved_at is not None and m.moved_at in self._moves):
                        self._moves.remove(m.moved_at)  # nobody moved: the hour's count is given back
                    if code in NATIVE_OFF and self._off_until <= self.app.clock.now():
                        self._off_until = self.app.clock.now() + NATIVE_OFF_S
                        log_event("invites_native_off", code=code)
                    break
        except asyncio.CancelledError:
            m.failed = m.failed or "unknown"
            raise
        except Exception as exc:
            m.failed = m.failed or "unknown"
            log_event("invite_failed", level=logging.WARNING, error=type(exc).__name__)
        finally:
            m.done = True
            m.ready.set()
            self._pending.pop(m.guid, None)
        if not self._closed:
            self._follow_up(inv, m)

    async def _send(self, guid: int, step: tuple) -> tuple[str, str]:
        """One string_do: ("done", what she did), ("refused", code), ("no_link", "") or
        ("unknown", "") when the ack was lost and no done job came."""
        from leeroy_mind.guild.protocol import StringDo
        app = self.app
        bridge = getattr(app, "bridge", None)
        if bridge is None or not getattr(bridge, "connected", False):
            return "no_link", "no_link"
        kind = step[0]
        wire = uuid.uuid4().hex
        self.jobs.expect(wire)
        cmd = StringDo(id=wire, guid=guid, kind=kind, target_guid=app.human_guid or 0,
                       spot=step[1] if kind == "go_to" else "",
                       slot=step[2] if kind == "go_to" else 0, via="invite")
        try:
            ack = await bridge.send(cmd, preserve_id=True)
        except NotConnected:
            await self.jobs.wait(wire, 0)
            return "no_link", "no_link"
        except BridgeError:
            job = await self.jobs.wait(wire, 0)
            return ("done", _done(job)) if job is not None else ("unknown", "unknown")
        if ack.ok:
            return "done", _done(await self.jobs.wait(wire, JOB_WAIT_S))
        await self.jobs.wait(wire, 0)
        return "refused", native_code(ack.error)

    def on_job(self, ev) -> None:
        if getattr(ev, "kind", "") == "string_do":
            self.jobs.feed(ev)

    async def on_hello(self, ev) -> None:
        """A (re)greeting worldserver: its switch and features are asked again."""
        self._off_until = 0.0

    # -- talk -----------------------------------------------------------------------------------

    def reply_turns(self, row, reply_to: str) -> list[ReplyTurn] | None:
        """The answers to his invitation: one per member it asked who answers, in order."""
        inv = self._invites.get(row.id)
        if inv is None or reply_to != inv.channel:
            return None
        to = self.app.human_guid if reply_to == "whisper" else 0
        afk = self.app.world.afk  # an AFK member answers when she is back (talk/owed.py)
        speakers = [m.guid for m in inv.members.values() if m.answers and m.guid not in afk]
        return [ReplyTurn(g, f"reply-{row.id}-invite-{i}", TurnKind.REPLY_HUMAN, reply_to, reply_to, to)
                for i, g in enumerate(speakers)] or None

    def ready(self, line: int, guid: int) -> asyncio.Event | None:
        inv = self._invites.get(line)
        m = inv.members.get(guid) if inv is not None else None
        return m.ready if m is not None else None

    def instruction(self, turn, meta, previous: str) -> str:
        """Her answer's instruction (Pipeline instruction hook): what he asked, what she decided
        and what her body did, as it stands now; his Discord DM inviting her to something in game
        only agrees to meet later."""
        if getattr(meta, "why", "") == W.FOLLOW_WHY:
            return previous
        text = None
        if (turn.channel or "").startswith("dm:") and getattr(meta, "answers_human", False):
            text = self._dm_text(turn, meta)
        elif turn.trigger_line:
            inv = self._invites.get(turn.trigger_line)
            if inv is not None:
                m = inv.members.get(turn.speaker)
                text = self._text(inv, m) if m is not None else W.not_asked(self._forms())
                if m is not None and m.told is None:
                    m.told = sum(1 for _, o, _ in m.outcomes if o == "done")
        if not text:
            return previous
        return f"{previous}\n{text}" if previous else text

    def _text(self, inv: Invite, m: Member) -> str:
        a = inv.ask
        return W.instruction(what=a.what, place=a.place, later=a.later, accept=m.accept,
                             reason=m.reason, steps=m.steps, outcomes=list(m.outcomes),
                             failed=m.failed, done=m.done, note=m.note, forms=self._forms())

    def _dm_text(self, turn, meta) -> str | None:
        if not self.enabled():
            return None
        try:
            his = self.app.engine._his_line(turn, meta)
        except Exception:
            return None
        if not his:
            return None
        ask = P.parse(his, channel="whisper", names=self._names(),
                      his_zone=self.app.world.human_zone)
        if not isinstance(ask, P.Ask) or ask.place is S.UNKNOWN:
            return None
        log_event("invite_dm_later", guid=turn.speaker, what=ask.what)
        return W.later_dm(ask.what, ask.place, self._forms())

    def _follow_up(self, inv: Invite, m: Member) -> None:
        """One short line when her body did more or less than her answer told."""
        if m.told is None or not self.app.world.human_online:
            return  # her answer, still to come, tells the end itself
        text = W.follow_up(what=inv.ask.what, place=inv.ask.place, steps=m.steps,
                           outcomes=list(m.outcomes), failed=m.failed, told_done=m.told,
                           forms=self._forms())
        if text is None:
            return
        hg, now = self.app.human_guid, self.app.clock.now()
        if inv.channel == "whisper":
            turn = Turn(TurnKind.WHISPER, m.guid, "whisper", to_guid=hg, addressee=hg, max_lines=1,
                        deadline=now + 60, instruction=text, delay=2.0)
        else:
            turn = Turn(TurnKind.REPLY_HUMAN, m.guid, inv.channel, addressee=hg, max_lines=1,
                        deadline=now + 60, instruction=text, delay=2.0)
        log_event("invite_followup", line=inv.line, guid=m.guid, failed=m.failed or "-")
        self._spawn(self.app.engine.speak(turn, why=W.FOLLOW_WHY))

    # -- the model, for lines the patterns could not settle ------------------------------------

    def _may_read(self) -> bool:
        arbiter = getattr(self.app, "arbiter", None)
        return arbiter is not None and not getattr(arbiter, "paused", False)

    async def _read(self, held: _Held) -> None:
        try:
            what = await self._classify(held.ev.text)
            if what is not None and not held.released:
                held.ask = P.ask_of(what, held.ev.text, channel=held.ev.channel,
                                    names=self._names(),
                                    his_zone=held.ev.zone or self.app.world.human_zone)
        except asyncio.CancelledError:
            held.done = True
            return
        except Exception as exc:
            log_event("invite_model_failed", level=logging.WARNING, error=type(exc).__name__)
        held.done = True
        await self._settle()

    async def _settle(self) -> None:
        """Held lines whose reading is done go on, in his order: an invitation is opened first,
        then the line goes to talk, which plans its answers."""
        while self._held and self._held[0].done:
            h = self._held.popleft()
            if h.released:
                continue
            h.released = True
            opened = False
            if (h.ask is not None and not self._closed and self.enabled()
                    and self.app.world.human_online and not self._quiet()):
                opened = self._open(h.ev, h.line_id, h.ask)
            log_event("invite_model", line=h.line_id, outcome="invite" if opened else "talk")
            await self.app.release_to_talk(h.ev, h.line_id)

    async def before_his_line(self) -> None:
        """His next line is about to be handled: a line still being read goes to talk first, so
        his lines reach talk in the order he wrote them."""
        if not any(not h.done for h in self._held):
            return
        while self._held:
            h = self._held.popleft()
            if h.task is not None and not h.task.done():
                h.task.cancel()
            if not h.released:
                h.released = True
                log_event("invite_model", line=h.line_id, outcome="talk_first")
                await self.app.release_to_talk(h.ev, h.line_id)

    async def _classify(self, text: str) -> str | None:
        """The live model's class for one line, from SCHEMA's list; None when the model is not
        already loaded, the GPU is paused, it is not sure or the answer is not valid."""
        from leeroy_mind.llm.client import GpuPaused
        from leeroy_mind.llm.scheduler import Priority
        if not await self._model_ready():
            return None
        app = self.app
        model = app.cfg.models.live
        messages = [{"role": "system", "content": SYSTEM},
                    {"role": "user", "content": P.normalize(text)}]

        async def job(llm):
            return await llm.chat(model, messages, options={"temperature": 0.0, "num_predict": 40,
                                                             "num_ctx": 2048},
                                  think=False, format=SCHEMA)
        try:
            result = await app.scheduler.submit(Priority.REPLY_HUMAN, job,
                                                deadline=app.clock.now() + MODEL_S)
        except GpuPaused:
            return None
        return read_answer(getattr(result, "text", ""))

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

    # -- lifetime -------------------------------------------------------------------------------

    def _spawn(self, coro) -> None:
        task = asyncio.get_running_loop().create_task(coro, name="invites")
        self._tasks.add(task)

        def done(t):
            self._tasks.discard(t)
            if not t.cancelled() and t.exception() is not None:
                log_event("invite_failed", level=logging.WARNING, error=type(t.exception()).__name__)
        task.add_done_callback(done)

    async def close(self) -> None:
        self._closed = True
        self.jobs.close()
        tasks = [t for t in self._tasks if not t.done()]
        tasks += [h.task for h in self._held if h.task is not None and not h.task.done()]
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


def read_answer(text: str) -> str | None:
    """The model's class, or None when it is not sure or not valid."""
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    what, sure = data.get("what"), data.get("sure")
    if what not in ("none", "later", *P.WHAT) or type(sure) not in (int, float):
        return None
    return what if sure >= SURE else None


def _steps(ask: P.Ask, *, in_group: bool, feature: bool, his_zone: int):
    """Her steps for an invitation she said yes to, and a note: (steps, note), or (None, reason)
    when the place cannot be reached (she says no with that reason). Without native's
    "invites" feature only the console strings' kinds go (come_to_me, sit, follow, join_group)."""
    what, place = ask.what, ask.place
    spot = place if isinstance(place, S.Spot) else None
    if what == "come":
        return [("come_to_me",)], ""
    if what in ("join", "follow"):
        return ([], "in_group") if in_group else ([("join_group",)], "")
    if what == "meet":
        if spot is None:
            return [("come_to_me",)], ""
        if in_group:
            return [], "with_him"
        if feature:
            return [("go_to", spot.name, 0)], ""
        if spot.zone == his_zone:
            return [("come_to_me",)], ""  # he is in that city: she meets him where he is
        return None, "cant_reach"
    if what == "sit":
        if spot is not None and not in_group:
            if feature:
                return [("go_to", spot.name, 0)], ""  # she waits for him there
            if spot.zone != his_zone:
                return None, "cant_reach"
        return [("come_to_me",), ("sit",)], ""
    if what == "fish":
        if not feature:
            return ([], "with_him") if in_group else ([("come_to_me",)], "no_fish")
        if in_group:
            return [("fish",)], ""
        if spot is not None:
            return [("go_to", spot.name, 0), ("fish",)], ""
        return [("come_to_me",), ("fish",)], ""
    return None, "cant_reach"


def attach(app):
    """Always attached: [talk] invites is read at each line (default on; it does nothing until
    he invites)."""
    from leeroy_mind.guild.protocol import register
    register()  # string_do and the job event (safe again)
    service = Invites(app)
    app.engine.invites = service
    app.engine.add_instruction_hook(service.instruction)

    async def job(ev):
        service.on_job(ev)
    app.register_event_handler("job", job)
    app.register_event_handler("hello", service.on_hello)
    app.register_shutdown_receipt("job", service.on_job)
    app.register_shutdown_drain(service.close)
    return service
