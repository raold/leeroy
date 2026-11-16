"""What she wants from a turn (human-likeness spec section 2; core plan Task 4).

One want enum for the whole mind: `Intent`, the twelve codes think-dm's beat (talk/beat.py, spec
section 7) asks the model to choose from on his whispers and DMs, with the words "You want to:"
reads (WANT_WORDS). Everywhere else the want is drawn by code: `weights` is a small table by
situation (his line's class, or the turn kind) times multipliers from her persona, her stance
toward him, her day and her feeling right now; `draw_intent` is one seeded weighted draw over it
and `topic_for` the code-built subject a want may carry ("about a rough day"), never invented. The
want reaches a prompt only through beat.render, the one appended block.

Rules the table keeps: flirting and seeking reassurance only from a woman, toward him, with an
attitude (never toward a member, never in a group DM), flirting scaled by her forward dial (0: no
flirt); an answer owed to him never "just reacts" (it adds something); under his explicit whisper
overlay (`tail`) she never vents, gossips, deflects, draws a line, sorts out plans or keeps it
light (the overlay wins: PM log 2026-11-16; beat.wants_for drops the same wants from the beat);
her feeling about him moves only her turns to him.

Her day comes from life.day (Block, DayEvent) and life.presence (Attention); her feeling from
life.state (State; plan Task 3, not built yet). An event or block is compared with its code (a
StrEnum, so a plain code string works too) and a state's five floats are read by name (a missing
one reads 0), so this module never needs life.state at run time.

Pure: no clock, no I/O, no logging. `rng` is the caller's seeded draw (life.seed), never
engine.rng. Nothing calls the draw on a live turn until the pipeline wiring ([talk] intent, plan
Task 8)."""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Literal

from leeroy_mind.life.day import Block, DayEvent
from leeroy_mind.talk.filters import flirty
from leeroy_mind.talk.questions import is_plan
from leeroy_mind.talk.select import classify_human_line
from leeroy_mind.talk.turns import TurnKind

if TYPE_CHECKING:
    import random

    from leeroy_mind.life.presence import Attention
    from leeroy_mind.life.state import State


class Intent(StrEnum):
    """Spec section 7's codes, in the beat's order (the schema's enum and the ask's list)."""
    VENT = "vent"
    SHARE = "share"
    ASK = "ask"
    TEASE = "tease"
    FLIRT = "flirt"
    REASSURE_SEEK = "reassure_seek"
    GOSSIP = "gossip"
    DEFLECT = "deflect"
    SMALL_TALK = "small_talk"
    REACT = "react"
    BOUNDARY = "boundary"
    LOGISTICS = "logistics"


# What "You want to:" reads for each (think-dm's words as merged, #329). {who} is the addressee:
# his called name on his turns, the member's name on a member's, "everyone" on an opener.
WANT_WORDS: dict[Intent, str] = {
    Intent.VENT: "get something off your chest",
    Intent.SHARE: "share something of your own",
    Intent.ASK: "get something from {who}",
    Intent.TEASE: "tease {who}",
    Intent.FLIRT: "flirt with {who}",
    Intent.REASSURE_SEEK: "hear that things are good between you",
    Intent.GOSSIP: "talk about someone else",
    Intent.DEFLECT: "steer away from it",
    Intent.SMALL_TALK: "keep it light",
    Intent.REACT: "just react",
    Intent.BOUNDARY: "draw a line",
    Intent.LOGISTICS: "sort out the plan",
}

INTENT_KINDS = frozenset({TurnKind.REPLY_HUMAN, TurnKind.CONTINUE, TurnKind.OPEN,
                          TurnKind.WHISPER, TurnKind.FLIRT})

Family = Literal["game", "whisper", "discord", "dm", "group"]



def want_words(intent: Intent, who: str, topic: str = "") -> str:
    """The words "You want to:" reads, then ` (<topic>)` when there is one."""
    words = WANT_WORDS[intent].format(who=who)
    return f"{words} ({topic})" if topic else words


@dataclass(frozen=True)
class IntentContext:
    kind: TurnKind
    to_him: bool  # the turn speaks to him (his line answered, or hers to him)
    owed: bool  # an answer owed to him (LiveReview.owed / owes_him)
    his_line: str  # the line of his it answers; "" when none
    family: Family
    female: bool
    stance: str  # romance.stance toward him: none, fond, drawn, lust
    style: str | None  # romance.style: bold, shy, guarded, ...
    attachment: str  # secure, anxious, avoidant
    humour: str
    agreeableness: int  # Big Five scores, 1-10
    extraversion: int
    tail: bool  # the owner's explicit whisper overlay applies this turn
    attention: Attention | None
    state: State | None  # None: neutral (every multiplier from it is 1)
    event: DayEvent | str  # the day's event; "none" when nothing happened
    block: Block | str | None  # where her day is now; None when unknown
    kids: bool
    last: Intent | None  # her want on her last turn in this conversation
    # her forward / flirty dial (tuning), as romance.forward gives it: 0-2, 1 as written. It
    # scales flirt, which only a turn to him can draw (review-332)
    forward: float = 1.0


