"""Her feeling right now: five numbers, each in [-1, 1], that move through her day (section 3
of the human-likeness design). Pure: no clock, database, model or log; the service that keeps
them is `life.statebook`.

- valence: how good she feels (the night's PAD pleasure is her baseline);
- energy: awake and lively, or worn out (the night's PAD arousal, less after waking, before bed,
  after a shift and on a late shift);
- stress: neuroticism, work and the day's trouble;
- social: how much she wants to talk (extraversion);
- toward_him: how she feels about him right now, short-term and apart from the relationship
  row (last night's feelings about him seed it).

Events push the numbers (`Impulse`, `apply`) and they ease back to her baseline over hours
(`decayed`: half-lives of 3 h, 6 h for toward_him). `words` and `lands` put the state into her
prompt's words; her feeling about him shows only on his turns."""
from __future__ import annotations

import re
from dataclasses import dataclass, fields
from datetime import timedelta
from enum import StrEnum
from typing import Literal

from ..personas.schema import Persona
from .day import DAY_MIN, Block, DayEvent, DayPlan, Span, day_plan


@dataclass(frozen=True, slots=True)
class State:
    valence: float
    energy: float
    stress: float
    social: float
    toward_him: float


AXES = tuple(f.name for f in fields(State))
HOUR_S = 3600.0
HALF_LIFE_S = {"valence": 3 * HOUR_S, "energy": 3 * HOUR_S, "stress": 3 * HOUR_S,
               "social": 3 * HOUR_S, "toward_him": 6 * HOUR_S}


class Impulse(StrEnum):
    HIS_WARM = "his_warm"
    HIS_HARSH = "his_harsh"
    HIS_PLAIN = "his_plain"
    IGNORED = "ignored"
    KILL = "kill"
    WIPE = "wipe"
    LOOT_WON = "loot_won"
    LOOT_LOST = "loot_lost"
    TALKED = "talked"


IMPULSES: dict[Impulse, dict[str, float]] = {
    Impulse.HIS_WARM: {"toward_him": 0.15, "valence": 0.08, "social": 0.05},
    Impulse.HIS_HARSH: {"toward_him": -0.25, "valence": -0.12, "stress": 0.10},
    Impulse.HIS_PLAIN: {"toward_him": 0.04, "social": 0.02},
    Impulse.IGNORED: {"toward_him": -0.08, "valence": -0.04, "social": -0.03},
    Impulse.KILL: {"valence": 0.15, "energy": 0.10, "stress": -0.08},
    Impulse.WIPE: {"valence": -0.06, "stress": 0.08, "energy": -0.04},
    Impulse.LOOT_WON: {"valence": 0.20},
    Impulse.LOOT_LOST: {"valence": -0.12, "stress": 0.04},
    Impulse.TALKED: {"social": -0.04},
}

# The baseline's day (her local minutes)
EVENT_VALENCE = {DayEvent.BAD_DAY: -0.25, DayEvent.NIGHT_OUT: 0.10, DayEvent.SICK_KID: -0.10}
EVENT_STRESS = {DayEvent.SICK_KID: 0.25, DayEvent.BAD_DAY: 0.20}
WAKE_MIN, WAKE_DIP = 60, 0.4            # just up
BED_MIN, BED_DIP = 90, 0.3              # nearly bedtime
AFTER_WORK_MIN, AFTER_WORK_DIP = 60, 0.15
LATE_SHIFT_DIP = 0.15
WORK_STRESS = 0.15
TRAIT_MID, TRAIT_SPAN = 5.5, 9.0


def _clamp(x: float) -> float:
    return max(-1.0, min(1.0, float(x)))


def clamped(**values: float) -> State:
    return State(**{axis: _clamp(values.get(axis, 0.0)) for axis in AXES})


def _merged(spans: list[Span]) -> list[Span]:
    out: list[Span] = []
    for s in spans:
        if out and out[-1].block is s.block and out[-1].end == s.start:
            out[-1] = Span(out[-1].start, s.end, s.block)
        else:
            out.append(s)
    return out


def _timeline(p: Persona, plan: DayPlan) -> list[Span]:
    """Yesterday, the plan's day and tomorrow as one run of spans in the plan day's minutes
    (yesterday negative, tomorrow from 1440), a block that crosses midnight as one span."""
    spans: list[Span] = []
    for offset in (-1, 0, 1):
        day = plan if offset == 0 else day_plan(p, plan.day + timedelta(days=offset))
        base = offset * DAY_MIN
        spans.extend(Span(s.start + base, s.end + base, s.block) for s in day.spans)
    return _merged(spans)


def _energy_dips(p: Persona, plan: DayPlan, minute: int) -> float:
    timeline = _timeline(p, plan)
    now = next(s for s in timeline if s.start <= minute < s.end)
    dip = 0.0
    if now.block is not Block.ASLEEP:
        woke = max((s.end for s in timeline if s.block is Block.ASLEEP and s.end <= minute),
                   default=None)
        if woke is not None and minute - woke < WAKE_MIN:
            dip += WAKE_DIP
        bed = min((s.start for s in timeline if s.block is Block.ASLEEP and s.start > minute),
                  default=None)
        if bed is not None and bed - minute <= BED_MIN:
            dip += BED_DIP
    shift_end = max((s.end for s in timeline if s.block is Block.WORK and s.end <= minute),
                    default=None)
    if shift_end is not None and minute - shift_end < AFTER_WORK_MIN:
        dip += AFTER_WORK_DIP
    if plan.event is DayEvent.LATE_SHIFT:
        dip += LATE_SHIFT_DIP
    return dip


