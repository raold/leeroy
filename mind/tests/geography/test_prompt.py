"""Geography in her live prompts (talk/pipeline.py): her home in her own card, her "where you
live, right now" line on her private and Discord turns, his [owner] home in her private turns
with him only, and the guards. Invented homes and lines only."""
import asyncio
import logging
from dataclasses import replace
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from conftest import TEST_CAST, seed_test_cast
from discord.test_dm_prompt import answer, answers, post, talk_for
from engine_rig import make_rig, numbered, save_member
from leeroy_mind.geography import config as geo_config
from leeroy_mind.personas.io import load_persona, save_persona
from leeroy_mind.talk.pipeline import NEVER_MET
from leeroy_mind.talk.prompt import PromptParts, render_messages, trim_to_budget
from leeroy_mind.testing.fake_clock import FakeClock
from leeroy_mind.text import estimate_tokens
from leeroy_mind.wow import CLASSES, RACES
from test_sofar import his_dm, section
from leeroy_mind.talk.prompt import SO_FAR_HEAD

HOMES = {101: "Idaho", 102: "Nevada", 103: "Utah", 104: "Kansas", 105: "Oregon"}


@pytest.fixture
def clock():
    return FakeClock(datetime(2026, 10, 29, 20, tzinfo=ZoneInfo("America/New_York")), yields=64)


def rig_with_homes(store, clock, tmp_path, homes=HOMES, **kw):
    seed_test_cast(store)
    for g, (_, race, cls) in TEST_CAST.items():
        save_member(store, g, region="US", accent="en-us")
        if g in homes:
            p = load_persona(store, g)
            save_persona(store, replace(p, identity=replace(p.identity, home=homes[g])),
                         source="seed", ts=1000.0, race=RACES[race], class_name=CLASSES[cls])
    rig = make_rig(store, clock, tmp_path, **kw)
    rig.world.human_online = False
    return rig


def own_home():
    return geo_config.parse_owner({"home": "Montana", "home_detail": "Pinecrest"})


def whisper_prompt(rig, text="how was your day", guid=102):
    async def main():
        rig.world.human_online = True
        await rig.session()
        await rig.advance(120)
        before = len(rig.prompts())
        await rig.human(text, channel="whisper", to_guid=guid)
        await rig.advance(90)
        return rig.prompts()[before]
    return asyncio.run(main())


def test_her_whisper_carries_her_home_and_place_and_nobody_elses(store, clock, tmp_path):
    rig = rig_with_homes(store, clock, tmp_path, script=numbered("mm {n} then"))
    prompt = whisper_prompt(rig)
    assert "You live in Nevada." in prompt
    assert "Where you live: Nevada, " in prompt
    assert prompt.index("Where you live: Nevada") < prompt.index("Right now:")
    assert "Late October there: highs near " in prompt
    for g, home in HOMES.items():
        if g != 102:
            assert home not in prompt  # another member's home never reaches her prompt
    assert "Montana" not in prompt  # no [owner] home set: nothing of his
    rig.close()


def test_her_guild_line_has_her_home_but_no_place_line(store, clock, tmp_path):
    async def main():
        rig = rig_with_homes(store, clock, tmp_path, reply="sounds good")
        rig.world.human_online = True
        await rig.session()
        await rig.advance(120)
        before = len(rig.prompts())
        await rig.human("anyone up for a run later?")
        await rig.advance(90)
        prompts = rig.prompts()[before:]
        assert prompts and all("You live in " in p for p in prompts)
        assert not any("Where you live:" in p for p in prompts)
        rig.close()
    asyncio.run(main())


def test_his_home_reaches_her_private_turns_with_him_only(store, clock, tmp_path):
    geo_config.pin(True, own_home())
    rig = rig_with_homes(store, clock, tmp_path, script=numbered("mm {n} then"))
    talk = talk_for(rig)
    mid = his_dm(rig, "so where are you based again")
    answer(rig, talk, mid)
    far = section(rig.prompts()[-1], SO_FAR_HEAD).splitlines()
    assert far[0] == "- " + NEVER_MET.format(name=rig.engine.human.called)
    assert far[1].startswith(f"- {rig.engine.human.called} lives in Pinecrest, Montana. "
                             "From you that's about ")
    assert "clock is 1 hour ahead of yours." in far[1]  # Nevada (Pacific) to Montana (Mountain)
    assert far[1].endswith("Meeting up is only ever a what-if between you.")
    # a Discord channel turn: her own place, never his
    mid = post(rig, "general", "@Arkon you around tonight?")
    answer(rig, talk, mid)
    channel = rig.prompts()[-1]
    assert "Where you live: Idaho, " in channel
    assert "Montana" not in channel and "Pinecrest" not in channel
    rig.close()


def test_his_home_is_never_in_a_public_game_prompt(store, clock, tmp_path):
    geo_config.pin(True, own_home())

    async def main():
        rig = rig_with_homes(store, clock, tmp_path, reply="sounds good")
        rig.world.human_online = True
        await rig.session()
        await rig.advance(120)
        await rig.human("anyone up for a run later?")
        await rig.advance(90)
        assert rig.prompts()
        assert not any("Montana" in p or "Pinecrest" in p for p in rig.prompts())
        rig.close()
    asyncio.run(main())


