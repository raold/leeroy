"""A reply's shape, drawn by code before generation (human-likeness spec section 4).

Before a member's line is generated, `draw_shape` picks what kind of message it is: an emoji
reaction, a word or two, a short line, a two- or three-message burst, a paragraph, a short line
asking something back, a late reply that says so, or nothing at all (left on read). The odds
come from the context's row (ROWS: owed or not, the channel family, the turn kind), then the
multipliers of her intent, her state, her attention and her typing profile, renormalized. An
owed answer (`ShapeContext.owed`) is never left on read, never a reaction and never a word or
two; a whisper or DM is never left on read either (leave on read is for channels only).
`shape_line` is the one length line the shape puts in the prompt ("" for LEAVE and REACTION);
`dm_words_for` mirrors his DM's length within the owner's `dm_words` cap by the `dm_mirror`
dial (0: a DM is exactly today's, a paragraph of up to `dm_words`).

Pure: no clock, no I/O, no logging. `rng` is the caller's seeded draw (life.seed), never
engine.rng. Nothing calls this until the pipeline wiring ([talk] shape, plan Task 9).

The context's `intent`, `attention` and `state` come from talk.intent.Intent,
life.presence.Attention and life.state.State, built in their own tasks. This module reads only
what it needs of them: an enum member's name in lower case (`_code`: "react", "ask", "vent",
"boundary", "logistics"; "glancing", "half") and a state's `energy` and `social` floats (-1..1;
a missing or non-numeric one reads 0). Any value with that shape works, including None."""
from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Literal

from leeroy_mind.talk.discord_length import CHANNEL_BANDS, band, burst_draw, num_predict
from leeroy_mind.talk.prompt import DISCORD_DM, DISCORD_LENGTH, DISCORD_WHERE, MORE_LINES
from leeroy_mind.talk.turns import TurnKind

if TYPE_CHECKING:
    from leeroy_mind.life.presence import Attention
    from leeroy_mind.life.state import State
    from leeroy_mind.personas.schema import Typing
    from leeroy_mind.talk.intent import Intent

LATE_SHAPE_S = 120.0  # a game answer this late since his line is LATE, not drawn
ONE_WORD_NUM_PREDICT = 24
DISCORD_SHORT_WORDS = CHANNEL_BANDS[1][1]  # a quick line on Discord (20 words)

Family = Literal["game", "whisper", "discord", "dm", "group"]
GAME_FAMILIES = frozenset({"game", "whisper"})
DISCORD_FAMILIES = frozenset({"discord", "dm", "group"})
PRIVATE_FAMILIES = frozenset({"whisper", "dm"})  # never left on read


class Shape(StrEnum):
    REACTION = "reaction"
    ONE_WORD = "one_word"
    SHORT = "short"
    BURST = "burst"
    PARAGRAPH = "paragraph"
    QUESTION = "question"
    LATE = "late"
    LEAVE = "leave"


# The draw's order: a fixed order keeps a seeded draw stable.
ORDER = (Shape.LEAVE, Shape.REACTION, Shape.ONE_WORD, Shape.SHORT, Shape.BURST, Shape.QUESTION,
         Shape.PARAGRAPH)


def _row(leave=0.0, reaction=0.0, one_word=0.0, short=0.0, burst=0.0, question=0.0,
         paragraph=0.0) -> dict[Shape, float]:
    return {Shape.LEAVE: leave, Shape.REACTION: reaction, Shape.ONE_WORD: one_word,
            Shape.SHORT: short, Shape.BURST: burst, Shape.QUESTION: question,
            Shape.PARAGRAPH: paragraph}