def _row(vent=0.0, share=0.0, ask=0.0, tease=0.0, flirt=0.0, reassure_seek=0.0, gossip=0.0,
         deflect=0.0, small_talk=0.0, react=0.0, boundary=0.0,
         logistics=0.0) -> dict[Intent, float]:
    return dict(zip(Intent, (vent, share, ask, tease, flirt, reassure_seek, gossip, deflect,
                             small_talk, react, boundary, logistics)))


# Spec section 2's "small table": the base weights by situation, before the multipliers. His
# reply rows by select.classify_human_line (a goodbye reads as a greeting: a social formula),
# his flirt (filters.flirty) overriding the class.
ROWS: dict[str, dict[Intent, float]] = {
    "question": _row(.7, 3, 1, 1.5, 1, .3, .3, .7, .5, 1, .3, .5),
    "greeting": _row(1, 2, .5, 1, 1, .3, .3, .2, 3, 2, 0, .5),
    "remark": _row(1, 2, 1, 2, 1, .3, .5, .5, 1, 3, .3, .5),
    "flirt": _row(.3, 1, .5, 2, 4, 1, 0, 1, .5, 1, .5, 0),
    "continue": _row(1, 1.5, 1, 2, 0, 0, 1, .5, 1, 3, .3, .5),
    "open": _row(1.5, 3, 1.5, 0, 0, 0, 1, 0, 1, 0, 0, 0),
    "hers": _row(1, 2, 1, 1.5, 4, 1, 0, 0, 1, 0, 0, 0),  # WHISPER, FLIRT: hers to him
}
_LINE_ROW = {"question_all": "question", "greeting": "greeting", "goodbye": "greeting",
             "remark": "remark"}
STANCE_FLIRT = {"fond": 0.3, "drawn": 0.7, "lust": 1.0}
# Off under his explicit whisper overlay: it says not to put him off or make him wait.
TAIL_OFF = frozenset({Intent.VENT, Intent.GOSSIP, Intent.DEFLECT, Intent.BOUNDARY,
                      Intent.LOGISTICS, Intent.SMALL_TALK})
# A time of day in his line: a real hour ("8pm", "8:30 p.m.", "20:30") or "server time";
# never "lvl 80 pm me"
_TIME = re.compile(r"\b(?:1[0-2]|0?[1-9])(?::[0-5]\d)?\s*(?:[ap]m\b|[ap]\.m\.)"
                   r"|\b(?:[01]?\d|2[0-3]):[0-5]\d\b|\bserver time\b", re.IGNORECASE)


def _mood(ctx: IntentContext) -> tuple[float, float, float, float, float]:
    """Her state's valence, energy, stress, social and toward_him; 0 for each without one."""
    s = ctx.state

    def get(name: str) -> float:
        value = getattr(s, name, 0.0) if s is not None else 0.0
        number = isinstance(value, (int, float)) and not isinstance(value, bool)
        return float(value) if number else 0.0
    return (get("valence"), get("energy"), get("stress"), get("social"), get("toward_him"))


def _line_class(ctx: IntentContext) -> str:
    return classify_human_line(ctx.his_line) if ctx.his_line.strip() else ""


def _row_for(ctx: IntentContext) -> dict[Intent, float]:
    if ctx.kind == TurnKind.REPLY_HUMAN:
        if ctx.his_line and flirty(ctx.his_line):
            return ROWS["flirt"]
        return ROWS[_LINE_ROW.get(_line_class(ctx), "remark")]
    if ctx.kind == TurnKind.CONTINUE:
        return ROWS["continue"]
    if ctx.kind == TurnKind.OPEN:
        return ROWS["open"]
    return ROWS["hers"]


def _plans(line: str) -> bool:
    """His line names a plan or a time: talk.questions.is_plan, or a time like "8pm"."""
    return bool(line.strip()) and (bool(_TIME.search(line)) or is_plan(line))


