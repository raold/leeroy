"""Where she is and how closely she is watching, from her day plan: the block at her local minute
gives where she is and her attention per medium; attention gives a weight (who answers) and a
heavy-tailed latency (how soon). `[life] availability` scales both: 0 means always present (the
callers keep today's behaviour), 6 is the design point, 10 the most scarcity. Pure."""
from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import IntEnum, StrEnum
from typing import Literal

from ..personas.schema import Persona
from .day import Block, DayEvent, day_plan

# the blocks in which she cannot take a photo of herself (photos/requests.py): asleep, driving
NO_PHOTO = frozenset({Block.ASLEEP, Block.COMMUTE})

DESIGN_AVAILABILITY = 6
DISCORD_CAP_S = 2700.0


class Attention(IntEnum):
    NONE = 0
    GLANCING = 1
    HALF = 2
    FULL = 3


class Where(StrEnum):
    ASLEEP = "asleep"
    AT_WORK = "at_work"
    BUSY = "busy"
    FREE = "free"
    GAMING = "gaming"


# block: (attention in game, attention on Discord, where she is)
_BY_BLOCK = {
    Block.ASLEEP: (Attention.NONE, Attention.NONE, Where.ASLEEP),
    Block.WORK: (Attention.GLANCING, Attention.GLANCING, Where.AT_WORK),
    Block.COMMUTE: (Attention.NONE, Attention.GLANCING, Where.BUSY),
    Block.HOME_DUTY: (Attention.GLANCING, Attention.HALF, Where.BUSY),
    Block.GYM: (Attention.NONE, Attention.GLANCING, Where.BUSY),
    Block.OUT: (Attention.NONE, Attention.GLANCING, Where.BUSY),
    Block.FREE: (Attention.HALF, Attention.FULL, Where.FREE),
    Block.GAMING: (Attention.FULL, Attention.HALF, Where.GAMING),
}
BASE_W = {Attention.FULL: 1.0, Attention.HALF: 0.6, Attention.GLANCING: 0.2, Attention.NONE: 0.05}
# attention: (median seconds at availability 6, sigma of the log)
LATENCY = {Attention.FULL: (3.0, 0.5), Attention.HALF: (25.0, 0.8),
           Attention.GLANCING: (150.0, 0.9), Attention.NONE: (300.0, 0.7)}
_AWAY = {Block.ASLEEP: "asleep", Block.WORK: "at work", Block.COMMUTE: "driving",
         Block.GYM: "at the gym", Block.OUT: "out", Block.GAMING: "in the middle of something in game"}


@dataclass(frozen=True, slots=True)
class LifePresence:
    block: Block
    where: Where
    attention: Attention
    event: DayEvent
    kids: bool


def _medium(medium: str) -> str:
    if medium not in ("game", "discord"):
        raise ValueError("medium must be game or discord")
    return medium


def presence_at(p: Persona, her_local: datetime, *, medium: Literal["game", "discord"],
                in_his_group: bool = False) -> LifePresence:
    """At her local wall-clock time. In game, a member in his group gives him full attention."""
    _medium(medium)
    plan = day_plan(p, her_local.date())
    block = plan.block_at(her_local.hour * 60 + her_local.minute)
    game, discord, where = _BY_BLOCK[block]
    attention = discord if medium == "discord" else Attention.FULL if in_his_group else game
    return LifePresence(block, where, attention, plan.event, plan.kids)


def free_after(p: Persona, her_local: datetime, busy=NO_PHOTO) -> datetime | None:
    """Her local wall-clock time when her day plan next leaves the blocks of `busy`, from
    `her_local` (the start of the first minute in another block); None when she is not in one of
    them now. Today's plan, then the next two days'."""
    minute = her_local.hour * 60 + her_local.minute
    if day_plan(p, her_local.date()).block_at(minute) not in busy:
        return None
    midnight = her_local.replace(hour=0, minute=0, second=0, microsecond=0)
    for days in range(3):
        for span in day_plan(p, her_local.date() + timedelta(days=days)).spans:
            if span.block not in busy and (days or span.start > minute):
                return midnight + timedelta(days=days, minutes=span.start)
    return None


def attention_weight(att: Attention, availability: int) -> float:
    """How likely she is the one who answers, relative to a member giving full attention."""
    if availability <= 0:
        return 1.0
    return BASE_W[Attention(att)] ** (availability / DESIGN_AVAILABILITY)


def game_cap_s(availability: int) -> float:
    return 60.0 + 54.0 * availability


