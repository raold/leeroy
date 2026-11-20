"""His "you lead", carried out: "take the lead", "lead the way", "take us questing", "go grind",
"lead us to the inn". Read from his party, raid and whisper lines (parse.py), the member who leads
chosen (the one he names or whispers, else his group's raid lead, a tank, an officer by the raid
nights she led and attended), and carried out:

- in the open world by native lead_do (Guild/Lead.cpp): she takes his group's lead and runs
  playerbots' own strategies for the activity, the others of his group follow her, and he follows
  her himself; she waits whenever he falls behind;
- inside a dungeon or raid by guild life's raid_control dc_on: dungeon-clear's tank leads the group
  boss to boss (the member he named also becomes his raid lead, who calls it: raidlead/).

Her answer is planned by talk (talk.engine.on_human_chat asks reply_turns), waits for the outcome
(TurnMeta.ready) and its instruction says exactly what happened (words.py). A lead native ends by
itself (arrived, left behind, an hour, no way there, her death) gets one short line from her; his
group going into an instance hands it to dungeon-clear. His "i'll lead", "stop leading", "follow
me" end the lead in force.

[talk] lead (default true; anything but true is off) is read at each line. Logs carry line ids,
guids, activities and codes only, never his words."""
import asyncio
import logging
import re
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field

from leeroy_mind.bridge import BridgeError, NotConnected
from leeroy_mind.guild.ports import JobWaiter
from leeroy_mind.guild.roles import role_of
from leeroy_mind.invites import spots as S
from leeroy_mind.logs import log as log_event
from leeroy_mind.raidlead.designate import nights
from leeroy_mind.talk.select import ReplyTurn
from leeroy_mind.talk.turns import Turn, TurnKind
from leeroy_mind.wow import DUNGEONS, RAID_INSTANCES, RAID_MAPS

from . import parse as P
from . import words as W

log = logging.getLogger(__name__)

GAME_CHANNELS = frozenset({"party", "raid", "raid_warning", "whisper"})
FEATURE = "lead"  # the hello feature: lead_do (LeeroyProtocol.h)
BG_MAPS = frozenset({30, 489, 529, 559, 562, 566, 572, 607, 617, 618, 628})
INSTANCE_MAPS = (frozenset(DUNGEONS) | frozenset(RAID_INSTANCES) | RAID_MAPS) - BG_MAPS
RAID_QUIET = frozenset({"combat", "wipe_silence", "wipe_debrief"})
JOB_WAIT_S = 2.0     # native publishes the done job before its ack; this only covers reordering
HANDOVER_S = 10.0    # after his group goes into an instance, before dungeon-clear is asked
KEPT = 64            # his lines kept, newest first out
_CODE = re.compile(r"[a-z_]{1,40}(:[a-z_0-9]{1,40})?")
_MISMATCH = re.compile(r"leader_mismatch:(\d{1,10})")


def native_code(error: str) -> str:
    """Native's refusal as one code: its own policy codes, an older worldserver's unknown command
    or field as `unsupported`; anything else `refused` (never its text)."""
    error = (error or "").strip()
    if error.startswith(("unknown type", "bad field")):
        return "unsupported"
    return error if _CODE.fullmatch(error) else "refused"


@dataclass(eq=False)
class Asked:
    """One line of his the lead answers. kind: world (lead_do), dc (dungeon-clear), end or refused
    (the mind's own refusal: nothing was sent)."""
    line: int
    channel: str                 # where she answers
    leader: int                  # who answers
    kind: str
    start: P.Start | None = None
    outcome: str = ""            # "" not known yet, "done" or "refused"
    code: str = ""
    tank: int = 0                # dc: dungeon-clear's tank
    was_leading: bool = True     # end: a lead was in force when native heard it
    ready: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass(eq=False)
class Active:
    """The lead in force in the open world, as the mind knows it."""
    id: str
    guid: int
    activity: str
    place: object
    channel: str
    hello: int                   # the worldserver it runs in (hello's started_ms)