def test_whisper_to_him_gets_his_home_too(store, clock, tmp_path):
    geo_config.pin(True, own_home())
    rig = rig_with_homes(store, clock, tmp_path, script=numbered("mm {n} then"))
    prompt = whisper_prompt(rig)
    assert f"{rig.engine.human.called} lives in Pinecrest, Montana." in section(prompt,
                                                                               SO_FAR_HEAD)
    rig.close()


def test_geography_off_is_todays_prompt(store, clock, tmp_path):
    geo_config.pin(False, own_home())
    rig = rig_with_homes(store, clock, tmp_path, script=numbered("mm {n} then"))
    prompt = whisper_prompt(rig)
    assert "You live in" not in prompt and "Where you live:" not in prompt
    assert "Montana" not in prompt and "Nevada" not in prompt
    rig.close()


def test_a_us_member_without_a_home_has_no_line(store, clock, tmp_path):
    rig = rig_with_homes(store, clock, tmp_path, homes={}, script=numbered("mm {n} then"))
    prompt = whisper_prompt(rig)
    assert "You live in" not in prompt and "Where you live:" not in prompt
    rig.close()


def test_a_slip_about_her_own_place_is_asked_again(store, clock, tmp_path, caplog):
    caplog.set_level(logging.INFO)
    rig = rig_with_homes(store, clock, tmp_path, homes={102: "Florida"})
    rig.script("omg its snowing here, the yard is white", "warm muggy night here, how was yours")
    talk = talk_for(rig)
    mid = his_dm(rig, "how is it over there tonight")
    answer(rig, talk, mid)
    got = answers(rig, "dm:102", mid)
    assert [m["text"] for m in got] == ["warm muggy night here, how was yours"]
    retry = rig.chat_calls()[1]["messages"][-1]["content"]
    assert "Keep to where you live: Florida, " in retry
    assert "say_tells: guid=102" in caplog.text and "place" in caplog.text
    rig.close()


def test_his_home_said_in_public_is_never_sent(store, clock, tmp_path, caplog):
    caplog.set_level(logging.INFO)
    geo_config.pin(True, own_home())

    async def main():
        rig = rig_with_homes(store, clock, tmp_path)
        rig.world.human_online = True
        await rig.session()
        await rig.advance(120)
        rig.script("you're in montana right? must be cold", "you're in montana right, brr",
                   "you're in montana yeah?", "hey, whats up")
        await rig.human("hey arkon")
        await rig.advance(120)
        said = [s.text for s in rig.sent.says() if s.channel == "guild"]
        assert said and not any("montana" in x.lower() for x in said)
        assert "tells=his place" in caplog.text
        assert "say_owed_fallback_refused" in caplog.text  # the fallback is screened too
        assert any("don't say where" in c["messages"][-1]["content"] for c in rig.chat_calls())
        assert "montana" not in caplog.text.lower()
        rig.close()
    asyncio.run(main())


def _parts(place, chat=20):
    return PromptParts(card="card", samples=[], rules="rules", rel_cards=[], memories=[],
                       mood="It's Thursday evening.",
                       chat=[f"[1m ago] Arkon: line {i} " + "word " * 30 for i in range(chat)],
                       own_recent=[], instruction="Answer him.", place=place)


def test_the_place_line_is_the_first_thing_the_trim_lets_go():
    place = ["Where you live: Utah, red rock.", "Late October there: mild.", "It's dark out now."]
    full = _parts(place)

    def size(parts):
        return sum(estimate_tokens(m["content"]) for m in render_messages(parts))
    one = trim_to_budget(full, budget=size(full) - 1)
    assert one.place == place[:2] and len(one.chat) == 20  # its last part first, chat untouched
    none = trim_to_budget(full, budget=size(replace(full, place=[])))
    assert none.place == [] and len(none.chat) == 20


def test_the_place_line_sits_just_before_right_now_in_both_modes():
    for situation in (False, True):
        parts = replace(_parts(["Where you live: Utah, red rock."], chat=1), situation=situation)
        user = render_messages(parts)[1]["content"]
        assert "Where you live: Utah, red rock.\n\nRight now:" in user


def test_only_her_own_live_card_carries_her_home():
    from conftest import make_persona
    from leeroy_mind.geography.lines import home_phrase
    from leeroy_mind.geography.places import place_of
    from leeroy_mind.personas.card import render_night_card, render_prompt_card
    p = make_persona(101, "Arkon", region="US", accent="en-us")
    p = replace(p, identity=replace(p.identity, home="Wyoming"))
    plain = render_prompt_card(p, race="Human", class_name="Priest")
    assert "Wyoming" not in plain
    assert "Wyoming" not in render_night_card(p, race="Human", class_name="Priest")
    home = render_prompt_card(p, race="Human", class_name="Priest",
                              home=home_phrase(place_of(p)))
    assert "(he/him). You live in Wyoming. You play Arkon" in home
    assert home.replace(" You live in Wyoming.", "", 1) == plain  # nothing else moves


def test_tiers():
    from leeroy_mind.talk.pipeline import DROP_ON_RETRY, OWED_HARD_TELLS, OWED_REFUSED
    assert "place" not in DROP_ON_RETRY and "place" not in OWED_HARD_TELLS  # soft: a retry stays
    assert "his place" in DROP_ON_RETRY and "his place" in OWED_REFUSED