# Spec section 4's odds by context, before the multipliers. A paragraph needs Discord length (a
# Discord family with its words setting above 0), so OPEN's paragraph is Discord's only.
ROWS = {
    "owed_game": _row(short=.65, burst=.15, question=.20),
    "owed_discord": _row(short=.55, burst=.10, question=.15, paragraph=.20),
    "owed_dm": _row(short=.55, burst=.20, question=.25),
    "game": _row(leave=.25, one_word=.25, short=.35, burst=.07, question=.08),
    "channel": _row(leave=.20, reaction=.15, one_word=.12, short=.30, burst=.08, question=.05,
                    paragraph=.10),
    "open": _row(short=.60, burst=.15, question=.25, paragraph=.15),
    "flirt": _row(short=.60, burst=.25, question=.15),
    # A kind the table does not name (a greeting, gz, a raid call, a bit, an aside...): her one
    # line, as today.
    "line": _row(short=1.0),
}
_OWED_ROW = {"game": "owed_game", "whisper": "owed_game", "discord": "owed_discord",
             "group": "owed_discord", "dm": "owed_dm"}

LINE_ONE_WORD = "Just a word or two this time."
LINE_SHORT = "One short line, about {words} words."
LINE_SHORT_DM = ("This is your private Discord DM, not the game: a sentence or two (at most about "
                 "{words} words). No line breaks.")
LINE_BURST = "Say it as two quick lines, the way you'd double-text."
LINE_QUESTION = "Keep it short and ask {them} something back."
LINE_LATE = "You're only now seeing this (you were {excuse}): say so in a few words, then answer."
LINE_LATE_BARE = "You're only now seeing this: say so in a few words, then answer."
# The explicit whisper band (`two_lines`) keeps at least two lines: its short line and its
# question say so, in MORE_LINES' words, instead of "one short line" / "keep it short".
LINE_TWO_LINES = "Say it " + MORE_LINES + ", about {words} words."
LINE_TWO_LINES_QUESTION = "Say it " + MORE_LINES + ", and ask {them} something back."


@dataclass(frozen=True)
class ShapeContext:
    family: Family
    kind: TurnKind
    owed: bool  # an answer he is owed (LiveReview.owed / owes_him)
    intent: Intent | None
    typing: Typing
    attention: Attention | None
    state: State | None
    late_by: float  # seconds since his line, for an answer to him
    his_words: int  # his message's words, for DM mirroring
    discord_words: int  # [talk] discord_words
    dm_words: int  # [talk] dm_words
    dm_mirror: int  # [talk] dm_mirror, 0-10
    realism: bool  # [talk] realism: Discord bursts need it
    two_lines: bool  # the explicit whisper band: at least two lines


@dataclass(frozen=True)
class ShapeDraw:
    shape: Shape
    words: int
    lines: int
    num_predict: int | None  # None: the preset's own


def _code(value) -> str:
    """An Intent's or Attention's code: its enum member's name in lower case; "" for None."""
    if value is None:
        return ""
    name = getattr(value, "name", None)
    return (name if isinstance(name, str) else str(value)).lower()


def _level(state, attr: str) -> float:
    v = getattr(state, attr, 0.0) if state is not None else 0.0
    if isinstance(v, bool) or not isinstance(v, (int, float)) or math.isnan(v):
        return 0.0
    return float(v)


def _cap(ctx: ShapeContext) -> int:
    """The words setting a Discord family writes to; 0 for the game and for a setting of 0 (her
    one short line, as in game)."""
    if ctx.family == "dm":
        return max(ctx.dm_words, 0)
    if ctx.family in DISCORD_FAMILIES:
        return max(ctx.discord_words, 0)
    return 0


def _row_for(ctx: ShapeContext) -> dict[Shape, float]:
    if ctx.owed:
        return ROWS[_OWED_ROW[ctx.family]]
    if ctx.kind is TurnKind.OPEN:
        return ROWS["open"]
    if ctx.kind in (TurnKind.WHISPER, TurnKind.FLIRT):
        return ROWS["flirt"]
    if ctx.kind in (TurnKind.REPLY_HUMAN, TurnKind.CONTINUE):
        if ctx.family in PRIVATE_FAMILIES:  # leave on read is for channels only
            return ROWS[_OWED_ROW[ctx.family]]
        return ROWS["game" if ctx.family == "game" else "channel"]
    return ROWS["line"]