@dataclass(eq=False)
class Dc:
    """Dungeon-clear switched on by his "you lead" (or the handover): his to end."""
    tank: int
    leader: int
    channel: str


class LeadService:
    def __init__(self, app):
        self.app = app
        self.jobs = JobWaiter(capacity=64)
        self._asked: OrderedDict[int, Asked] = OrderedDict()
        self.active: Active | None = None
        self.dc: Dc | None = None
        self._tasks: set[asyncio.Task] = set()
        self._closed = False
        self._hello = 0

    # -- switches and facts ---------------------------------------------------------------------

    def enabled(self) -> bool:
        return getattr(getattr(self.app.cfg, "talk", None), "lead", False) is True

    def _feature(self) -> bool:
        hello = getattr(self.app, "hello", None)
        return hello is not None and FEATURE in getattr(hello, "features", ())

    def _quiet(self) -> bool:
        raid = getattr(self.app, "raid", None)
        try:
            return raid is not None and str(raid.phase(self.app.clock.now())) in RAID_QUIET
        except Exception:
            return True

    def _raid_led(self) -> bool:
        """A raid the mind leads (forming, or under way without him driving it) is the guild's."""
        try:
            night = self.app.guild.raid_night
        except AttributeError:
            return False
        run = getattr(night, "run", None)
        state = str(getattr(getattr(night, "state", None), "value", ""))
        if state == "forming":
            return True
        return state in ("assembling", "running") and run is not None and not run.lead_is_human

    def _forms(self) -> tuple[str, str, str]:
        human = getattr(self.app.engine, "human", None)
        forms = getattr(human, "forms", None)
        return tuple(forms) if forms else ("he", "him", "his")

    def _names(self) -> dict[int, str]:
        world, hg = self.app.world, self.app.human_guid
        return {g: m.name for g, m in world.members.items() if g != hg and m.name}

    def _name(self, guid: int) -> str:
        member = self.app.world.members.get(guid)
        return member.name if member is not None and member.name else ""

    def _group(self) -> set[int]:
        """His group's cast members (him left out), when he is in a group."""
        world, hg = self.app.world, self.app.human_guid
        g = world.group
        if g is None:
            return set()
        everyone = set(g.members) | ({g.leader_guid} if g.leader_guid else set())
        if hg not in everyone:
            return set()
        return {m for m in everyone if m != hg and m in world.members}

    def _able(self, guid: int) -> bool:
        world = self.app.world
        return (guid in world.online and guid not in world.afk
                and self.app.personas.get(guid) is not None)

    def _tanks(self, group: set[int]) -> list[int]:
        out = []
        for guid in sorted(group):
            member, persona = self.app.world.members.get(guid), self.app.personas.get(guid)
            if member is None or persona is None:
                continue
            if role_of(member.class_, member.spec_tab, persona.play.raid_role) == "tank":
                out.append(guid)
        return out

    def _pick(self, group: set[int]) -> int | None:
        """Who leads when he names nobody: his group's raid lead, else a tank, else an officer,
        else anyone; within each, the most raid nights led, then attended, then the lowest guid."""
        able = sorted(g for g in group if self._able(g))
        if not able:
            return None
        raidlead = getattr(self.app, "raidlead", None)
        try:
            designated = raidlead.lead() if raidlead is not None else None
        except Exception:
            designated = None
        if designated in able:
            return designated
        try:
            counts = nights(self.app.store.conn, able)
        except Exception:
            counts = {g: (0, 0) for g in able}

        def best(pool):
            return max(pool, key=lambda g: (*counts.get(g, (0, 0)), -g)) if pool else None
        tanks = [g for g in self._tanks(set(able))]
        officers = [g for g in able
                    if getattr(self.app.personas.get(g).play, "guild_role", "") == "officer"]
        return best(tanks) or best(officers) or best(able)

    # -- his line -------------------------------------------------------------------------------

    def claims(self, ev, line_id: int) -> str | None:
        """His line (stored) as his "you lead" or its end: "lead" when it is one (its answer is
        planned by talk, or none for a playerbots command), None for anything else. Synchronous;
        any failure is None (talk)."""
        try:
            app = self.app
            if (self._closed or not self.enabled() or ev.from_guid != app.human_guid
                    or getattr(ev, "via_bridge", False) or ev.channel not in GAME_CHANNELS
                    or not app.world.human_online or self._quiet()):
                return None
            end = P.ends(ev.text)
            if end is not None:
                return self._end(ev, line_id, end)
            start = P.parse(ev.text, channel=ev.channel, names=self._names(),
                            his_zone=ev.zone or app.world.human_zone)
            if start is None:
                return None
            return self._open(ev, line_id, start)
        except Exception as exc:
            log_event("lead_failed", level=logging.WARNING, error=type(exc).__name__)
            return None

    def _keep(self, asked: Asked) -> None:
        self._asked[asked.line] = asked
        while len(self._asked) > KEPT:
            self._asked.popitem(last=False)

    def _open(self, ev, line_id: int, start: P.Start) -> str | None:
        app, world = self.app, self.app.world
        channel = "raid" if ev.channel == "raid_warning" else ev.channel
        group = self._group()
        if ev.channel == "whisper":
            leader = ev.to_guid
        elif start.named:
            leader = start.named[0]
            if leader not in group:
                return None  # a member outside his group did not hear his party line
        else:
            leader = self._pick(group)
        if not leader or app.personas.get(leader) is None or leader == app.human_guid:
            return None
        asked = Asked(line_id, channel, leader, "refused", start)
        his_map = getattr(ev, "map", 0)
        if leader not in group:
            asked.code = "not_in_group"
        elif leader not in world.online:
            asked.code = "not_online"
        elif leader in world.afk:
            asked.code = "afk"
        elif his_map in BG_MAPS:
            asked.code = "in_bg"
        elif self._raid_led():
            asked.code = "raid_led"
        elif his_map in INSTANCE_MAPS:
            asked.kind = "dc"
        elif start.activity == "travel" and not isinstance(start.place, S.Spot):
            asked.code = "unknown_place"
        elif start.activity == "travel" and start.place.map != his_map:
            asked.code = "other_map"
        elif not self._feature():
            asked.code = "unsupported"
        else:
            asked.kind = "world"
        if asked.kind == "refused":
            asked.outcome = "refused"
            asked.ready.set()
        self._keep(asked)
        where = getattr(start.place, "name", "unknown" if start.place is S.UNKNOWN else "-")
        log_event("lead_read", line=line_id, activity=start.activity, who=start.who, where=where,
                  guid=leader, kind=asked.kind, code=asked.code or "-")
        if asked.kind == "world":
            self._spawn(self._start(asked))
        elif asked.kind == "dc":
            self._spawn(self._dc_on(asked))
        return "lead"

    def _end(self, ev, line_id: int, end: P.End) -> str | None:
        """His end of the lead in force (only while one is): native ends it, she answers."""
        active, dc = self.active, self.dc
        if active is None and dc is None:
            return None
        leader = active.guid if active is not None else dc.leader
        if ev.channel == "whisper" and ev.to_guid != leader:
            return None  # a whisper to someone else is talk with her
        channel = "raid" if ev.channel == "raid_warning" else ev.channel
        asked = Asked(line_id, channel, leader, "end")
        self._keep(asked)
        log_event("lead_read", line=line_id, activity="end", who="-", where="-", guid=leader,
                  kind="end", code="-")
        self._spawn(self._stop(asked))
        return "lead"

    # -- carrying it out ------------------------------------------------------------------------

    async def _start(self, asked: Asked) -> None:
        from .protocol import LeadDo
        start = asked.start
        wire = uuid.uuid4().hex
        try:
            cmd = LeadDo(id=wire, action="start", guid=asked.leader, activity=start.activity,
                         spot=start.place.name if isinstance(start.place, S.Spot) else "")
            outcome, code = await self._send(cmd, job=True)
            asked.outcome, asked.code = ("done", "") if outcome == "done" else ("refused", code)
            if outcome == "done":
                self.active = Active(wire, asked.leader, start.activity, start.place, asked.channel,
                                     self._hello)
                self._designate(asked)
            log_event("lead_body", line=asked.line, guid=asked.leader, kind="start",
                      outcome=outcome, code=code or "-")
        except Exception as exc:
            asked.outcome, asked.code = "refused", "unknown"
            log_event("lead_failed", level=logging.WARNING, error=type(exc).__name__)
        finally:
            asked.ready.set()

    async def _stop(self, asked: Asked) -> None:
        try:
            if self.active is not None:
                from .protocol import LeadDo
                active = self.active
                outcome, code = await self._send(LeadDo(id=uuid.uuid4().hex, action="end",
                                                        guid=active.guid))
                if outcome in ("done", "refused") and self.active is active:
                    self.active = None  # ended, or native had none left (no_lead)
                asked.was_leading = not (outcome == "refused" and code == "no_lead")
            elif self.dc is not None:
                dc = self.dc
                self.dc = None
                outcome, code, _ = await self._dc(dc.tank, "dc_off")
            else:
                outcome, code = "refused", "no_lead"
                asked.was_leading = False
            asked.outcome, asked.code = ("done", "") if outcome == "done" else ("refused", code)
            log_event("lead_body", line=asked.line, guid=asked.leader, kind="end",
                      outcome=outcome, code=code or "-")
        except Exception as exc:
            asked.outcome, asked.code = "refused", "unknown"
            log_event("lead_failed", level=logging.WARNING, error=type(exc).__name__)
        finally:
            asked.ready.set()

    def _designate(self, asked: Asked) -> None:
        """The member he named or whispered also calls his raid (raidlead/), as his designation
        "Ana, you lead" did before the lead read it; one he did not name changes nothing there."""
        raidlead = getattr(self.app, "raidlead", None)
        if raidlead is not None and asked.start is not None and asked.start.who in ("you", "named"):
            try:
                raidlead.designate(asked.leader)
            except Exception as exc:
                log_event("lead_failed", level=logging.WARNING, error=type(exc).__name__)

    async def _dc_on(self, asked: Asked) -> None:
        try:
            group = self._group()
            tanks = self._tanks(group)
            outcome, code, tank = await self._dc(tanks[0] if tanks else asked.leader, "dc_on")
            asked.outcome, asked.code, asked.tank = (
                ("done", "", tank) if outcome == "done" else ("refused", code, 0))
            if outcome == "done":
                self.dc = Dc(tank, asked.leader, asked.channel)
                self._designate(asked)
            log_event("lead_body", line=asked.line, guid=asked.leader, kind="dc_on",
                      outcome=outcome, code=code or "-")
        except Exception as exc:
            asked.outcome, asked.code = "refused", "unknown"
            log_event("lead_failed", level=logging.WARNING, error=type(exc).__name__)
        finally:
            asked.ready.set()

    async def _dc(self, tank: int, action: str) -> tuple[str, str, int]:
        """Guild life's raid_control for his group's dungeon-clear, led by `tank`; native names the
        tank dungeon-clear elects when it is another (leader_mismatch:<guid>), asked once more.
        (outcome, code, the tank)."""
        from leeroy_mind.guild.protocol import RaidControl
        for _ in range(2):
            cmd = RaidControl(id=uuid.uuid4().hex, member_guid=self.app.human_guid, action=action,
                              leader_guid=tank)
            outcome, code = await self._send(cmd)
            found = _MISMATCH.fullmatch(code) if outcome == "refused" else None
            if found is None:
                return outcome, code, tank
            elected = int(found.group(1))
            if not elected:
                return "refused", "no_tank", 0
            if elected == tank:
                break
            tank = elected
        return "refused", "dc_refused", 0

    async def _send(self, cmd, *, job: bool = False) -> tuple[str, str]:
        """One command: ("done", ""), ("refused", code), ("no_link", "no_link") or ("unknown",
        "unknown") when the ack was lost (and, for a start, no done job came)."""
        bridge = getattr(self.app, "bridge", None)
        if bridge is None or not getattr(bridge, "connected", False):
            return "no_link", "no_link"
        if job:
            self.jobs.expect(cmd.id)
        try:
            ack = await bridge.send(cmd, preserve_id=True)
        except NotConnected:
            if job:
                await self.jobs.wait(cmd.id, 0)
            return "no_link", "no_link"
        except BridgeError:
            found = await self.jobs.wait(cmd.id, 0) if job else None
            return ("done", "") if found is not None else ("unknown", "unknown")
        if ack.ok:
            if job:
                await self.jobs.wait(cmd.id, JOB_WAIT_S)
            return "done", ""
        if job:
            await self.jobs.wait(cmd.id, 0)
        return "refused", native_code(ack.error)

    # -- native's events ------------------------------------------------------------------------

    def on_job(self, ev) -> None:
        kind = getattr(ev, "kind", "")
        if kind == "lead_do":
            self.jobs.feed(ev)
        elif kind == "lead" and getattr(ev, "state", "") == "ended":
            self._ended(ev)

    def _ended(self, ev) -> None:
        active = self.active
        if active is None or ev.id != active.id:
            return  # not ours (an earlier mind's), or already ended by his own end
        self.active = None
        why = ev.reason if _CODE.fullmatch(ev.reason or "") else "unknown"
        log_event("lead_ended", guid=active.guid, why=why)
        if self._closed or not self.app.world.human_online:
            return
        if why == "instance":
            self._spawn(self._handover(active))
            return
        text = W.ended(why, active.place, self._forms())
        if text is not None:
            self._follow_up(active.guid, active.channel, text)

    async def _handover(self, active: Active) -> None:
        """His group went into an instance while she led: dungeon-clear takes it from there."""
        await self.app.clock.sleep(HANDOVER_S)
        if self._closed or not self.app.world.human_online or self._raid_led():
            return
        group = self._group()
        if active.guid not in group:
            return
        tanks = self._tanks(group)
        outcome, code, tank = await self._dc(tanks[0] if tanks else active.guid, "dc_on")
        if outcome == "done":
            self.dc = Dc(tank, active.guid, active.channel)
        log_event("lead_handover", guid=active.guid, outcome=outcome, code=code or "-")
        name = None if tank == active.guid else self._name(tank) or None
        self._follow_up(active.guid, active.channel,
                        W.handed_over(name, "" if outcome == "done" else code, self._forms()))

    async def on_hello(self, ev) -> None:
        """A (re)greeting worldserver: a lead of an earlier worldserver is gone with it; with none
        of ours in force, one an earlier mind left running is ended (it has nobody to end it)."""
        started = getattr(ev, "started_ms", 0) or 0
        if self.active is not None and started != self.active.hello:
            self.active = None
        if started != self._hello:
            self.dc = None
        self._hello = started
        if (self.active is None and FEATURE in getattr(ev, "features", ())
                and not self._closed):
            self._spawn(self._clear())

    async def _clear(self) -> None:
        from .protocol import LeadDo
        outcome, code = await self._send(LeadDo(id=uuid.uuid4().hex, action="end"))
        if outcome == "done":
            log_event("lead_cleared", why="hello")

    async def on_group(self, ev) -> None:
        if self.dc is not None and not self._group():
            self.dc = None  # his group broke: dungeon-clear's run went with it

    # -- talk -----------------------------------------------------------------------------------

    def reply_turns(self, row, reply_to: str) -> list[ReplyTurn] | None:
        """The answer to his "you lead": hers, the member he asked (none for a bot command)."""
        asked = self._asked.get(row.id)
        if asked is None or reply_to != asked.channel or asked.leader in self.app.world.afk:
            return None
        to = self.app.human_guid if reply_to == "whisper" else 0
        return [ReplyTurn(asked.leader, f"reply-{row.id}-lead", TurnKind.REPLY_HUMAN, reply_to,
                          reply_to, to)]

    def ready(self, line: int, guid: int) -> asyncio.Event | None:
        asked = self._asked.get(line)
        return asked.ready if asked is not None and asked.leader == guid else None

    def instruction(self, turn, meta, previous: str) -> str:
        """Her answer's instruction (Pipeline instruction hook): what he asked and what really
        happened, as it stands now."""
        if getattr(meta, "why", "") == W.FOLLOW_WHY or not turn.trigger_line:
            return previous
        asked = self._asked.get(turn.trigger_line)
        if asked is None:
            return previous
        forms = self._forms()
        text = (self._text(asked, forms) if turn.speaker == asked.leader
                else W.not_asked(self._name(asked.leader), forms))
        return f"{previous}\n{text}" if previous else text

    def _text(self, asked: Asked, forms) -> str:
        start = asked.start
        place = start.place if start is not None else None
        if asked.kind == "end":
            return W.stopped(forms, was_leading=asked.was_leading)
        if not asked.outcome:
            return W.pending(forms)
        if asked.kind == "dc":
            if asked.outcome == "done":
                return W.dc_started(None if asked.tank == asked.leader
                                    else self._name(asked.tank) or None, forms)
            return W.dc_refused(asked.code, forms)
        if asked.outcome == "done":
            return W.started(start.activity, place, forms)
        return W.refused(asked.code, place, forms)

    def _follow_up(self, guid: int, channel: str, text: str) -> None:
        """One short line of hers about a lead that ended (or was handed over) by itself."""
        if not self.app.world.human_online or guid not in self.app.world.online:
            return
        hg, now = self.app.human_guid, self.app.clock.now()
        if channel == "whisper":
            turn = Turn(TurnKind.WHISPER, guid, "whisper", to_guid=hg, addressee=hg, max_lines=1,
                        deadline=now + 60, instruction=text, delay=2.0)
        else:
            group = self.app.world.group
            channel = "raid" if group is not None and group.raid else "party"
            turn = Turn(TurnKind.REPLY_HUMAN, guid, channel, addressee=hg, max_lines=1,
                        deadline=now + 60, instruction=text, delay=2.0)
        log_event("lead_followup", guid=guid)
        self._spawn(self.app.engine.speak(turn, why=W.FOLLOW_WHY))

    # -- lifetime -------------------------------------------------------------------------------

    def _spawn(self, coro) -> None:
        task = asyncio.get_running_loop().create_task(coro, name="lead")
        self._tasks.add(task)

        def done(t):
            self._tasks.discard(t)
            if not t.cancelled() and t.exception() is not None:
                log_event("lead_failed", level=logging.WARNING, error=type(t.exception()).__name__)
        task.add_done_callback(done)

    async def close(self) -> None:
        self._closed = True
        self.jobs.close()
        tasks = [t for t in self._tasks if not t.done()]
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


def attach(app):
    """Always attached: [talk] lead is read at each line (default on; it does nothing until he
    asks a member to lead)."""
    from leeroy_mind.guild.protocol import register as register_guild
    from leeroy_mind.protocol import Group
    from .protocol import register
    register_guild()  # raid_control and the job event (safe again)
    register()
    service = LeadService(app)
    app.engine.leading = service
    app.engine.add_instruction_hook(service.instruction)

    async def job(ev):
        service.on_job(ev)

    async def group(ev):
        if isinstance(ev, Group):
            await service.on_group(ev)
    app.register_event_handler("job", job)
    app.register_event_handler("hello", service.on_hello)
    app.register_event_handler("group", group)
    app.register_shutdown_receipt("job", service.on_job)
    app.register_shutdown_drain(service.close)
    return service
