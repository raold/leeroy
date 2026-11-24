"""A reply's shape and want (talk/shape.py, talk/intent.py) over generated contexts: every
channel family and turn kind, owed or not, her typing, voice, state and attention, and the
owner's [talk] length settings."""
import random

import pytest

pytest.importorskip("hypothesis", reason='Hypothesis comes with the test extra: pip install -e ".[test]"')

from hypothesis import given, strategies as st

from leeroy_mind.life.day import Block, DayEvent
from leeroy_mind.life.presence import Attention
from leeroy_mind.life.state import State
from leeroy_mind.personas.schema import Typing
from leeroy_mind.talk.intent import (BAD_NEWS_OFF, INTENT_KINDS, STANCE_FLIRT, TAIL_OFF, Intent,
                                     IntentContext, allowed, draw_intent)
from leeroy_mind.talk.shape import (MIN_LINE_WORDS, Shape, ShapeContext, dm_words_for, draw_shape,
                                    short_cap)
from leeroy_mind.talk.turns import TurnKind

FAMILIES = ("game", "whisper", "discord", "dm", "group")
CHANNEL_FAMILIES = {"game", "discord", "group"}  # where a line may be left on read
unit = st.floats(-1, 1)
states = st.one_of(st.none(), st.builds(State, unit, unit, unit, unit, unit))
word_range = st.tuples(st.integers(1, 60), st.integers(1, 60)).map(lambda r: tuple(sorted(r)))
typings = st.builds(
    Typing, case=st.sampled_from(("lower", "mixed", "proper")), end_punct=st.sampled_from(("none", "some", "full")),
    emoticons=st.just(()), emoticon_rate=st.just(0.0), abbreviations=st.just({}), typo_rate=st.just(0.0),
    words_per_line=word_range, split_rate=st.floats(0, 1), chars_per_sec=st.just(5.0))
LINES = ("", "how was work?", "hey", "night all", "wanna run icc at 8pm?", "you look cute today ;)",
         "my dad is in the hospital", "explain the lich king fight", "lol", "you around", "no, that's wrong")


@st.composite
def shape_contexts(draw):
    family = draw(st.sampled_from(FAMILIES))
    return ShapeContext(
        family=family, kind=draw(st.sampled_from(TurnKind)), owed=draw(st.booleans()),
        intent=draw(st.one_of(st.none(), st.sampled_from(Intent))), typing=draw(typings),
        attention=draw(st.one_of(st.none(), st.sampled_from(Attention))), state=draw(states),
        late_by=draw(st.sampled_from((0.0, 30.0, 119.9, 120.0, 900.0))), his_words=draw(st.integers(0, 200)),
        discord_words=draw(st.integers(0, 200)), dm_words=draw(st.integers(0, 200)),
        dm_mirror=draw(st.integers(0, 10)), realism=draw(st.booleans()),
        two_lines=family == "whisper" and draw(st.booleans()), away=draw(st.booleans()),
        content=draw(st.booleans()), waited=draw(st.booleans()), his_asks=draw(st.booleans()),
        voice_words=draw(st.one_of(st.none(), word_range)))


def cap_of(ctx):
    if ctx.family == "dm":
        return ctx.dm_words
    return ctx.discord_words if ctx.family in ("discord", "group") else 0


@given(shape_contexts(), st.integers(0, 2**32))
def test_a_drawn_shape_keeps_the_owed_and_channel_rules(ctx, seed):
    """For any context and seed:
    - the draw is a function of the context and the seed alone;
    - an answer he is owed is never left on read, a reaction or a word or two, and holds at
      least MIN_LINE_WORDS words unless the owner's own Discord words setting is smaller (the
      setting wins: it is the most a message may hold);
    - a whisper or a DM is never left on read, and the game has no reactions;
    - nothing is generated for LEAVE and REACTION, anything else is at least one word on at least
      one line; a paragraph needs a Discord words setting, and no Discord message passes it."""
    draw = draw_shape(ctx, random.Random(seed))
    assert draw_shape(ctx, random.Random(seed)) == draw
    cap = cap_of(ctx)
    if ctx.owed:
        assert draw.shape not in (Shape.LEAVE, Shape.REACTION, Shape.ONE_WORD)
        assert draw.words >= (MIN_LINE_WORDS if cap == 0 or cap >= MIN_LINE_WORDS else cap)
    if draw.shape is Shape.LEAVE:
        assert ctx.family in CHANNEL_FAMILIES and not ctx.owed
    if ctx.family in ("game", "whisper"):
        assert draw.shape is not Shape.REACTION
    if draw.shape in (Shape.LEAVE, Shape.REACTION):
        assert (draw.words, draw.lines) == (0, 0)
    else:
        assert draw.words >= 1 and draw.lines >= 1
    if draw.shape is Shape.PARAGRAPH:
        assert cap > 0
    if cap > 0:
        assert draw.words <= cap


