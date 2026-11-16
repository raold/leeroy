# tests/life/test_shape.py
import json
import random, statistics
from collections import Counter
from dataclasses import dataclass, replace
from enum import StrEnum

from leeroy_mind.life.presence import Attention
from leeroy_mind.personas.schema import Typing
from leeroy_mind.talk.shape import Shape, ShapeContext, draw_shape, dm_words_for, shape_line
from leeroy_mind.talk.turns import TurnKind

T = Typing(case="lower", end_punct="none", emoticons=(), emoticon_rate=0.0, abbreviations={},
           typo_rate=0.0, words_per_line=(3, 8), split_rate=0.3, chars_per_sec=5.0)
OWED = ShapeContext(family="whisper", kind=TurnKind.REPLY_HUMAN, owed=True, intent=None, typing=T,
                    attention=None, state=None, late_by=0.0, his_words=12, discord_words=90,
                    dm_words=200, dm_mirror=5, realism=True, two_lines=False)

def test_owed_is_never_left_reacted_or_one_word():   # Review Focus 4
    for fam in ("game", "whisper", "discord", "dm", "group"):
        got = {draw_shape(replace(OWED, family=fam), random.Random(i)).shape for i in range(500)}
        assert not got & {Shape.LEAVE, Shape.REACTION, Shape.ONE_WORD}

def test_non_owed_channel_can_leave_or_react():
    ctx = replace(OWED, family="discord", owed=False)
    got = {draw_shape(ctx, random.Random(i)).shape for i in range(500)}
    assert {Shape.LEAVE, Shape.REACTION} <= got

def test_late_is_forced_in_game():
    assert draw_shape(replace(OWED, late_by=300.0), random.Random(1)).shape is Shape.LATE

def test_dm_mirror_dial():
    rng = random.Random(3)
    assert dm_words_for(28, T, 0, 200, rng) == 200
    tight = [dm_words_for(10, T, 10, 200, random.Random(i)) for i in range(1000)]
    assert sorted(tight)[950] <= 30
    mid = statistics.median(dm_words_for(28, T, 5, 200, random.Random(i)) for i in range(1000))
    assert 40 <= mid <= 110

def test_one_word_draws_are_small():
    ctx = replace(OWED, owed=False, family="game", kind=TurnKind.CONTINUE)
    ones = [d for d in (draw_shape(ctx, random.Random(i)) for i in range(300)) if d.shape is Shape.ONE_WORD]
    assert ones and all((d.num_predict, d.lines) == (24, 1) and 1 <= d.words <= 3 for d in ones)

def test_late_line_copy():
    from leeroy_mind.talk.shape import ShapeDraw
    line = shape_line(ShapeDraw(Shape.LATE, 6, 1, None), OWED, forms=("he", "him", "his"), excuse="at work")
    assert line == "You're only now seeing this (you were at work): say so in a few words, then answer."


# Stand-ins for Task 3's State and Task 4's Intent (Task 1's Attention is the real one, #330):
# talk.shape reads them by member name (an enum's `.name`, lower case) and by the `energy` /
# `social` attributes only.
class Intent(StrEnum):
    VENT = "vent"
    ASK = "ask"
    REACT = "react"
    BOUNDARY = "boundary"
    LOGISTICS = "logistics"
    SMALL_TALK = "small_talk"


@dataclass(frozen=True)
class State:
    energy: float = 0.0
    social: float = 0.0


def _shares(ctx, n=2000):
    got = Counter(draw_shape(ctx, random.Random(i)).shape for i in range(n))
    return {s: got[s] / n for s in Shape}


def test_owed_game_matches_its_row():
    s = _shares(replace(OWED, family="game"), 4000)
    assert abs(s[Shape.SHORT] - 0.65 / 0.97) < 0.04  # burst x(0.5 + 0.3): .15 -> .12
    assert abs(s[Shape.QUESTION] - 0.20 / 0.97) < 0.03
    assert s[Shape.PARAGRAPH] == 0


