"""Where she is and how closely she is watching, from her day plan: the block at her local minute
gives where she is and her attention per medium; attention gives a weight (who answers) and a
heavy-tailed latency (how soon). `[life] availability` scales both: 0 means always present (the
callers keep today's behaviour), 6 is the design point, 10 the most scarcity. Pure."""
from __future__ import annotations

import math
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


def away_words(lp: LifePresence, medium: str) -> str:
    """Where she is, in words for her prompt and catch-up lines."""
    if lp.block is Block.HOME_DUTY:
        return "with the kids" if lp.kids else "busy at home"
    if lp.block is Block.FREE:
        return "away from the keyboard" if _medium(medium) == "game" else "away from your phone"
    return _AWAY[lp.block]
