"""Members' reactions to the rivals' lines (Task 13).

RealmTurnSource is a Realm part and a TurnSource. react(fact, line) takes at most one turn per
realm line or fact, and at most LIMITS.member_reactions_per_hour an hour, while he is online. The
member is a cast member at 80 who is online, not away and not a former member, and who either
stands where the Horde City channel opens (so saw the line in Trade) or is competitive or social
(so hears of it). The turn is an ASIDE on the guild channel with the instruction

    {rival} from {guild} just said in Trade: "{line}". React in a few words, your way.

propose(now) hands out the turns still due; the part's tick passes them to the Realm's own turn
port (ports.propose), which speaks one per tick through the ordinary pipeline.

A Trade answer instead: now and then (trade_chance) a member who stands in such a city answers
in Trade through realm_say, composed and screened as a rival's line is (speech.member_trade).
That line is stored only in realm_line (kind member, the cast guild); P1b's utterance rows and
_spoke_at never see it (M35).
"""
from __future__ import annotations

from collections import OrderedDict, deque
import logging
import random

from leeroy_mind.talk.turns import Turn, TurnKind

from .config import LIMITS
from .places import trade_cities

log = logging.getLogger(__name__)

FACTION = "horde"  # the rivals' and his: the City channel his members share
TRADE_ANSWER_CHANCE = 0.25
REACT_DEADLINE_S = 20
_HEARS = ("social", "competitor")
_REMEMBER = 256


def instruction(line) -> str:
    return (f'{line.name} from {line.guild_name} just said in Trade: "{line.text}". '
            "React in a few words, your way.")


def _key(fact, line):
    fact_id = getattr(fact, "id", None) if fact is not None else None
    if type(fact_id) is int:
        return f"fact:{fact_id}"
    line_id = getattr(line, "id", None)
    return f"line:{line_id}" if type(line_id) is int else None


class RealmTurnSource:
    """The members' reactions; see the module's docstring."""

    name = "talk"

    def __init__(self, realm, *, rng=None):
        self.realm = realm
        self._rng = rng or random.Random()
        self.trade_chance = TRADE_ANSWER_CHANCE
        self._turns = deque(maxlen=32)
        self._seen = OrderedDict()
        self._times = deque()

    def _live(self):
        context = self.realm.action_context()
        return context.enabled and context.connected and context.presence_known

    def react(self, fact, line):
        """'aside' or 'trade' when a member reacts to the line (or its fact), else None."""
        ports = self.realm.ports
        key = _key(fact, line)
        if key is None or key in self._seen or not self._live() or ports.human_online() is not True:
            return None
        now = ports.now()
        while self._times and now - self._times[0] >= LIMITS.hour_s:
            self._times.popleft()
        if len(self._times) >= LIMITS.member_reactions_per_hour:
            return None
        picked = self._pick()
        if picked is None:
            return None
        member, saw = picked
        self._seen[key] = now
        while len(self._seen) > _REMEMBER:
            self._seen.popitem(last=False)
        self._times.append(now)
        speech = self.realm.part("speech")
        if saw and speech is not None and self._rng.random() < self.trade_chance:
            persona = ports.persona(member.guid)
            name = persona.character_name if persona is not None else member.name
            if speech.member_trade(member.guid, name, self._sketch(persona), line):
                return "trade"
        self._turns.append(Turn(kind=TurnKind.ASIDE, speaker=member.guid, channel="guild",
                                instruction=instruction(line), deadline=now + REACT_DEADLINE_S,
                                max_lines=1))
        return "aside"

    def _pick(self):
        """(member, whether they stand in a Trade city), or None when nobody would react."""
        ports = self.realm.ports
        try:
            cast = ports.cast()
            former = self.realm.ledger.former_members()
            away = ports.away()
        except Exception as error:
            log.debug("realm_reaction_audience_unavailable error=%s", type(error).__name__)
            return None
        human, cities = ports.human().guid, trade_cities(FACTION)
        saw, heard = [], []
        for member in sorted(cast, key=lambda m: m.guid):
            if not member.online or member.guid == human or member.guid in former or member.guid in away:
                continue
            if member.zone in cities:
                saw.append(member)
            elif self._hears(member.guid):
                heard.append(member)
        pool = saw + heard
        if not pool:
            return None
        member = self._rng.choice(pool)
        return member, member in saw

    def _hears(self, guid):
        persona = self.realm.ports.persona(guid)
        game = getattr(persona, "game", None)
        return game is not None and (game.motivation in _HEARS or game.thinks_as == "social")

    @staticmethod
    def _sketch(persona):
        if persona is None:
            return ""
        play = persona.play
        return f"a {play.main_spec} {play.raid_role}, humour {persona.humour}"

    async def propose(self, now) -> list[Turn]:
        """The TurnSource contract: every queued turn still due, once."""
        due = [turn for turn in self._turns if turn.deadline > now]
        self._turns.clear()
        return due

    async def tick(self):
        if not self._live():
            return
        ports = self.realm.ports
        now = ports.now()
        for turn in await self.propose(now):
            ports.propose(turn.speaker, turn.channel, turn.instruction, kind=turn.kind,
                          deadline_s=turn.deadline - now)

    async def on_event(self, ev):
        """Reactions come from speech's sent lines, never from an event of their own."""


def attach(realm):
    """The members' reactions as a part; no I/O."""
    return RealmTurnSource(realm), ()
