import pytest

from leeroy_mind.talk.movelog import MoveRow
from leeroy_mind.talk.moves import (MOVE_PHRASES, MOVES, MOVES_LINE_CHARS, classify, is_summon,
                                    main_move, moves_line, topics)

HE = ("he", "him", "his")


def row(main="tease", t=100.0, clause="", topics=(), moves=None):
    return MoveRow(ts=t, channel="whisper", to_guid=1, moves=frozenset(moves or {main}),
                   main=main, topics=tuple(topics), clause=clause, angle="")


@pytest.mark.parametrize("text,move", [
    ("then just get over here", "summon"), ("i've been waiting all night", "summon"),
    ("door's open, your move", "summon"), ("hurry up already", "summon"),
    ("i want you so bad", "declare"), ("you look good in that tier", "compliment"),
    ("bet you can't even pull that", "tease"), ("what did you do today?", "question"),
    ("remember arathi last week", "callback"), ("we should actually meet one day", "plan"),
    ("ugh my boss kept me late again", "complaint"), ("after raid ;)", "later"),
    ("hey handsome", "petname"), ("sent you 200g", "gift")])
def test_each_move_is_classified(text, move):
    assert move in classify(text)


@pytest.mark.parametrize("text", ["meet me in dalaran", "come duel me", "come to vent",
                                  "get over here we're queueing for the bg", "find me in shatt"])
def test_in_game_invitations_are_never_summons(text):
    assert "summon" not in classify(text) and "invite_game" in classify(text)
    assert not is_summon(text)


def test_main_move_prefers_the_summons_then_the_declaration():
    assert main_move(classify("i want you. get over here")) == "summon"
    assert main_move(classify("i want you")) == "declare" and main_move(frozenset()) == ""


def test_topics_skip_game_words_names_and_stopwords():
    assert topics("the table is up, arkon wiped the raid on the table", names={"arkon"}) == ("table",)


def test_typographic_apostrophes_fold():
    assert "summon" in classify("door’s open")


@pytest.mark.parametrize("text", [
    "come over after raid", "come over after the raid, i'm waiting", "hurry up, the raid starts",
    "find me after raid", "door's open, the bg is up", "I'm waiting, hurry up, the queue is long",
    "get over here, i have a group of tanks for you", "come over after this bg and hurry up"])
def test_a_game_word_as_a_time_or_aside_does_not_hide_a_summons(text):
    assert "summon" in classify(text) and "invite_game" not in classify(text)


@pytest.mark.parametrize("text", ["come to org with me", "join the raid", "inv me",
                                  "queue with me", "come help me with the quest",
                                  "meet me at the arena", "come to my group voice chat"])
def test_destinations_and_activities_are_invitations(text):
    assert "invite_game" in classify(text) and "summon" not in classify(text)


def test_right_now_is_not_a_later():
    assert "later" not in classify("i want you right now")


def test_lately_line_names_the_last_move_and_any_repeated_one():
    rows = [row(main="declare", t=100), row(main="declare", t=110), row(main="tease", t=120)]
    line = moves_line(rows, night_start=50, forms=HE)
    assert line == ("Tonight you've already told him you want him twice and teased him: "
                    "do something else this time.")


def test_lately_line_reads_lately_across_nights_and_is_empty_without_history():
    assert moves_line([row(main="tease", t=10)], night_start=50, forms=HE).startswith("Lately")
    assert moves_line([], night_start=0, forms=HE) == ""
    assert moves_line([row(main="")], night_start=0, forms=HE) == ""


def test_lately_line_never_quotes_her_words():
    line = moves_line([row(main="summon", clause="get over here")], night_start=0, forms=HE)
    assert line and "get over here" not in line


def test_lately_line_names_a_repeated_topic():
    rows = [row(main="tease", topics=("table",)), row(main="tease", topics=("table", "dress"))]
    line = moves_line(rows, night_start=0, forms=("she", "her", "hers"))
    assert "brought up the table" in line and "dress" not in line and "twice" in line


def test_every_move_has_a_phrase_and_the_line_stays_short():
    assert set(MOVE_PHRASES) == set(MOVES)
    rows = [row(main=m, topics=("table",), t=i) for i, m in enumerate(MOVES) for _ in (0, 1)]
    assert len(moves_line(rows, night_start=0, forms=HE)) <= MOVES_LINE_CHARS
    assert "{" not in moves_line(rows, night_start=0, forms=HE)