def weights(ctx: ShapeContext) -> dict[Shape, float]:
    """The context's row with every multiplier applied (not yet renormalized)."""
    w = dict(_row_for(ctx))

    def mul(factor: float, *shapes: Shape) -> None:
        for s in shapes:
            w[s] *= factor

    if _cap(ctx) <= 0:
        w[Shape.PARAGRAPH] = 0.0
    intent = _code(ctx.intent)
    if intent == "react":
        mul(3.0, Shape.ONE_WORD, Shape.REACTION)
    elif intent == "ask":
        mul(4.0, Shape.QUESTION)
    elif intent == "vent":
        mul(2.0, Shape.BURST)
        mul(1.5, Shape.PARAGRAPH)
    elif intent == "boundary":
        mul(2.0, Shape.SHORT)
    elif intent == "logistics":
        mul(1.5, Shape.QUESTION)
    if _level(ctx.state, "energy") <= -0.3:
        mul(1.5, Shape.ONE_WORD, Shape.REACTION)
        mul(0.5, Shape.BURST, Shape.PARAGRAPH)
    social = _level(ctx.state, "social")
    if social <= -0.3:
        mul(1.5, Shape.LEAVE)
    elif social >= 0.3:
        mul(1.5, Shape.BURST)
        mul(0.5, Shape.LEAVE)
    attention = _code(ctx.attention)
    if attention == "glancing":
        mul(0.3, Shape.PARAGRAPH)
        mul(0.5, Shape.BURST)
        mul(1.5, Shape.SHORT)
    elif attention == "half":
        mul(0.6, Shape.PARAGRAPH)
    mul(0.5 + ctx.typing.split_rate, Shape.BURST)
    if max(ctx.typing.words_per_line) <= 6:
        mul(1.5, Shape.ONE_WORD)
    if ctx.family in DISCORD_FAMILIES and not ctx.realism:
        w[Shape.BURST] = 0.0
    if ctx.family in GAME_FAMILIES:  # no reactions in game: a reaction is a word or two there
        w[Shape.ONE_WORD] += w[Shape.REACTION]
        w[Shape.REACTION] = 0.0
    return w


def _pick(w: dict[Shape, float], rng) -> Shape:
    total = sum(w[s] for s in ORDER)
    u = rng.random() * total
    acc, last = 0.0, Shape.SHORT
    for s in ORDER:
        if w[s] <= 0:
            continue
        acc += w[s]
        last = s
        if u < acc:
            return s
    return last


def _line_words(ctx: ShapeContext, rng) -> int:
    wpl = ctx.typing.words_per_line
    return rng.randint(min(wpl), max(wpl))


def _dm_words(ctx: ShapeContext, rng) -> int:
    return dm_words_for(ctx.his_words, ctx.typing, ctx.dm_mirror, ctx.dm_words, rng)


def _sized(shape: Shape, ctx: ShapeContext, rng) -> ShapeDraw:
    if shape in (Shape.LEAVE, Shape.REACTION):
        return ShapeDraw(shape, 0, 0, None)
    if shape is Shape.ONE_WORD:
        return ShapeDraw(shape, rng.randint(1, 3), 1, ONE_WORD_NUM_PREDICT)
    two = shape is Shape.BURST or (ctx.two_lines and shape in (Shape.SHORT, Shape.QUESTION))
    cap = _cap(ctx)
    if cap <= 0:  # the game, or Discord at a setting of 0: her own line, the preset's room
        words = _line_words(ctx, rng)
        return ShapeDraw(shape, 2 * words, 2, None) if two else ShapeDraw(shape, words, 1, None)
    dm = ctx.family == "dm"
    if two:
        words = _dm_words(ctx, rng) if dm else band(cap, burst_draw(rng))
        lines = rng.randint(2, 3) if shape is Shape.BURST else 2
        return ShapeDraw(shape, words, lines, num_predict(words))
    if dm:
        words = _dm_words(ctx, rng)
    elif shape is Shape.PARAGRAPH:
        words = cap
    else:
        words = min(cap, DISCORD_SHORT_WORDS)
    return ShapeDraw(shape, words, 1, num_predict(words))


