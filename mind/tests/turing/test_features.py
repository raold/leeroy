"""Per-line features on invented lines."""
from datetime import timezone

import pytest

from leeroy_mind.turing.corpus import Line, name_pattern
from leeroy_mind.turing.features import COLUMNS, FEATURES, contexts, hour_of, table, text_traits


def traits(text, **kw):
    return text_traits(text, **kw)


@pytest.mark.parametrize("text,key,value", [
    ("i'm on my way", "apostrophe", 1.0), ("im on my way", "apostrophe", 0.0),
    ("im on my way", "bare", 1.0), ("i’m here", "apostrophe", 1.0),
    ("its fine", "bare", 0.0),  # "its" is a word of its own
    ("they were here", "bare", 0.0), ("dont care thats fine", "bare", 1.0),
    ("all lower here", "lower", 1.0), ("Some Caps", "lower", 0.0), ("123 !!", "lower", 0.0),
    ("Starting high", "capital", 1.0), ("starting low", "capital", 0.0),
    ("done.", "end_period", 1.0), ("done...", "end_period", 0.0), ("done...", "end_ellipsis", 1.0),
    ("done…", "end_ellipsis", 1.0), ("really?", "end_question", 1.0), ("go!", "end_exclaim", 1.0),
    ("call lapsed. next pull in 30", "inner_period", 1.0), ("wait... what", "inner_period", 0.0),
    ("yes, sure", "comma", 1.0), ("idk tbh", "abbrev", 1.0), ("i do not know", "abbrev", 0.0),
    ("lmao", "laugh", 1.0), ("hahaha nice", "laugh", 1.0), ("lol", "laugh", 1.0), ("hello", "laugh", 0.0),
    ("nice \U0001F602", "emoji", 1.0), ("nice :)", "emoticon", 1.0), ("<3 you", "emoticon", 1.0),
    ("see https://x.y", "emoticon", 0.0), ("xD", "emoticon", 1.0),
    ("ready?", "question", 1.0), ("how are u", "you", 1.0), ("we pull now", "you", 0.0),
])
def test_text_features(text, key, value):
    assert traits(text)[key] == value


def test_words_count_whitespace_words():
    assert traits("one two  three")["words"] == 3.0


def test_naming_someone_ignores_the_speakers_own_name():
    pattern = name_pattern(["Arkon", "Testmage"])
    assert traits("hey testmage you up", names=pattern, own="Arkon")["addressed"] == 1.0
    assert traits("arkon here", names=pattern, own="Arkon")["addressed"] == 0.0
    assert traits("arkons stuff", names=pattern, own="Bea")["addressed"] == 0.0  # whole words only
    assert traits("anyone there", names=pattern)["addressed"] == 0.0


def line(i, ts, speaker, conv="guild", human=False, text="hi"):
    return Line(f"c:{i}", ts, "game", conv, conv, speaker, human, text)


def test_latency_and_bursts_follow_each_conversation():
    lines = [line(1, 0, 1, human=True), line(2, 10, 101), line(3, 12, 101),
             line(4, 13, 102, conv="party"), line(5, 1000, 101)]
    ctx = contexts(lines)
    assert [c.latency for c in ctx] == [None, 10, 2, None, 988]
    assert [c.followup for c in ctx] == [False, False, True, False, False]  # 988 s is a new burst
    assert [c.burst for c in ctx] == [False, True, True, False, False]


def test_hour_reads_the_given_zone():
    assert hour_of(0, timezone.utc) == 0.0
    assert hour_of(3600 * 13 + 1800, timezone.utc) == 13.5


def test_table_columns_line_up_with_the_feature_list():
    lines = [line(1, 0, 1, human=True, text="Hello there."), line(2, 30, 101, text="hey u")]
    tab = table(lines, tz=timezone.utc)
    assert len(tab.columns) == len(COLUMNS) == len(FEATURES) + 1  # the hour is two columns
    assert all(len(c) == 2 for c in tab.columns)
    assert tab.raw["latency"] == [None, 30]
    assert tab.raw["words"] == [2.0, 2.0]