def test_intent_multipliers():
    base = _shares(OWED)[Shape.QUESTION]
    assert _shares(replace(OWED, intent=Intent.ASK))[Shape.QUESTION] > 2 * base
    react = replace(OWED, family="discord", owed=False, intent=Intent.REACT)
    plain = replace(react, intent=Intent.SMALL_TALK)
    assert _shares(react)[Shape.REACTION] > 1.5 * _shares(plain)[Shape.REACTION]
    vent = replace(OWED, family="discord", intent=Intent.VENT)
    assert _shares(vent)[Shape.PARAGRAPH] > _shares(replace(vent, intent=None))[Shape.PARAGRAPH]


def test_a_plain_want_code_counts_as_its_intent():
    from leeroy_mind.talk.beat import WANTS  # think-dm's want codes (#329)
    assert {"react", "ask", "vent", "boundary", "logistics"} <= set(WANTS)
    assert _shares(replace(OWED, intent="ask")) == _shares(replace(OWED, intent=Intent.ASK))


def test_attention_and_state_multipliers():
    ctx = replace(OWED, family="discord")
    glancing = _shares(replace(ctx, attention=Attention.GLANCING))
    full = _shares(replace(ctx, attention=Attention.FULL))
    half = _shares(replace(ctx, attention=Attention.HALF))
    none = _shares(replace(ctx, attention=Attention.NONE))
    assert glancing[Shape.PARAGRAPH] < 0.5 * full[Shape.PARAGRAPH]
    assert glancing[Shape.SHORT] > full[Shape.SHORT]
    assert half[Shape.PARAGRAPH] < 0.8 * full[Shape.PARAGRAPH]
    assert none == full  # only glancing and half move the odds
    chan = replace(ctx, owed=False)
    tired = _shares(replace(chan, state=State(energy=-0.5)))
    rested = _shares(replace(chan, state=State()))
    assert tired[Shape.ONE_WORD] > rested[Shape.ONE_WORD]
    assert tired[Shape.PARAGRAPH] < rested[Shape.PARAGRAPH]
    withdrawn = _shares(replace(chan, state=State(social=-0.5)))
    assert withdrawn[Shape.LEAVE] > rested[Shape.LEAVE]


def test_reaction_in_game_becomes_one_word():
    ctx = replace(OWED, family="game", owed=False, kind=TurnKind.CONTINUE, intent=Intent.REACT)
    assert all(draw_shape(ctx, random.Random(i)).shape is not Shape.REACTION for i in range(500))


def test_discord_burst_needs_realism():
    for fam in ("discord", "dm", "group"):
        ctx = replace(OWED, family=fam, realism=False)
        assert all(draw_shape(ctx, random.Random(i)).shape is not Shape.BURST for i in range(500))
    assert Shape.BURST in {draw_shape(replace(OWED, realism=False), random.Random(i)).shape
                           for i in range(500)}  # the game's two lines need no realism


def test_private_turns_are_never_left_on_read():
    for fam in ("whisper", "dm"):
        ctx = replace(OWED, family=fam, owed=False, kind=TurnKind.CONTINUE)
        got = {draw_shape(ctx, random.Random(i)).shape for i in range(500)}
        assert not got & {Shape.LEAVE, Shape.REACTION, Shape.ONE_WORD}


def test_open_and_flirt_rows():
    opener = {draw_shape(replace(OWED, owed=False, kind=TurnKind.OPEN, family=fam),
                         random.Random(i)).shape for fam in ("game", "discord") for i in range(500)}
    assert not opener & {Shape.LEAVE, Shape.REACTION, Shape.ONE_WORD}
    game_open = {draw_shape(replace(OWED, owed=False, kind=TurnKind.OPEN, family="game"),
                            random.Random(i)).shape for i in range(500)}
    assert Shape.PARAGRAPH not in game_open
    flirt = {draw_shape(replace(OWED, owed=False, kind=TurnKind.FLIRT), random.Random(i)).shape
             for i in range(500)}
    assert flirt == {Shape.SHORT, Shape.BURST, Shape.QUESTION}