def draw_shape(ctx: ShapeContext, rng) -> ShapeDraw:
    """The reply's shape, words, lines and num_predict for `ctx`, drawn with `rng`. A game or
    whisper answer LATE_SHAPE_S after his line is LATE (not drawn); a DM with `dm_mirror` 0 is
    today's DM (a paragraph of up to `dm_words`, not drawn)."""
    if ctx.family in GAME_FAMILIES and ctx.late_by >= LATE_SHAPE_S:
        return _sized(Shape.LATE, ctx, rng)
    if ctx.family == "dm" and ctx.dm_words > 0 and ctx.dm_mirror <= 0:
        return ShapeDraw(Shape.PARAGRAPH, ctx.dm_words, 1, num_predict(ctx.dm_words))
    return _sized(_pick(weights(ctx), rng), ctx, rng)


def shape_line(draw: ShapeDraw, ctx: ShapeContext, *, forms, excuse: str = "") -> str:
    """The length line `draw` puts in the prompt: "" for LEAVE and REACTION (nothing is
    generated). `forms` are the addressee's (they, them, their); `excuse` is where she was, for
    LATE ("at work"), left out when empty. A Discord paragraph keeps DISCORD_LENGTH's wording
    (DISCORD_DM's in a DM)."""
    shape, words = draw.shape, draw.words
    them = forms[1]
    discord = _cap(ctx) > 0
    if shape in (Shape.LEAVE, Shape.REACTION):
        return ""
    if shape is Shape.ONE_WORD:
        return LINE_ONE_WORD
    if shape is Shape.BURST:
        return LINE_BURST
    if shape is Shape.LATE:
        excuse = " ".join(excuse.split())
        return LINE_LATE.format(excuse=excuse) if excuse else LINE_LATE_BARE
    if shape is Shape.QUESTION:
        return (LINE_TWO_LINES_QUESTION if ctx.two_lines else LINE_QUESTION).format(them=them)
    if shape is Shape.PARAGRAPH and discord:
        if ctx.family == "dm":
            return DISCORD_DM.format(words=words)
        return DISCORD_LENGTH.format(where=DISCORD_WHERE["channel"], words=words)
    if ctx.two_lines:
        return LINE_TWO_LINES.format(words=words)
    if discord and ctx.family == "dm":
        return LINE_SHORT_DM.format(words=words)
    return LINE_SHORT.format(words=words)


def _clamp(x: float, lo: float, hi: float) -> float:
    """`x` within [lo, hi]; `hi` wins when lo > hi (a cap under the floor)."""
    return min(hi, max(lo, x))


def dm_words_for(his_words: int, typing: Typing, dm_mirror: int, dm_words: int, rng) -> int:
    """A DM's words: his message's length (mirrored) and her habit (her typing's longest line),
    as a distribution within the owner's `dm_words` cap, pulled from the cap toward that target
    by `dm_mirror` (0 returns `dm_words`: today's; 10 follows the target alone). A long message
    of his (40 words or more) gets the full cap a quarter of the time."""
    if dm_mirror <= 0 or dm_words <= 0:
        return dm_words
    m = min(dm_mirror, 10) / 10
    lo, hi = min(typing.words_per_line), max(typing.words_per_line)
    mirrored = _clamp(max(his_words, 0) * rng.uniform(0.7, 1.8), max(3, lo), dm_words)
    habit = _clamp(hi * rng.uniform(1.0, 3.0), 3, dm_words)
    target = math.sqrt(mirrored * habit)
    words = round(dm_words ** (1 - m) * target ** m)
    if his_words >= 40 and rng.random() < 0.25:
        words = dm_words
    return int(_clamp(words, 3, dm_words))
