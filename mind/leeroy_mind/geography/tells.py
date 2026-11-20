"""Lines that get places wrong, for the talk pipeline's tells (talk/filters.py's tiers).

- slip: her line contradicts her own place, here and now: snow in a month it cannot fall there,
  a heatwave in a cold month or a hard frost in a warm one, a temperature far outside her
  month's, an ocean or mountains where her place has none, a clock time or a time of day that is
  not hers. Soft: asked again once with talk.pipeline.PLACE_HINT, and a retry that still has it
  is kept.
- his_home: a line others read says where he lives (his `[owner] home` or home_detail named in a
  sentence about him), when nothing in the chat shown already said it. Asked again, and a retry
  that still has it is dropped (DROP_ON_RETRY).

Only clauses about here and now count for a slip: never one about the game, another place, him
("where you are"), the past, a plan, a trip, a what-if, or indoors."""
import re
from collections.abc import Iterable
from datetime import datetime

from leeroy_mind.talk.moves import GAME_WORDS

from .config import OwnerHome
from .lines import typical
from .places import Place, all_names

_COARSE = re.compile(r"[.!?;\n]+")
_CLAUSE = re.compile(r"[.!?;,\n]+|\s(?:but|though|although)\s")
_WORD = re.compile(r"[a-z']+")

# Not here and now: the past, a plan, a what-if, a trip, him, indoors, the game
_ELSEWHERE = re.compile(
    r"\b(?:yesterday|last (?:night|week|month|year|winter|summer|spring|fall|autumn|time|weekend)"
    r"|ago|used to|when i was|as a kid|growing up|back (?:home|then|in)|next (?:week|month|year"
    r"|winter|summer|spring|weekend)|tomorrow|someday|one day|if|wish|wished|would|could|imagine"
    r"|dream\w*|pretend|vacation|holiday|trip|visit\w*|went to|going to|gonna|heading to|flying"
    r"|moving|moved|over there|down there|up there|where (?:you|u) (?:are|r|live)|(?:your|ur) "
    r"(?:place|end|side|way|town|city|state)|by (?:you|u)|for (?:you|u)|in here|inside|indoors"
    r"|in the (?:house|office|car|kitchen|shower|bath)|in my (?:room|house|flat|office|car)"
    r"|oven|sauna|freezer|fridge|in game|ingame|in-game|in wow|azeroth|northrend|icecrown"
    r"|storm peaks|dragonblight|winterspring|dun morogh|grizzly hills|zul'?drak|howling fjord"
    r"|borean tundra|sholazar|tanaris|stranglethorn|desolace|silithus|un'?goro|nagrand"
    r"|outland|alterac|server|realm|reset|movie|show|film|book|tv|news)\b")
_SNOW = re.compile(
    r"\b(?:(?:it'?s|its|it is|still|been|keeps?) snowing|snowing (?:here|outside|out|again|hard"
    r"|like crazy|now)|snowed in|snow (?:outside|out there|on the ground|everywhere|day)"
    r"|shovel+(?:ing|ed)? (?:the )?(?:snow|driveway|drive|walk|sidewalk|path)|blizzard (?:here"
    r"|outside|out there|right now)|(?:we|i) (?:got|have|had|just got) (?:\w+ )?(?:inches|feet"
    r"|a foot|a ton|loads|lots|a load) of snow|first snow)\b")
_HEATWAVE = re.compile(r"\bheat ?wave\b")
_FROST = re.compile(
    r"\b(?:it'?s|its|it is) (?:below freezing|icy) (?:out|outside|here)\b|\b(?:frost|ice) on "
    r"(?:the|my) (?:car|windscreen|windshield)\b|\bscrap(?:e|ing) (?:the )?(?:ice|frost)\b"
    r"|\bde-?ic(?:e|ing) (?:the|my) car\b")
_TEMP = re.compile(
    r"\b(?:it'?s|its|it is|it was|been|hit|hitting|reached|getting|up to|down to)\s+(?:like |"
    r"about |around |almost |nearly |over |under |only |barely |a balmy |a cool )?(-?\d{1,3})"
    r"\s*(?:(°|º|degrees?|deg)\s*(f|c|fahrenheit|celsius)?\b|(f|c)\b|(?=\s+(?:out|outside)\b"
    r"(?!\s+of)))")
_OCEAN = re.compile(
    r"\b(?:the |our )?(?:beach|ocean|sea|seaside|coast)(?:'s| is) (?:right |just )?(?:down the "
    r"road|round the corner|around the corner|nearby|close|(?:\d+|two|five|ten|fifteen|twenty|a "
    r"few) (?:min|mins|minutes) (?:away|from (?:me|here|my)))|\b(?:walk|walked|walking|drive|"
    r"drove) (?:down )?to the (?:beach|ocean|sea|seafront)\b|\b(?:i |we )(?:live|living) (?:right"
    r" )?(?:by|on|near|at) the (?:beach|coast|ocean|sea|seaside)\b|\b(?:ocean|sea) (?:view|"
    r"breeze|air) (?:here|from (?:my|our))\b|\bsurf(?:ed|ing)? (?:this morning|before work|after"
    r" work|today)\b|\bbeach (?:near|by|behind) (?:me|my|our|us)\b")
