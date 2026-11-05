"""Native raid-lead templates and bounded, fight-scoped urgent callouts.

Event handlers only enqueue. The service owns tick/close and sends through the
normal speech boundary, including the six-turn guard and native voice routing.
"""

import asyncio
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime
import logging
import json
import re

from leeroy_mind.guild.lines import public_text
from leeroy_mind.guild.loot import _get, _put
from leeroy_mind.clock import night_of
from leeroy_mind.talk.turns import TurnKind
from leeroy_mind.wow import BOSS_NAMES

from . import store
from .cards import Card, stock_card
from .ledger import combat_role, context_key


log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Callout:
    spell: str
    state: str
    text: str


CALLOUTS = {
    (631, 0): (Callout("Bone Storm", "start", "bone storm, spread out"),),
    (631, 1): (Callout("Dominate Mind", "done", "mc on {target}, cc them"),),
    (631, 3): (Callout("Mark of the Fallen Champion", "done", "mark on {target}"),),
    (631, 4): (Callout("Gas Spore", "done", "spores out, stack on them"),),
    (631, 5): (Callout("Slime Spray", "start", "slime spray, get out of the front"),
               Callout("Mutated Infection", "done", "{target} has the ooze, take it to the big one")),
    (631, 6): (Callout("Malleable Goo", "start", "goo, move"),
               Callout("Unstable Experiment", "start", "ooze coming, ranged on it")),
    (631, 7): (Callout("Shock Vortex", "start", "vortex, spread"),
               Callout("Empowered Shock Vortex", "start", "empowered vortex, everyone spread")),
    (631, 8): (Callout("Swarming Shadows", "done", "shadows on {target}, take it out"),
               Callout("Pact of the Darkfallen", "done", "pact, group up")),
    (631, 11): (Callout("Frost Beacon", "done", "beacons, spread for tombs"),
                Callout("Blistering Cold", "start", "blistering cold, run out"),
                Callout("Unchained Magic", "done", "unchained, stop casting")),
    (631, 12): (Callout("Defile", "start", "defile on {target}, move"),
                Callout("Infest", "start", "infest, top everyone up"),
                Callout("Necrotic Plague", "done", "plague on {target}"),
                Callout("Shadow Trap", "done", "trap near {target}, move")),
    (649, 1): (Callout("Legion Flame", "done", "legion flame on {target}, run"),
               Callout("Fel Fireball", "start", "interrupt the fireball")),
    (649, 3): (Callout("Light Vortex", "start", "light vortex, get light"),
               Callout("Dark Vortex", "start", "dark vortex, get dark")),
}

HUMAN_ASSIGNMENTS = {
    (631, 0): {"ranged": "bone spikes"}, (631, 1): {"ranged": "adds"},
    (631, 3): {"ranged": "blood beasts"}, (631, 6): {"ranged": "the oozes"},
    (631, 7): {"ranged": "kinetic bombs", "melee": "the active prince"},
    (631, 12): {"ranged": "valkyrs", "melee": "the horrors"},
    (649, 1): {"ranged": "volcanoes and portals"}, (649, 4): {"ranged": "burrowers"},
}


def _sentence(text):
    return " ".join(str(text).split()).rstrip(" .!?")


def _limit(text, limit=255):
    if len(text) <= limit:
        return text
    prefix = text[:limit]
    ends = list(re.finditer(r"[.!?](?:\s|$)", prefix))
    if ends:
        return prefix[:ends[-1].start()+1]
    return prefix[:limit-1].rsplit(" ", 1)[0].rstrip(" ,;:") + "."


def briefing(card: Card, lesson, assignment, *, boss, human):
    text = f"{_sentence(boss)} next. plan: {_sentence(card.call)}."
    if lesson:
        text += f" last time: {_sentence(lesson)}."
    if assignment:
        text += f" {_sentence(human)}, {_sentence(assignment)}."
    return _limit(text)


