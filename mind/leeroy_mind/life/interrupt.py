"""Interruptions and coming back (human-likeness spec section 1: "Mid-conversation, a probability
per block of 'brb', 'kid's up', 'boss walking by', and coming back later"; core plan Task 10).

After a line of hers to him in their whisper or her Discord DM, while his last line to her is
fresh, her day can call her away: `draw_interruption` decides from her presence's block (work,
home duty, the commute, free time, her own game; never asleep, at the gym or out, where she
would not be talking), [life] availability and her stress whether something comes up, what (its
reason code and her line: "brb boss") and for how long. LifeService.after_line draws it and
says her line; while she is away her answers to him wait (LifeService.away_until), and when she
is back to a conversation he left quiet she may say so (`draw_back`).

Her lines fit her: a member who is her own boss (self-employed, she runs her own business:
`own_boss`) never says "brb boss", and one who takes the bus, the train or walks (`drives` false)
never says she has to drive; every block has at least three lines. A work line needs a
workplace (`workplace`: "site"): one who works from home ("remote") has work but no boss
walking by (HOME_WORK), and one on leave, at home or out of work ("none": maternity leave, a
stay-at-home parent) gets the home lines whatever block her day plan paints (self-play audit,
2026-11-17: "brb boss" on maternity leave).

His DMs are answered promptly (owner, 2026-11-17), so LifeService draws none within
MIN_AFTER_HIS_S of his message, after his bad news (talk.hisline.bad_news) or in an explicit
scene he is turning up (the explicit overlay in force and romance.his_heat on his last line).

Pure: no clock, no I/O, no logging. `rng` is the caller's seeded draw (life.seed), never
engine.rng, so [life] availability 0 draws nothing and moves no other draw."""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .day import Block, JobShape, job_shape
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
# a draw past its cap is drawn again under it, at this share of the cap (never a pile at the cap)
UNDER_CAP = (0.4, 1.0)
# her line, by reason (the reason is a code for the logs; the line is never logged)
LINES = {
    "work": ("brb boss", "sec, work", "gotta do a work thing brb"),
    "kids": ("brb kid's up", "sec, kids", "brb the kids"),
    "home": ("brb", "sec", "one sec, brb"),
    "commute": ("gotta drive, brb", "driving, brb", "sec, driving"),
    "door": ("brb door",),
    "phone": ("sec, phone",),
    "food": ("brb food",),
}
# her own boss has no boss to walk by; one who does not drive gets off the bus or train instead
OWN_BOSS_WORK = ("sec, client", "sec, work", "gotta do a work thing brb")
TRANSIT_COMMUTE = ("brb my stop", "sec, getting off", "gotta get off here brb")
# she works from home: work calls her away, no boss walks by
HOME_WORK = ("sec, work", "gotta do a work thing brb", "brb work call")
# no interruption starts this soon after his message: he just wrote, she is reading it
MIN_AFTER_HIS_S = 60.0
_REASONS = {Block.WORK: ("work",), Block.COMMUTE: ("commute",),
            Block.FREE: ("door", "phone", "food"), Block.GAMING: ("door", "phone", "food")}
# back to a conversation he left quiet: said half the time
BACK_LINES = ("back", "back, sorry", "ok back")
BACK_P = 0.5
DELAY_S = (2.0, 6.0)  # her line goes out this long after the line she was saying
# her late answer's excuse for the time she was away (talk.shape LATE: "you were away"), only
# for his line written at least EXCUSE_MIN_S before she was back (none to excuse otherwise)
EXCUSE = "away"
EXCUSE_MIN_S = 20.0
# his DM post while she was away: she finds it this long after she is back
# (DiscordTalk._back_at; health's owner-replies bound reads the same)
BACK_FIND_S = (30.0, 120.0)

# her job or days say she is her own boss (her own business, self-employed, freelance)
_BUSINESS = (r"business|shop|salon|bakery|studio|store|cafe|caf\u00e9|bar|company|firm|practice"
             r"|boutique|clinic|garage|restaurant|pub|farm|agency|gym|parlou?r|kennels?|nursery")
_OWN_BOSS = re.compile(
    r"\b(?:self[- ]employed|freelanc\w*|business owner|owner of|founder|proprietor|entrepreneur"
    r"|sole trader|contractor|(?:her|his|their) own boss|own boss"
    rf"|(?:own(?:s|ed)?|runs?|running) (?:a|an|her|his|their) (?:own )?(?:\w+ ){{0,2}}"
    rf"(?:{_BUSINESS}))\b")
# her days say she takes the bus, the metro or the train, cycles or walks, or does not drive
_NO_CAR = re.compile(
    r"\b(?:bus(?:es)?|tube|subway|metro|tram|public transport|underground|commuter train"
    r"|(?:takes?|taking|on|by) (?:the )?trains?|(?:cycles?|bikes?|walks?) to work"
    r"|(?:doesn|don|can)['\u2019]?t drive|does not drive|no car|no licen[cs]e)\b")