def test_other_kinds_keep_one_short_line():
    for kind in (TurnKind.GZ, TurnKind.CALLOUT, TurnKind.GREET):
        ctx = replace(OWED, family="game", owed=False, kind=kind)
        assert {draw_shape(ctx, random.Random(i)).shape for i in range(200)} == {Shape.SHORT}


def test_words_lines_and_num_predict():
    from leeroy_mind.talk.discord_length import num_predict
    for i in range(200):
        d = draw_shape(OWED, random.Random(i))
        if d.shape in (Shape.SHORT, Shape.QUESTION):
            assert d.lines == 1 and 3 <= d.words <= 8 and d.num_predict is None
        if d.shape is Shape.BURST:
            assert d.lines == 2 and 6 <= d.words <= 16 and d.num_predict is None
        c = draw_shape(replace(OWED, family="discord"), random.Random(i))
        if c.shape in (Shape.SHORT, Shape.QUESTION):
            assert (c.words, c.lines, c.num_predict) == (20, 1, num_predict(20))
        if c.shape is Shape.PARAGRAPH:
            assert (c.words, c.lines, c.num_predict) == (90, 1, num_predict(90))
        if c.shape is Shape.BURST:
            assert 2 <= c.lines <= 3 and c.words == 45
        x = draw_shape(replace(OWED, family="discord", owed=False), random.Random(i))
        if x.shape in (Shape.LEAVE, Shape.REACTION):
            assert (x.words, x.lines, x.num_predict) == (0, 0, None)


def test_two_lines_keeps_two_lines():
    ctx = replace(OWED, two_lines=True)
    for i in range(300):
        d = draw_shape(ctx, random.Random(i))
        assert d.lines == 2 and 6 <= d.words <= 16


def test_two_lines_keeps_two_lines_when_late():   # review #331 P2-1
    ctx = replace(OWED, two_lines=True, late_by=300.0)
    for i in range(100):
        d = draw_shape(ctx, random.Random(i))
        assert d.shape is Shape.LATE and d.lines == 2 and 6 <= d.words <= 16
    assert draw_shape(replace(ctx, two_lines=False), random.Random(1)).lines == 1


def test_dm_mirror_zero_is_todays_dm():
    from leeroy_mind.talk.discord_length import num_predict
    from leeroy_mind.talk.prompt import DISCORD_DM
    for owed, kind in ((True, TurnKind.REPLY_HUMAN), (False, TurnKind.OPEN)):
        ctx = replace(OWED, family="dm", dm_mirror=0, owed=owed, kind=kind)
        for i in range(50):
            d = draw_shape(ctx, random.Random(i))
            assert (d.shape, d.words, d.lines, d.num_predict) == (Shape.PARAGRAPH, 200, 1, num_predict(200))
            assert shape_line(d, ctx, forms=("he", "him", "his")) == DISCORD_DM.format(words=200)


def test_dm_lengths_follow_the_mirror():
    ctx = replace(OWED, family="dm", his_words=10, dm_mirror=10)
    words = [draw_shape(ctx, random.Random(i)).words for i in range(500)]
    assert max(words) <= 50 and min(words) >= 3


def test_dm_mirror_follows_his_length_with_a_spread():   # review #331 ruling
    def spread(his):
        w = sorted(dm_words_for(his, T, 5, 200, random.Random(i)) for i in range(2000))
        return w[200], w[1000], w[1800]
    short, mid, long = spread(3), spread(28), spread(120)
    assert 25 <= short[1] <= 42 and 60 <= mid[1] <= 90 and 120 <= long[1] <= 160
    assert mid[2] / mid[0] >= 1.4  # a distribution for one message, not one length
    assert dm_words_for(28, T, 0, 200, random.Random(1)) == 200


def test_zero_setting_is_one_short_line():
    for fam, ctx in (("discord", replace(OWED, family="discord", discord_words=0)),
                     ("dm", replace(OWED, family="dm", dm_words=0))):
        for i in range(200):
            d = draw_shape(ctx, random.Random(i))
            assert d.shape is not Shape.PARAGRAPH, fam
            assert d.num_predict is None and d.words <= 16, fam