# The sea itself: a beach alone is no slip anywhere (lakes and reservoirs have beaches), and "the
# coast" only where there is no big lake either
_SALT = re.compile(r"\b(?:ocean|sea|seaside|seafront|surf\w*|salt)\b")
_COAST = re.compile(r"\bcoast\b")
_MOUNTAIN = re.compile(
    r"\b(?:the |our )?mountains? (?:near|by|behind|outside|around) (?:me|my|our|us|here|the "
    r"house|town)\b|\bmountains? out (?:my|the) window\b|\b(?:view of|look out at|looking out "
    r"at) (?:the )?mountains?\b|\bup in the mountains (?:near|by) (?:me|us|here)\b")
_CLOCK = re.compile(
    r"\b(?:it'?s|its|it is)\s+(?:like\s+|almost\s+|nearly\s+|past\s+|gone\s+|just after\s+)?"
    r"(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)(?![a-z])")
_PHASE = re.compile(
    r"\b(?:it'?s|its|it is) (?:the )?(middle of the night|early morning|morning|afternoon|"
    r"evening|lunchtime|midnight) (?:here|for me|over here|where i am)\b")
# The hours each time of day may name (her local hour, inclusive start, exclusive end; a day
# part may wrap past midnight)
_PHASE_HOURS = {"middle of the night": (23, 6), "early morning": (4, 10), "morning": (4, 13),
                "afternoon": (11, 19), "evening": (16, 24), "lunchtime": (10, 15),
                "midnight": (22, 3)}
HEAT_SLACK_F = 25.0  # a stated temperature this far above her month's typical high is a slip
COLD_SLACK_F = 30.0  # ...or this far below its typical low
CLOCK_SLACK_H = 2.5


def _fold(text: str) -> str:
    return (text or "").casefold().replace("’", "'")


def _others(place: Place) -> frozenset[str]:
    own = {n.casefold() for n in place.names()} | {place.key.casefold()}
    return all_names() - own


def _about_here(clause: str, others: frozenset[str]) -> bool:
    if _ELSEWHERE.search(clause):
        return False
    words = set(_WORD.findall(clause))
    if words & GAME_WORDS:
        return False
    return not any(re.search(rf"\b{re.escape(o)}\b", clause) for o in others if o in clause)


def _hour_off(said: int, local: datetime) -> float:
    now = local.hour + local.minute / 60
    gap = abs(said - now) % 24
    return min(gap, 24 - gap)


def _in_hours(hour: int, span: tuple[int, int]) -> bool:
    a, b = span
    return a <= hour < b if a < b else hour >= a or hour < b


def slip(text: str, place: Place | None, local: datetime) -> str | None:
    """The first way `text` contradicts her own place at her local time `local` ("snow",
    "heat", "frost", "temperature", "coast", "mountains", "clock"), or None."""
    if place is None or not text:
        return None
    t = _fold(text)
    others = _others(place)
    high, low = typical(place, local)
    month = local.month
    for coarse in _COARSE.split(t):
        for clause in _CLAUSE.split(coarse):
            if not clause.strip() or not _about_here(clause, others):
                continue
            if month not in place.snow and _SNOW.search(clause):
                return "snow"
            if high < 50 and _HEATWAVE.search(clause):
                return "heat"
            if low >= 50 and _FROST.search(clause):
                return "frost"
            for m in _TEMP.finditer(clause):
                unit = (m.group(3) or m.group(4) or "")[:1]
                value = float(m.group(1))
                celsius = unit == "c" or (unit != "f" and place.celsius)
                value_f = value * 9 / 5 + 32 if celsius else value
                if value_f > high + HEAT_SLACK_F or value_f < low - COLD_SLACK_F:
                    return "temperature"
            m = _OCEAN.search(clause)
            if m and place.coast != "ocean" and (
                    _SALT.search(m.group(0))
                    or (place.coast == "none" and _COAST.search(m.group(0)))):
                return "coast"
            if not place.mountains and _MOUNTAIN.search(clause):
                return "mountains"
            m = _CLOCK.search(clause)
            if m:
                hour = int(m.group(1)) % 12 + (12 if m.group(3).startswith("p") else 0)
                minute = int(m.group(2) or 0)
                if 1 <= int(m.group(1)) <= 12 and minute < 60 and _hour_off(
                        hour + minute / 60, local) > CLOCK_SLACK_H:
                    return "clock"
            m = _PHASE.search(clause)
            if m and not _in_hours(local.hour, _PHASE_HOURS[m.group(1)]):
                return "clock"
    return None


_HIM = re.compile(r"\b(?:you|u|ur|your|yours|youre|you're|ya|yall|y'all)\b")


def his_home(text: str, owner: OwnerHome | None, *, him: Iterable[str] = (),
             shown: Iterable[str] = ()) -> bool:
    """`text` names where he lives (his home's name or home_detail) in a sentence about him
    (second person, or one of `him`, his names), and no line of `shown` (the chat the prompt
    showed) names it already."""
    if owner is None or not text:
        return False
    names = {owner.place.label.removeprefix("the ").casefold()}
    if owner.place.country == "US":
        names.add(owner.place.key.casefold())
    if owner.detail:
        names.add(owner.detail.casefold())
    seen = _fold(" ".join(shown))
    names = {n for n in names if n and not re.search(rf"\b{re.escape(n)}\b", seen)}
    if not names:
        return False
    his = [h.casefold() for h in him if h]
    for sentence in _COARSE.split(_fold(text)):
        if not any(re.search(rf"\b{re.escape(n)}\b", sentence) for n in names):
            continue
        if _HIM.search(sentence) or any(re.search(rf"\b{re.escape(h)}\b", sentence) for h in his):
            return True
    return False