def baseline(p: Persona, *, mood: float, arousal: float, plan: DayPlan, minute: int,
             toward_seed: float) -> State:
    """Where her feeling rests at her local `minute` of `plan`'s day: the night's mood (valence)
    and arousal (energy), her traits, her day's blocks and event, and last night's feelings
    about him (toward_seed)."""
    if not 0 <= minute < DAY_MIN:
        raise ValueError("minute must be 0-1439")
    event = plan.event
    stress = (p.big_five.neuroticism.score - TRAIT_MID) / TRAIT_SPAN + EVENT_STRESS.get(event, 0.0)
    if plan.block_at(minute) is Block.WORK:
        stress += WORK_STRESS
    return clamped(valence=mood + EVENT_VALENCE.get(event, 0.0),
                   energy=0.5 * arousal - _energy_dips(p, plan, minute),
                   stress=stress,
                   social=(p.big_five.extraversion.score - TRAIT_MID) / TRAIT_SPAN,
                   toward_him=toward_seed)


def decayed(stored: State, base: State, dt_s: float) -> State:
    """`stored` eased toward `base` over `dt_s` seconds (exponential, HALF_LIFE_S per axis)."""
    dt = max(0.0, float(dt_s))
    return clamped(**{axis: getattr(base, axis) + (getattr(stored, axis) - getattr(base, axis))
                      * 0.5 ** (dt / HALF_LIFE_S[axis]) for axis in AXES})


def apply(state: State, impulse: Impulse, scale: float = 1.0) -> State:
    deltas = IMPULSES[Impulse(impulse)]
    return clamped(**{axis: getattr(state, axis) + deltas.get(axis, 0.0) * scale
                      for axis in AXES})


_APOSTROPHES = str.maketrans({"\u2019": "'", "\u02bc": "'"})
HARSH_RE = re.compile(r"\b(?:stupid|idiot|useless|pathetic|shut up|wtf is wrong with you"
                      r"|are you kidding me|piss off|fuck off|so annoying|you suck)\b", re.I)
WARM_RE = re.compile(r"\b(?:thanks|thank you|ty|love (?:you|ya)|miss(?:ed)? you|proud of you"
                     r"|you'?re (?:the best|amazing|awesome|sweet|cute)|good job|nice one|gj"
                     r"|well done)\b", re.I)


def tone(text: str) -> Literal["warm", "harsh", "plain"]:
    """How his line reads: harsh (annoyed with someone, or a cutting word), else warm (a flirt,
    thanks or praise), else plain."""
    from ..talk.filters import flirty
    from ..talk.select import pushback_kind
    text = (text or "").translate(_APOSTROPHES)
    if pushback_kind(text) == "anger" or HARSH_RE.search(text):
        return "harsh"
    if flirty(text) or WARM_RE.search(text):
        return "warm"
    return "plain"


def _parts(state: State, to_him: bool, them: str) -> list[str]:
    parts = []
    if to_him and state.toward_him >= 0.3:
        parts.append(f"happy with {them} right now")
    if to_him and state.toward_him <= -0.3:
        parts.append(f"a bit annoyed with {them} right now")
    if state.energy <= -0.4:
        parts.append("tired")
    if state.stress >= 0.3 and state.social <= -0.2:
        parts.append("a bit short")
    if state.stress >= 0.4:
        parts.append("stressed")
    if state.valence >= 0.4:
        parts.append("in a great mood")
    elif state.valence >= 0.15:
        parts.append("in a good mood")
    elif state.valence <= -0.4:
        parts.append("in a bad mood")
    elif state.valence <= -0.15:
        parts.append("a bit off")
    if state.social <= -0.4:
        parts.append("not feeling chatty")
    elif state.social >= 0.4:
        parts.append("chatty")
    return parts[:3]


def words(state: State, *, to_him: bool, forms: tuple[str, str, str]) -> str:
    """Her state as one short line for her prompt ("You're tired and a bit short today."), at
    most three parts; "" when nothing stands out. Her feeling about him (forms: his subject,
    object and possessive pronouns) shows only when `to_him`: never on another member's turn."""
    parts = _parts(state, to_him, forms[1])
    if not parts:
        return ""
    joined = parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]
    when = "" if joined.endswith("right now") else " today"
    return f"You're {joined}{when}."


def lands(state: State, tone: str, *, agreeableness: int, forms: tuple[str, str, str]) -> str:
    """How his line (of `tone`) lands on her right now; "" when it just lands."""
    if tone == "harsh":
        return "it stings" if agreeableness >= 7 else "it annoys you"
    if tone == "warm":
        if state.toward_him <= -0.3:
            return f"nice, but you're still a bit sore with {forms[1]}"
        return "it makes you smile"
    if tone != "plain":
        return ""
    if state.stress >= 0.4:
        return "one more thing on a busy day"
    if state.energy <= -0.4:
        return "you're too tired to say much"
    if state.social <= -0.4:
        return "you're not really in the mood to chat"
    return ""