def latency(att: Attention, availability: int, rng, *, medium: str, energy: float = 0.0,
            stress: float = 0.0) -> float:
    """Seconds before she answers: lognormal by attention, capped per medium. Availability 0
    has no draw here (ValueError): the caller keeps today's."""
    if availability <= 0:
        raise ValueError("availability 0 keeps today's latency")
    cap = game_cap_s(availability) if _medium(medium) == "game" else DISCORD_CAP_S
    att = Attention(att)
    median, sigma = LATENCY[att]
    if att is not Attention.FULL:
        median *= availability / DESIGN_AVAILABILITY
    if energy < -0.3:
        median *= 1.3
    if stress > 0.3:
        median *= 1.2
    return min(cap, rng.lognormvariate(math.log(median), sigma))


def discord_state(lp: LifePresence, *, dnd_day: bool) -> str:
    """Her Discord status from a `medium="discord"` presence."""
    if lp.attention is Attention.NONE:
        return "offline"
    if lp.attention is Attention.GLANCING:
        return "dnd" if dnd_day and lp.block is Block.WORK else "idle"
    return "online"


# Where she is, as her prompt's "Right now" says it (core plan Task 8, with [life] availability
# above 0): asleep and at work read differently in game and on her phone, home duty names the
# kids only when she has them at home.
WHERE_TEXT = {
    (Block.ASLEEP, "game"): "You're half asleep at the keyboard.",
    (Block.ASLEEP, "discord"): "Your phone woke you up.",
    (Block.WORK, "game"): "You're at work, the game open on your PC.",
    (Block.WORK, "discord"): "You're at work, checking your phone between tasks.",
    (Block.COMMUTE, ""): "You're on your way somewhere.",
    (Block.HOME_DUTY, "kids"): "You're at home sorting the kids out.",
    (Block.HOME_DUTY, ""): "You're busy at home.",
    (Block.GYM, ""): "You're at the gym.",
    (Block.OUT, ""): "You're out tonight.",
    (Block.FREE, ""): "You're at home with some free time.",
    (Block.GAMING, ""): "You're at your PC, playing.",
}


def where_text(lp: LifePresence, medium: str, *, playing: bool = False) -> str:
    """WHERE_TEXT for her presence on `medium` ("game" or "discord"). `playing`: she is in his
    group in game while he plays, so she is at her PC playing whatever her day plan says (PM log
    D15: she answers him at once then too)."""
    medium = _medium(medium)
    if playing:
        return WHERE_TEXT[(Block.GAMING, "")]
    if lp.block is Block.HOME_DUTY:
        return WHERE_TEXT[(Block.HOME_DUTY, "kids" if lp.kids else "")]
    return WHERE_TEXT.get((lp.block, medium)) or WHERE_TEXT[(lp.block, "")]


# One "right now" per conversation ([talk] coherence; self-play audit, 2026-11-17: a member said
# she was at work on the worst shift of her life, joined his group, and her next prompt said
# only "You're at your PC, playing", so her next line was "it's a day off"; grouped members
# talked as if they were elsewhere while he pulled a boss with them). Where she said she is, read
# from her own lines in the conversation (stated_where), holds for the conversation: the line
# agrees with it (settled_where), and a member in his group is told so, with the place.
PLAYING_WITH = "You're at your PC, playing: you're in {name}'s {group} with {them} right now{place}."
STATED_WORDS = {"work": "at work", "bed": "in bed", "out": "out", "home": "home"}
# she said it, and her plan (or his group) now has her elsewhere: what the line says instead
STILL = {"work": "You're still at work, as you told {them} a little while ago.",
         "bed": "You're still in bed, as you told {them} a little while ago.",
         "out": "You're still out, as you told {them} a little while ago.",
         "home": "You're at home, as you told {them} a little while ago."}
JOINED = {"work": "You told {them} a little while ago you were at work: you've just got in "
                  "from work and logged on.",
          "bed": "You told {them} a little while ago you were in bed: you got back up and "
                 "logged on.",
          "out": "You told {them} a little while ago you were out: you've just got back in "
                 "and logged on."}