def test_draws_are_deterministic():
    ctx = replace(OWED, family="discord", owed=False)
    assert ([draw_shape(ctx, random.Random(7)) for _ in range(3)]
            == [draw_shape(ctx, random.Random(7)) for _ in range(3)])


def test_shape_lines():
    from leeroy_mind.talk.prompt import DISCORD_LENGTH
    from leeroy_mind.talk.shape import ShapeDraw
    him = ("he", "him", "his")
    her = ("she", "her", "her")
    dm = replace(OWED, family="dm")
    chan = replace(OWED, family="discord")
    assert shape_line(ShapeDraw(Shape.ONE_WORD, 2, 1, 24), OWED, forms=him) == "Just a word or two this time."
    assert shape_line(ShapeDraw(Shape.SHORT, 7, 1, None), OWED, forms=him) == "One short line, about 7 words."
    assert shape_line(ShapeDraw(Shape.SHORT, 40, 1, 88), dm, forms=him) == (
        "This is your private Discord DM, not the game: a sentence or two (at most about 40 "
        "words). No line breaks.")
    assert shape_line(ShapeDraw(Shape.BURST, 12, 2, None), OWED, forms=him) == (
        "Say it as two quick lines, the way you'd double-text.")
    assert shape_line(ShapeDraw(Shape.BURST, 45, 3, 96), chan, forms=him) == (
        "Say it as two or three quick lines, the way you'd double-text.")
    assert shape_line(ShapeDraw(Shape.BURST, 60, 2, 120), dm, forms=him) == (
        "Say it as two or three quick lines, the way you'd double-text.")
    assert shape_line(ShapeDraw(Shape.QUESTION, 7, 1, None), OWED, forms=her) == (
        "Keep it short and ask her something back.")
    assert shape_line(ShapeDraw(Shape.LATE, 6, 1, None), OWED, forms=him) == (
        "You're only now seeing this: say so in a few words, then answer.")
    assert shape_line(ShapeDraw(Shape.PARAGRAPH, 90, 1, 168), chan, forms=him) == (
        DISCORD_LENGTH.format(where="Discord chat", words=90))
    for shape in (Shape.LEAVE, Shape.REACTION):
        assert shape_line(ShapeDraw(shape, 0, 0, None), chan, forms=him) == ""


def test_two_lines_copy_says_a_line_or_two():
    from leeroy_mind.talk.shape import ShapeDraw
    ctx = replace(OWED, two_lines=True)
    line = shape_line(ShapeDraw(Shape.SHORT, 12, 2, None), ctx, forms=("he", "him", "his"))
    assert line == "Say it in a line or two, more than your usual one line, about 12 words."
    assert "One short line" not in line
    ask = shape_line(ShapeDraw(Shape.QUESTION, 12, 2, None), ctx, forms=("she", "her", "her"))
    assert ask == "Say it in a line or two, more than your usual one line, and ask her something back."


def test_shapes_cli_prints_numbers_only(store, tmp_path, capsys):
    from conftest import seed_test_cast
    from leeroy_mind.life.cli import life_main
    seed_test_cast(store)
    rc = life_main(["shapes", "--data-dir", str(tmp_path), "--guid", "101", "--family", "dm",
                    "--his-words", "28", "--owed", "--samples", "300"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["guid"] == 101 and out["samples"] == 300 and out["dm_mirror"] == 5
    assert abs(sum(out["shares"].values()) - 1.0) < 1e-3
    assert out["shares"]["leave"] == 0 and out["shares"]["reaction"] == 0
    assert 3 <= out["words"]["p10"] <= out["words"]["p50"] <= out["words"]["p90"] <= 200
    assert life_main(["shapes", "--data-dir", str(tmp_path), "--guid", "999", "--family", "dm",
                      "--his-words", "28"]) == 1


def test_life_is_a_main_subcommand(store, tmp_path, capsys):
    from conftest import seed_test_cast
    from leeroy_mind.__main__ import main
    seed_test_cast(store)
    assert main(["life", "shapes", "--data-dir", str(tmp_path), "--guid", "102", "--family",
                 "game", "--kind", "continue", "--samples", "200"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["shares"]["reaction"] == 0 and out["shares"]["paragraph"] == 0
