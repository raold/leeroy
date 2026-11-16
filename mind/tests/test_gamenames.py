"""Bosses and battlegrounds a member names must exist, and her evening states only what the game
reported (2026-11-15: "a battleground in Tanaris", and an invented boss picked up by two more
officers). Invented boss names only."""
from types import SimpleNamespace

import pytest

from conftest import HUMAN, run, seed_test_cast
from engine_rig import engine_clock, make_rig, save_member
from leeroy_mind.protocol import BgEnd, Zone
from leeroy_mind.talk.gamenames import boss_words, misplaced_battleground, unknown_bosses
from leeroy_mind.talk.pipeline import GAME_NAME_HINT
from leeroy_mind.wow import bg_played

PLACES = frozenset({"tanaris", "crystalsong forest", "dragonblight", "warsong gulch",
                    "wintergrasp", "dalaran"})
LIBRARY = {"marrowgar", "festergut", "sindragosa", "putricide", "sapph", "valithria", "lord",
           "lapse", "trash"}


def known(words):
    return {w for w in words if w in LIBRARY}


@pytest.mark.parametrize("text", [
    "Just finished a battleground in Tanaris. My knees are killing me.",
    "hey i just got home from crystalsong forest battleground",
    "just lost a BG in dragonblight",
])
def test_a_battleground_put_in_a_zone(text):
    assert misplaced_battleground(text, PLACES)


@pytest.mark.parametrize("text", [
    "that battleground was a waste of time", "won a game of warsong gulch",
    "wintergrasp battle at 8", "back in dalaran after a bg", "tanaris is the vibe",
])
def test_battlegrounds_said_right(text):
    assert not misplaced_battleground(text, PLACES)


def test_no_places_no_flag():
    assert not misplaced_battleground("a battleground in tanaris", frozenset())


def test_boss_words_in_context():
    assert boss_words("we've been trying to parse zorbath since last week") == ["zorbath"]
    assert boss_words("if we get zorbath down i'll draw a diagram") == ["zorbath"]
    assert boss_words("zorbath is basically free") == ["zorbath"]
    assert boss_words("we wiped on Festergut's smell") == ["festergut"]


@pytest.mark.parametrize("text,unknown", [
    ("we've been trying to parse zorbath since last week", ["zorbath"]),
    ("one kill on quellmarr and zero wipes", ["quellmarr"]),
    ("we wiped on festergut again", []),
    ("finally killed sapph", []),
    ("kill sindy first", []),  # shorthand
    ("we wiped on Belra's pull", []),  # a member
    ("pull lapsed. 5 min", []),  # an everyday word the library spells one way
    ("we wiped on trash", []),
    ("killed everything", []),
])
def test_unknown_bosses(text, unknown):
    assert unknown_bosses(text, known, people=["Belra"]) == unknown


def test_the_battleground_she_played_is_named():
    assert bg_played(2) == "a game of Warsong Gulch"
    assert bg_played(4) == "an arena match"
    assert bg_played(32) == "a battleground"


@pytest.fixture
def clock():
    return engine_clock()


def test_tonight_so_far_names_the_battleground_in_order(store, clock, tmp_path):
    async def main():
        r = make_rig(store, clock, tmp_path)
        await r.event(Zone(ts=0, guid=101, zone_id=440, zone_name="Tanaris"))
        await r.advance(60)
        await r.event(BgEnd(ts=0, bg_type=2, winner="horde", members=(101,)))
        line = r.engine._lately_line(101, clock.now())
        assert line == "you were in Tanaris; then you played a game of Warsong Gulch"
        r.close()
    run(main())


def test_a_line_naming_an_unknown_boss_is_asked_again(store, clock, tmp_path):
    seed_test_cast(store)
    save_member(store, 102, player_gender="female", stance="drawn", typo_rate=0.0,
                split_rate=0.0, emoticon_rate=0.0, abbreviations={})
    r = make_rig(store, clock, tmp_path)
    r.engine.facts = SimpleNamespace(library=SimpleNamespace(known_words=known))
    try:
        r.script("we wiped on zorbath again", "rough night, we'll get it next time")
        r.whisper_from_him(102, "how was raid")
        r.run()
        assert len(r.chat_calls()) == 2
        assert GAME_NAME_HINT in r.last_user()
        assert [s.text for s in r.sent.says()] == ["rough night, we'll get it next time"]
    finally:
        r.close()