class VoiceLead:
    MAX_AGE_S = 3.0

    def __init__(self, ports, ground):
        self.ports, self.ground = ports, ground
        self._fight = None
        self._pull_id = None
        self._queue = deque(maxlen=16)
        self._last = {}
        self._minute = deque()
        self._speech = None
        self._closed = False
        self._ended = {}
        self.lifecycle = _EndListener(self)

    def _run(self):
        run = self.ground.listener.run
        if (self._closed or not self.ports.cfg.lead_voice or run is None
                or not run.human_in_raid or run.lead_is_human or run.plan.offline
                or run.plan.lead_guid == self.ports.human_guid or run.session._closing
                or not self.ports.human_online()):
            return None
        return run

    def _token(self, run):
        return (run, asdict(run.plan), run.session.raid_id, run.session.ctx.map_id,
                run.session.ctx.instance_id, getattr(self.ports, "connection_epoch", 0))

    def _valid(self, token):
        run = self._run()
        return (run is token[0] and self._token(run) == token
                and run.session.ctx.instance_id is not None)

    def _fresh(self, ts, *, age=3.0):
        return type(ts) is int and 0 <= self.ports.clock.now() - ts/1000 <= age

    def _wipe_silence(self):
        raid = getattr(getattr(self.ground, "app", None), "raid", None)
        if raid is None:
            return False
        from leeroy_mind.talk.raid import RaidPhase
        return raid.phase(self.ports.clock.now()) is RaidPhase.WIPE_SILENCE

    def pre_call(self, map_id, boss_id, *, call=None):
        """Replace the normal lead pre-call; never send a duplicate speech turn."""
        run = self._run()
        if run is None or map_id != run.session.ctx.map_id or (map_id, boss_id) not in BOSS_NAMES:
            return None
        context = context_key(map_id, boss_id, int(run.plan.size == 25) + 2 * int(run.plan.heroic))
        chosen = getattr(self.ground.listener, "card_for", None)
        applier = getattr(self.ground, "applier", None)
        if applier is not None and not applier.acting(self.ports.clock.now()):
            card = stock_card(context)
        elif chosen is not None:
            card, _ = chosen(context, run)
        else:
            row = store.champion(self.ports.conn, context)
            card = Card.from_row(row) if row else stock_card(context)
        if call is not None:
            from dataclasses import replace
            card = replace(card, call=call)
        world = getattr(getattr(self.ground, "app", None), "world", None)
        human = getattr(world, "members", {}).get(self.ports.human_guid)
        role = None
        if human is not None and human.online and human.map == map_id:
            role = combat_role(human.class_, human.spec_tab, None,
                               tank=run.plan.tank_guid == self.ports.human_guid)
        if role is None:
            row = self.ports.conn.execute(
                "SELECT m.role FROM tg_member m JOIN tg_pull p ON p.id=m.pull "
                "WHERE m.guid=? AND p.raid_id=? ORDER BY p.end_ms DESC LIMIT 1",
                (self.ports.human_guid, run.session.raid_id)).fetchone()
            role = row[0] if row else None
        assignment = card.assignment.get(role) or HUMAN_ASSIGNMENTS.get((map_id, boss_id), {}).get(role)
        if self.ports.on_comms(run.plan.lead_guid):
            lessons = store.lessons(self.ports.conn, context, n=1)
            text = briefing(card, lessons[0]["text"] if lessons else None, assignment,
                            boss=BOSS_NAMES[(map_id, boss_id)], human=self.ports.human().called)
        elif assignment:
            text = _limit(f"{_sentence(card.call)}. {self.ports.human().called}: {_sentence(assignment)}.")
        else:
            return None  # The normal typed call stands unchanged.
        text = public_text(self.ports, text)
        self._receipt(run, "briefing", boss_id, text, "expected")
        return text

    def _receipt(self, run, kind, boss, text, status):
        key, record = self._audit(run)
        event = dict(kind=kind, boss=boss, text=text, status=status, ts=self.ports.clock.now())
        if kind == "briefing" and any(e["kind"] == kind and e["boss"] == boss and e["text"] == text
                                      for e in record["events"]):
            return
        record["events"].append(event)
        if len(record["events"]) > 256:
            record["events"] = record["events"][-256:]
            record["truncated"] = True
        with self.ports.conn:
            _put(self.ports.conn, key, record)

    def _audit(self, run):
        local = datetime.fromtimestamp(run.session.started, tz=self.ports.clock.local().tzinfo)
        key = f"tg_voice:{night_of(local)}:{run.session.raid_id}"
        record = _get(self.ports.conn, key) or {
            "audit_version": 1, "lead": run.plan.lead_guid, "events": [],
            "fights": [], "truncated": False}
        return key, record

    def _fight_receipt(self, run, boss, start, *, end=None, outcome=None):
        key, record = self._audit(run)
        fights = record.setdefault("fights", [])
        matches = [f for f in fights if f["start_ms"] == start and f["boss"] == boss]
        if not matches:
            if end is not None:
                record["truncated"] = True
            else:
                fights.append(dict(boss=boss, start_ms=start, end_ms=None, outcome=None))
        elif end is not None:
            matches[0].update(end_ms=end, outcome=outcome)
        if len(fights) > 256:
            record["fights"] = fights[-256:]
            record["truncated"] = True
        with self.ports.conn:
            _put(self.ports.conn, key, record)

    async def on_boss(self, ev):
        if self._fight is not None and not self._valid(self._fight[0]):
            self._clear()
        run = self._run()
        if (run is None or not self._fresh(ev.ts, age=60) or ev.ts < run.session.started * 1000
                or ev.map != run.session.ctx.map_id or ev.instance_id != run.session.ctx.instance_id
                or not set(run.plan.members).intersection(ev.members)):
            return
        key = (run.session.raid_id, run.session.started, ev.instance_id, ev.boss_id)
        if ev.state == "engaged":
            if ev.ts <= self._ended.get(key, -1):
                return
            if self._fight is not None:
                return  # Duplicate/reordered engage never replaces a live fight.
            self._fight = (self._token(run), ev.boss_id, ev.ts)
            self._pull_id = None
            self._fight_receipt(run, ev.boss_id, ev.ts)
        elif ev.state in {"wiped", "killed"}:
            self._ended[key] = max(ev.ts, self._ended.get(key, -1))
            while len(self._ended) > 256:
                del self._ended[next(iter(self._ended))]
            if self._fight is not None and ev.boss_id == self._fight[1] and ev.ts >= self._fight[2]:
                self._fight_receipt(run, ev.boss_id, self._fight[2], end=ev.ts, outcome=ev.state)
                self._clear()

    def _clear(self):
        self._fight = self._pull_id = None
        self._queue.clear()
        if self._speech is not None:
            self._speech.cancel()

    def invalidate(self, run):
        if self._fight is not None and self._fight[0][0] is run:
            self._clear()

    async def on_end(self, outcome, *, run=None):
        if run is None or self._fight is None or self._fight[0][0] is run:
            self._clear()

    async def on_cast(self, ev):
        fight = self._fight
        if (fight is None or not self._valid(fight[0]) or not self._fresh(ev.ts)
                or self._wipe_silence()
                or not self.ground.gate.recording() or ev.map != fight[0][3]
                or ev.instance_id != fight[0][4] or ev.ts < fight[2]):
            return
        # Ordered server hooks use separate timestamps; the recorder starts at
        # or just after the engage event. Once bound, its exact ID cannot change.
        parts = ev.pull.split("-")
        if len(parts) != 3 or any(not p.isascii() or not p.isdecimal() for p in parts):
            return
        start, instance, boss = map(int, parts)
        if (not fight[2] <= start <= fight[2] + 1000 or start > ev.ts
                or instance != ev.instance_id or boss != fight[1]
                or self._pull_id not in {None, ev.pull}):
            return
        book = self.ground.book()
        if book is None:
            return
        name = book.name(ev.spell).casefold()
        call = next((c for c in CALLOUTS.get((ev.map, boss), ())
                     if c.spell.casefold() == name and c.state == ev.state), None)
        if call is None:
            return
        target = ""
        if "{target}" in call.text:
            if ev.target == self.ports.human_guid:
                target = self.ports.human().called
            elif ev.target in fight[0][0].plan.members:
                member = self.ports.cast().get(ev.target)
                target = member.name if member is not None and member.online else ""
            if not target:
                return  # No invented target from a targetless or stale cast.
        now = self.ports.clock.now()
        while self._minute and now - self._minute[0] >= 60:
            self._minute.popleft()
        if (len(self._minute) >= 4 or now - self._last.get(name, float("-inf")) < 8
                or len(self._queue) == self._queue.maxlen):
            return
        self._pull_id = ev.pull
        self._last[name] = now
        self._minute.append(now)
        self._queue.append((fight, ev.ts, public_text(self.ports, call.text.format(target=target))))

    async def tick(self):
        if self._speech is not None or not self._queue or self._closed:
            return
        fight, ts, text = self._queue.popleft()
        if (self._fight is not fight or not self._valid(fight[0]) or not self._fresh(ts)
                or self._wipe_silence()
                or not self.ground.gate.recording()):
            return
        self._speech = asyncio.create_task(self.ports.say(fight[0][0].plan.lead_guid,
                                                       "raid", text, kind=TurnKind.URGENT))
        try:
            async with asyncio.timeout(10):
                said = await self._speech
            self._receipt(fight[0][0], "callout", fight[1], text, "sent" if said is True else "refused")
        except asyncio.CancelledError:
            if asyncio.current_task().cancelling():
                raise
        except Exception:
            log.warning("training_voice_callout_failed")
        finally:
            self._speech = None

    async def close(self):
        self._closed = True
        task = self._speech
        self._clear()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)