# [talk] coherence (LifeService.configure): a trade with private clients is her own boss too
COHERENT = False
_OWN_TRADE = re.compile(
    r"\b(?:(?:private|(?:her|his|their) own|own) (?:clients|customers|practice|round)"
    r"|works? for (?:her|him|them)self|mobile (?:hairdresser|barber|mechanic|groomer|beautician"
    r"|massage therapist|nail technician)|personal trainer|private tutor|childminder"
    r"|dog (?:walker|groomer))\b")
# her job says she is not working now, whatever shape her day plan reads from it
_NOT_WORKING = re.compile(
    r"\b(?:(?:maternity|paternity|parental|sick|medical|family|extended|unpaid) leave|on leave"
    r"|furlough\w*|sabbatical|laid off|unemployed|between jobs|out of work|retired"
    r"|stay[- ]at[- ]home|housewife|homemaker|full[- ]time (?:mum|mom|mother|parent))\b")
_AT_HOME = re.compile(r"\b(?:wfh|works? from home|working from home|from home|remote(?:ly)?"
                      r"|home office|home studio)\b")


def workplace(p) -> str:
    """Where her work is, for her work lines: "none" when her job says she is not working now
    (on leave, at home, out of work: _NOT_WORKING, or the day plan's HOME shape), "remote" when
    she works from home (the REMOTE shape, _AT_HOME), else "site" (a workplace with people in
    it)."""
    rl = getattr(p, "real_life", None)
    if rl is None:
        return "site"
    text = f"{rl.job} {rl.rhythm}".casefold()
    try:
        shape = job_shape(p)
    except Exception:
        shape = None
    if _NOT_WORKING.search(text) or shape is JobShape.HOME:
        return "none"
    if shape is JobShape.REMOTE or _AT_HOME.search(text):
        return "remote"
    return "site"


def own_boss(p) -> bool:
    """Her job or days say she is her own boss: "brb boss" is not hers (OWN_BOSS_WORK). With
    [talk] coherence also a trade with private clients (nobody walks by her desk)."""
    rl = getattr(p, "real_life", None)
    if rl is None:
        return False
    text = f"{rl.job} {rl.rhythm}".casefold()
    return bool(_OWN_BOSS.search(text) or (COHERENT and _OWN_TRADE.search(text)))


def drives(p) -> bool:
    """Her days give no sign she goes without a car (TRANSIT_COMMUTE otherwise)."""
    rl = getattr(p, "real_life", None)
    if rl is None:
        return True
    text = f"{rl.job} {rl.rhythm} {rl.life_thread} {rl.household}".casefold()
    return not _NO_CAR.search(text)


def lines_for(reason: str, *, boss: bool = False, car: bool = True,
              place: str = "site") -> tuple[str, ...]:
    """Her lines for `reason`: the work lines without a boss for her own boss (`boss`) or when
    she works from home (`place` "remote"), the commute lines without driving when she has no
    car (`car` false)."""
    if reason == "work" and boss:
        return OWN_BOSS_WORK
    if reason == "work" and place != "site":
        return HOME_WORK
    if reason == "commute" and not car:
        return TRANSIT_COMMUTE
    return LINES[reason]


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
                      cap_s: float = DM_CAP_S, boss: bool = False,
                      car: bool = True, place: str = "site") -> Interruption | None:
    """Whether something calls her away now, after a line of hers to him: p = BASE[block] *
    availability / 6 * (1 + max(0, stress)); None at availability 0, without a presence, or in a
    block with no chance (asleep, the gym, out). Its reason and line by block (home duty names
    the kids only when she has them at home; lines_for with `boss`, her own boss, `car` and
    `place`; a work block of a member with no work to be at, `place` "none", gives the home
    reasons),
    and its length lognormal around MEDIAN_S scaled by availability / 6, under `cap_s` (DM_CAP_S
    in her DM; the caller passes 0.9 * game_cap_s in a whisper): a length past the cap is drawn
    again at UNDER_CAP of it, so no pile of aways sits at exactly the cap."""
    if availability <= 0 or lp is None:
        return None
    base = BASE.get(lp.block, 0.0)
    if base <= 0:
        return None
    scale = availability / DESIGN_AVAILABILITY
    if rng.random() >= base * scale * (1 + max(0.0, _stress(state))):
        return None
    if lp.block is Block.HOME_DUTY or (lp.block is Block.WORK and place == "none"):
        reason = "kids" if lp.kids else "home"
    else:
        reason = rng.choice(_REASONS[lp.block])
    line = rng.choice(lines_for(reason, boss=boss, car=car, place=place))
    away = rng.lognormvariate(math.log(MEDIAN_S[lp.block] * scale), SIGMA)
    if away > cap_s:
        away = cap_s * rng.uniform(*UNDER_CAP)
    return Interruption(reason, away, line)


def draw_back(rng) -> str:
    """What she says when she is back to a conversation he left quiet: one of BACK_LINES half
    the time, else "" (she picks it up when he writes)."""
    if rng.random() >= BACK_P:
        return ""
    return rng.choice(BACK_LINES)