def weights(ctx: IntentContext) -> dict[Intent, float]:
    """Each want's weight for this turn, every intent present (0 when it is off). A turn kind
    outside INTENT_KINDS wants nothing: every weight 0."""
    if ctx.kind not in INTENT_KINDS:
        return dict.fromkeys(Intent, 0.0)
    w = dict(_row_for(ctx))
    valence, energy, stress, social, toward = _mood(ctx)
    # romance: only a woman, toward him, with an attitude; never in a group DM
    attitude = ctx.female and ctx.to_him and ctx.stance in STANCE_FLIRT
    w[Intent.FLIRT] *= STANCE_FLIRT[ctx.stance] if attitude else 0.0
    w[Intent.FLIRT] *= max(0.0, ctx.forward)  # her forward dial: at 0 she draws no flirt
    if not attitude:
        w[Intent.REASSURE_SEEK] = 0.0
    elif ctx.attachment == "anxious":
        w[Intent.REASSURE_SEEK] *= 2
    if ctx.family == "group":
        w[Intent.FLIRT] = w[Intent.REASSURE_SEEK] = 0.0
    if ctx.tail:
        for i in TAIL_OFF:
            w[i] = 0.0
    if ctx.owed:
        w[Intent.REACT] = 0.0  # his rule: a reply that adds something
        if _line_class(ctx) == "question_all":
            w[Intent.DEFLECT] *= 0.3
            w[Intent.BOUNDARY] *= 0.3
            w[Intent.VENT] *= 0.5
    w[Intent.VENT] *= 1 + 2 * max(0.0, stress)
    if ctx.event in (DayEvent.BAD_DAY, DayEvent.SICK_KID):
        w[Intent.VENT] *= 2
    if ctx.block == Block.WORK:
        w[Intent.VENT] *= 1.5
    w[Intent.SHARE] *= 1 + max(0.0, valence)
    if ctx.event == DayEvent.NIGHT_OUT:
        w[Intent.SHARE] *= 1.5
    if ctx.humour == "teasing" or ctx.agreeableness <= 4:
        w[Intent.TEASE] *= 1.5
    if ctx.to_him and toward <= -0.3:
        w[Intent.TEASE] = 0.0
    w[Intent.BOUNDARY] *= 1 + max(0.0, stress)
    # her feeling about him moves only her turns to him, never how she treats others (review-332)
    if ctx.to_him and toward < 0:
        w[Intent.BOUNDARY] *= 1.5
    if ctx.to_him:
        w[Intent.DEFLECT] *= 1 + max(0.0, -toward)
    if ctx.style in ("shy", "guarded"):
        w[Intent.DEFLECT] *= 1.5
    if energy <= -0.3:
        w[Intent.SMALL_TALK] *= 2
        w[Intent.REACT] *= 2
    if social <= -0.3:
        w[Intent.REACT] *= 1.5
        w[Intent.BOUNDARY] *= 1.5
        w[Intent.SHARE] *= 0.6
        w[Intent.ASK] *= 0.6
    if _plans(ctx.his_line):
        w[Intent.LOGISTICS] *= 3
    if ctx.family in ("dm", "whisper") and not ctx.to_him:
        w[Intent.GOSSIP] = 0.0
    if ctx.last is not None and ctx.last in w:
        w[Intent(ctx.last)] *= 0.5
    return w


def allowed(ctx: IntentContext) -> tuple[Intent, ...]:
    """The wants this turn may have (weight above 0), in enum order: beat.wants_for generalised
    to every rule above."""
    w = weights(ctx)
    return tuple(i for i in Intent if w[i] > 0)


def draw_intent(ctx: IntentContext, rng: random.Random) -> Intent | None:
    """One weighted draw over `weights`, in enum order with one rng.random(); None when every
    weight is 0 (then nothing is drawn from `rng`)."""
    w = weights(ctx)
    total = sum(w.values())
    if total <= 0:
        return None
    x = rng.random() * total
    last = None
    for i in Intent:
        if w[i] <= 0:
            continue
        last = i
        x -= w[i]
        if x < 0:
            return i
    return last


def topic_for(intent: Intent, ctx: IntentContext) -> str:
    """The subject a want carries, built from her day ("" when it carries none): what she vents
    or shares about, gossip only from her own memories, which plan. An opener's own topic is
    its subject, so on OPEN this is always ""."""
    if ctx.kind == TurnKind.OPEN:
        return ""
    if intent == Intent.VENT:
        if ctx.event == DayEvent.BAD_DAY:
            return "about a rough day"
        if ctx.event == DayEvent.SICK_KID:
            return "about your kid being ill"
        if ctx.event == DayEvent.LATE_SHIFT:
            return "about being stuck late at work"
        if ctx.block in (Block.WORK, Block.COMMUTE):
            return "about work"
        return "about the kids" if ctx.kids else "about your day"
    if intent == Intent.SHARE:
        return "your night out" if ctx.event == DayEvent.NIGHT_OUT else "something from your day"
    if intent == Intent.GOSSIP:
        return "only from what you remember"
    if intent == Intent.LOGISTICS:
        return "raid times, gear or who's coming"
    return ""


__all__ = ["INTENT_KINDS", "Intent", "IntentContext", "ROWS", "TAIL_OFF", "WANT_WORDS", "allowed",
           "draw_intent", "topic_for", "want_words", "weights"]