class _EndListener:
    """Only end/invalidation follows the guild's possibly slow listener queue.

    Boss boundaries are consumed inline by TrainingGround, before a following
    cast can be considered. Replaying them from the guild queue is unnecessary.
    """

    def __init__(self, voice):
        self.voice = voice

    def invalidate(self, run):
        self.voice.invalidate(run)

    async def on_end(self, outcome, *, run=None):
        await self.voice.on_end(outcome, run=run)


def voicecheck(conn, night):
    """Report durable delivery evidence, never infer unheard audio or combat quiet."""
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", night):
        raise ValueError("invalid night")
    result = dict(night=night, briefings=0, callouts=0, refused_callouts=0,
                  expected_briefings=0, truncated=False, acceptance="unverified",
                  non_lead_raid_lines_in_combat=None, debriefs=None, kills=None,
                  combat_coverage="unavailable", fact_delivery="unverified", audible_delivery="unverified")
    combat_lines, debriefs, kills = set(), set(), set()
    fight_count, coverage = 0, True
    prefix = f"tg_voice:{night}:"
    for row in conn.execute("SELECT value FROM meta WHERE key LIKE ?", (prefix+"%",)):
        try:
            record = json.loads(row[0])
            lead = record["lead"]
            events = record["events"]
            result["truncated"] |= record["truncated"]
            fights = record.get("fights", [])
            coverage &= record.get("audit_version") == 1 and not record["truncated"]
            for fight in fights:
                start, end = fight["start_ms"], fight["end_ms"]
                if (type(start) is not int or type(end) is not int or end < start
                        or fight["outcome"] not in {"wiped", "killed"}):
                    coverage = False
                    continue
                fight_count += 1
                combat_lines.update(row[0] for row in conn.execute(
                    "SELECT id FROM utterance WHERE channel IN ('raid','voice:raid','voice:raid2') AND dropped IS NULL "
                    "AND guid<>? AND ts>=? AND ts<?", (lead, start/1000, end/1000)))
                # Count actual delivered lead lines with the existing reaction's
                # durable reason. The next engage ends this reaction window.
                next_start = min((f["start_ms"] for f in fights if f["start_ms"] > end),
                                 default=end+60000)
                until = min(end+60000, next_start)
                wiped = fight["outcome"] == "wiped"
                lines = {row[0] for row in conn.execute(
                    "SELECT id FROM utterance WHERE guid=? AND channel IN ('raid','voice:raid','voice:raid2') AND dropped IS NULL "
                    "AND kind=? AND why=? AND ts>=? AND ts<?",
                    (lead, "callout" if wiped else "kill", "wipe:lead" if wiped else "kill:lead",
                     end/1000, until/1000))}
                (debriefs if wiped else kills).update(lines)
            for event in events:
                if event["kind"] == "briefing":
                    result["expected_briefings"] += 1
                    # A pre-call provider only proposes text. Count a briefing
                    # after the guarded pipeline has an actual delivered row.
                    sent = conn.execute("SELECT 1 FROM utterance WHERE guid=? AND text=? AND dropped IS NULL "
                                        "AND kind='callout' AND ts>=? AND ts<=? LIMIT 1",
                                        (lead, event["text"], event["ts"], event["ts"]+60)).fetchone()
                    result["briefings"] += int(sent is not None)
                elif event["kind"] == "callout":
                    result["callouts"] += int(event["status"] == "sent")
                    result["refused_callouts"] += int(event["status"] == "refused")
        except (ValueError, TypeError, KeyError):
            result["truncated"] = True
            coverage = False
    if fight_count:
        result["debriefs"], result["kills"] = len(debriefs), len(kills)
        result["combat_coverage"] = "complete_recorded_intervals" if coverage else "partial"
        if coverage:
            result["non_lead_raid_lines_in_combat"] = len(combat_lines)
    return result
