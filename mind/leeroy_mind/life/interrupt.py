"""Interruptions and coming back (human-likeness spec section 1: "Mid-conversation, a probability
per block of 'brb', 'kid's up', 'boss walking by', and coming back later"; core plan Task 10).

After a line of hers to him in their whisper or her Discord DM, while his last line to her is
fresh, her day can call her away: `draw_interruption` decides from her presence's block (work,
home duty, the commute, free time, her own game; never asleep, at the gym or out, where she
would not be talking), [life] availability and her stress whether something comes up, what (its
reason code and her line: "brb boss") and for how long. LifeService.after_line draws it and
says her line; while she is away her answers to him wait (LifeService.away_until), and when she
is back to a conversation he left quiet she may say so (`draw_back`).

Pure: no clock, no I/O, no logging. `rng` is the caller's seeded draw (life.seed), never
engine.rng, so [life] availability 0 draws nothing and moves no other draw."""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .day import Block
from .presence import DESIGN_AVAILABILITY, DISCORD_CAP_S, LifePresence

if TYPE_CHECKING:
    from .state import State

# the chance per line of hers, at the design availability and no stress, by her block
BASE = {Block.WORK: 0.06, Block.HOME_DUTY: 0.06, Block.COMMUTE: 0.08, Block.FREE: 0.015,
        Block.GAMING: 0.01}
# how long she is away, median seconds at the design availability (lognormal, sigma SIGMA)
MEDIAN_S = {Block.WORK: 600.0, Block.HOME_DUTY: 480.0, Block.COMMUTE: 900.0, Block.FREE: 300.0,
            Block.GAMING: 240.0}
SIGMA = 0.5
DM_CAP_S = DISCORD_CAP_S  # the longest away in her DM; a whisper's is 0.9 * game_cap_s
WHISPER_CAP_SHARE = 0.9
# her line, by reason (the reason is a code for the logs; the line is never logged)
LINES = {
    "work": ("brb boss", "sec, work", "gotta do a work thing brb"),
    "kids": ("brb kid's up", "sec, kids", "brb the kids"),
    "home": ("brb", "sec"),
    "commute": ("gotta drive, brb",),
    "door": ("brb door",),
    "phone": ("sec, phone",),
    "food": ("brb food",),
}
_REASONS = {Block.WORK: ("work",), Block.COMMUTE: ("commute",),
            Block.FREE: ("door", "phone", "food"), Block.GAMING: ("door", "phone", "food")}
# back to a conversation he left quiet: said half the time
BACK_LINES = ("back", "back, sorry", "ok back")
BACK_P = 0.5
DELAY_S = (2.0, 6.0)  # her line goes out this long after the line she was saying
# her late answer's excuse for the time she was away (talk.shape LATE: "you were away")
EXCUSE = "away"


@dataclass(frozen=True, slots=True)
class Interruption:
    reason: str
    away_s: float
    line: str


@dataclass(slots=True)
class Away:
    """One time she stepped away (LifeService, in memory only): from `since` (her line said)
    to `until`, on `channel` ("whisper" or "dm:<guid>"); `night`/`minute`, when it began, seed
    the draws that follow it; `back`, what she says when she is back to a quiet conversation
    ("" for nothing); `done`, her coming back is settled."""
    since: float
    until: float
    channel: str
    reason: str
    night: str
    minute: int
    back: str = ""
    done: bool = False


def _stress(state) -> float:
    v = getattr(state, "stress", 0.0) if state is not None else 0.0
    if isinstance(v, bool) or not isinstance(v, (int, float)) or math.isnan(v):
        return 0.0
    return float(v)


def draw_interruption(lp: LifePresence, state: State | None, availability: int, rng, *,
                      cap_s: float = DM_CAP_S) -> Interruption | None:
    """Whether something calls her away now, after a line of hers to him: p = BASE[block] *
    availability / 6 * (1 + max(0, stress)); None at availability 0, without a presence, or in a
    block with no chance (asleep, the gym, out). Its reason and line by block (home duty names
    the kids only when she has them at home), and its length lognormal around MEDIAN_S scaled by
    availability / 6, at most `cap_s` (DM_CAP_S in her DM; the caller passes 0.9 * game_cap_s
    in a whisper)."""
    if availability <= 0 or lp is None:
        return None
    base = BASE.get(lp.block, 0.0)
    if base <= 0:
        return None
    scale = availability / DESIGN_AVAILABILITY
    if rng.random() >= base * scale * (1 + max(0.0, _stress(state))):
        return None
    if lp.block is Block.HOME_DUTY:
        reason = "kids" if lp.kids else "home"
    else:
        reason = rng.choice(_REASONS[lp.block])
    line = rng.choice(LINES[reason])
    away = rng.lognormvariate(math.log(MEDIAN_S[lp.block] * scale), SIGMA)
    return Interruption(reason, min(cap_s, away), line)


def draw_back(rng) -> str:
    """What she says when she is back to a conversation he left quiet: one of BACK_LINES half
    the time, else "" (she picks it up when he writes)."""
    if rng.random() >= BACK_P:
        return ""
    return rng.choice(BACK_LINES)