@given(st.integers(-5, 400), typings, st.integers(0, 10), st.integers(0, 200), st.booleans(),
       st.one_of(st.none(), word_range), st.integers(0, 2**32))
def test_a_mirrored_dm_stays_within_the_owners_caps(his_words, typing, mirror, dm_words, asks, habit, seed):
    """dm_words_for: with the dial or the setting at 0 it is the setting itself; otherwise the
    words are at least 3 (or the whole setting when it is smaller) and never above the setting,
    and a short line of his caps her (short_cap) whatever the draw; seeded, it is the same
    every time."""
    words = dm_words_for(his_words, typing, mirror, dm_words, random.Random(seed), asks=asks, habit=habit)
    assert words == dm_words_for(his_words, typing, mirror, dm_words, random.Random(seed), asks=asks, habit=habit)
    if mirror <= 0 or dm_words <= 0:
        assert words == dm_words
        return
    assert min(3, dm_words) <= words <= dm_words
    if short_cap(his_words, asks):
        assert words <= short_cap(his_words, asks)


@st.composite
def intent_contexts(draw):
    return IntentContext(
        kind=draw(st.sampled_from(TurnKind)), to_him=draw(st.booleans()), owed=draw(st.booleans()),
        his_line=draw(st.sampled_from(LINES)), family=draw(st.sampled_from(FAMILIES)), female=draw(st.booleans()),
        stance=draw(st.sampled_from(("none", "fond", "drawn", "lust"))),
        style=draw(st.sampled_from((None, "bold", "shy", "guarded"))),
        attachment=draw(st.sampled_from(("secure", "anxious", "avoidant"))),
        humour=draw(st.sampled_from(("teasing", "dry", "warm"))), agreeableness=draw(st.integers(1, 10)),
        extraversion=draw(st.integers(1, 10)), tail=draw(st.booleans()),
        attention=draw(st.one_of(st.none(), st.sampled_from(Attention))), state=draw(states),
        event=draw(st.sampled_from(DayEvent)), block=draw(st.one_of(st.none(), st.sampled_from(Block))),
        kids=draw(st.booleans()), last=draw(st.one_of(st.none(), st.sampled_from(Intent))),
        forward=draw(st.floats(0, 2)), reassure=draw(st.booleans()), pushback=draw(st.booleans()),
        bad_news=draw(st.booleans()), scene=draw(st.booleans()), answer_first=draw(st.booleans()),
        hold=draw(st.one_of(st.none(), st.sampled_from(Intent))))


@given(intent_contexts(), st.integers(0, 2**32))
def test_a_drawn_want_is_one_the_turn_allows(ctx, seed):
    """For any context and seed:
    - the drawn want is one allowed() offers, and there is none exactly when nothing is allowed
      (every turn kind outside INTENT_KINDS);
    - seeded, the draw is the same every time, and a held want draws nothing from the rng;
    - an answer he is owed never "just reacts"; under his explicit overlay she never vents,
      gossips, deflects, draws a line, sorts out plans or keeps it light; on his bad news she
      neither teases nor flirts with him;
    - flirting and seeking reassurance only from a woman, to him, with an attitude toward him,
      never in a group DM; and flirting never at a forward dial of 0."""
    offered = allowed(ctx)
    rng = random.Random(seed)
    want = draw_intent(ctx, rng)
    after = rng.random()
    again = random.Random(seed)
    assert draw_intent(ctx, again) == want
    if want is None:
        assert offered == ()
    else:
        assert want in offered
    if ctx.kind not in INTENT_KINDS:
        assert want is None
    if offered == (want,) and ctx.hold is not None and want == ctx.hold:
        assert after == random.Random(seed).random()  # held: the rng is untouched
    if ctx.owed:
        assert Intent.REACT not in offered
    if ctx.tail:
        assert not TAIL_OFF & set(offered)
    if ctx.to_him and ctx.bad_news:
        assert not BAD_NEWS_OFF & set(offered)
    romance = ctx.female and ctx.to_him and ctx.stance in STANCE_FLIRT and ctx.family != "group"
    if not romance:
        assert Intent.FLIRT not in offered and Intent.REASSURE_SEEK not in offered
    if ctx.forward <= 0:
        assert Intent.FLIRT not in offered