_APOS = "['\u2019\u02bc]?"
_I = rf"(?:i{_APOS}m|i am|im|still|stuck|been)"
_STATED = (
    # home first: "just got in from work" is no "at work"
    ("home", re.compile(
        rf"\b(?:(?:just )?(?:got|get|back|home)(?: (?:in|home|back))? from (?:work|my shift|the "
        rf"gym|the shops?)|(?:just )?(?:finished|done with|off|left|clocked off) (?:work|my shift)"
        rf"|(?:it{_APOS}s|its|on|got|have|having) (?:a|my) day off|off (?:work )?today"
        rf"|{_I} (?:at )?home|closed up|logged off work)\b", re.IGNORECASE)),
    ("work", re.compile(
        rf"\b(?:{_I} (?:at|in) (?:work|the office|the shop|the bar|the hospital|the plant|the "
        rf"lab|the studio|the depot|the clinic|the site)|at work\b|on (?:my |a |the )?shift"
        rf"|(?:this|my|whole|worst|long|double|late|night) shift|between (?:tasks|calls|"
        rf"customers|clients|patients|tables|meetings)|on my (?:lunch |tea )?break"
        rf"|my (?:boss|manager|supervisor) (?:is|just|keeps)|at my desk)\b", re.IGNORECASE)),
    ("bed", re.compile(
        rf"\b(?:{_I} in bed|in bed\b|under the (?:covers|duvet)|half asleep|"
        rf"about to (?:sleep|pass out)|woke me up|just woke up)\b", re.IGNORECASE)),
    ("out", re.compile(
        rf"\b(?:{_I} (?:out|at the (?:gym|shops?|pub|store|vet|dentist|doctors?)|driving|on "
        rf"the (?:bus|train|road))|(?:on|at) the (?:bus|train)\b|at the gym\b|out with (?:the|my)"
        rf"|in the car\b)\b", re.IGNORECASE)),
)
_STATED_BLOCKS = {"work": frozenset({Block.WORK}), "bed": frozenset({Block.ASLEEP}),
                  "out": frozenset({Block.OUT, Block.GYM, Block.COMMUTE}),
                  "home": frozenset({Block.FREE, Block.GAMING, Block.HOME_DUTY, Block.ASLEEP})}


def stated_where(lines: Sequence[str]) -> str:
    """Where she last said she is, from her own `lines` in this conversation (oldest first):
    "work", "bed", "out" or "home" from the newest line that says one; "" when none does."""
    for line in reversed(list(lines)):
        for kind, pattern in _STATED:
            if pattern.search(line or ""):
                return kind
    return ""


def playing_text(name: str, them: str, *, raid: bool = False, place: str = "") -> str:
    """"Right now" for a member in his group while he plays: at her PC, in his group or raid
    with him, and where they are when the instance is known."""
    return PLAYING_WITH.format(name=name, them=them, group="raid" if raid else "group",
                               place=f", in {place}" if place else "")


def settled_where(text: str, stated: str, lp: LifePresence, *, playing: bool, them: str) -> str:
    """`text` (her "Right now" line) made to agree with where she said she is a little while
    ago (`stated`: stated_where): unchanged when she said nothing or it already agrees; in
    his group (`playing`) she has just come from there and logged on (JOINED), then `text`;
    else she is still where she said (STILL), whatever her day plan has moved on to."""
    if not stated or not text:
        return text
    if playing:
        joined = JOINED.get(stated)
        return f"{joined.format(them=them)} {text}" if joined else text
    if lp.block in _STATED_BLOCKS[stated]:
        return text
    return STILL[stated].format(them=them)


# Her brb's reason, carried into her next prompts in that conversation (asked who was at the
# door, she said "nobody": the reason was a code her prompt never saw). By interrupt.LINES' codes.
AWAY_NOTE = "A few minutes ago you told {them} you had to step away: {why}. You're back now."
AWAY_WHY = {"work": "something came up at work", "kids": "the kids needed you",
            "home": "something needed you at home", "commute": "you were on the move",
            "door": "someone was at the door", "phone": "your phone rang",
            "food": "you went to get something to eat"}
AWAY_NOTE_S = 1800.0  # how long after she is back the note stays


def away_note(reason: str, them: str) -> str:
    """The "Right now" sentence that says why she stepped away ("" for a reason it has no
    words for)."""
    why = AWAY_WHY.get(reason)
    return AWAY_NOTE.format(them=them, why=why) if why else ""


def away_words(lp: LifePresence, medium: str) -> str:
    """Where she is, in words for her prompt and catch-up lines."""
    if lp.block is Block.HOME_DUTY:
        return "with the kids" if lp.kids else "busy at home"
    if lp.block is Block.FREE:
        return "away from the keyboard" if _medium(medium) == "game" else "away from your phone"
    return _AWAY[lp.block]
