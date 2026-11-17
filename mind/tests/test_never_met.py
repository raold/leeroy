"""Never met, as a hard rule (self-play audit, 2026-11-17): a member whose arc has not met him
never says they have met, been together or handed each other anything, in her DM or her whisper
to him, an answer he is owed included; the prompt says so plainly, and memories that see him in
person stay out of it. Invented lines only."""
import asyncio
import logging
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from discord.test_dm_prompt import answer, answers, rig_for, talk_for
from engine_rig import numbered
from leeroy_mind.talk import moves
from leeroy_mind.talk.filters import find_tells
from leeroy_mind.talk.moves import (have_met, in_person, is_summon, meets_in_memory,
                                    without_meeting)
from leeroy_mind.talk.pipeline import (DROP_ON_RETRY, NEVER_MET, OWED_HARD_TELLS, OWED_REFUSED,
                                       _far_memories)
from leeroy_mind.talk.prompt import SO_FAR_HEAD
from leeroy_mind.testing.fake_clock import FakeClock
from test_sofar import his_dm, section


@pytest.fixture
def clock():
    return FakeClock(datetime(2026, 10, 29, 20, tzinfo=ZoneInfo("America/New_York")), yields=64)


MET = [
    "still wearing your jumper, it smells like you",
    "remember when we kissed outside the station",
    "you left your scarf at mine",
    "last time you were at my place you fixed the shelf",
    "when we met up you were so nervous",
    "i slept in your shirt again",
    "you stayed at mine and it was the best sleep ive had",
    "that night we cuddled on the sofa",
]
# never a claim: wishes, denials, and everything the game really gives them (review of #368,
# P2-5): they did meet in the game, they raid at night, members mail him letters and gifts,
# and her own household has its dinners
NOT_MET = [
    "we've never met, you know that",
    "if you were here id make you tea",
    "one day when we meet ill hold you to that",
    "i wish i was wearing your jumper right now",
    "when we met in the guild you were a hunter",
    "when we first met you were a warrior",
    "when we met in icc you wiped us twice",
    "since we met ive healed better",
    "last time you were here we wiped on the third boss",
    "last night we stayed up way too late talking",
    "when we went for the achievement you carried",
    "you came over to my bank alt",
    "like friday night raid",
    "like tuesday night when we wiped",
    "that thing you do with your voice on vent",
    "we have dinner in an hour so brb",
    "our dinner is getting cold",
    "you gave me the ring from the vault",
    "i sent you a card in the mail",
    "you sent me that book rec",
    "you sent me that screenshot of the boss",
    "you sent me 50g for repairs",
    "remember when we wiped on that trash pull",
    "is your jumper the green one you mentioned",
    "last time you were on vent you were so quiet",
    "your shirt sounds awful honestly",
]


def test_have_met_claims_and_what_ifs():
    for line in MET:
        assert have_met(line) and in_person(line), line
    for line in NOT_MET:
        assert not have_met(line), line
    assert in_person("come to my quiz night, sit next to me")  # the plans of before still count
    assert not in_person("come to the raid tonight")


def test_summons_widened():
    for line in ("youre coming to me, end of", "come home to me", "ur coming over tonight"):
        assert is_summon(line), line
    for line in ("are you coming to the raid", "i came home late from work", "come home safe",
                 "come home soon from work", "come show me your new mount"):
        assert not is_summon(line), line


def test_the_tell_is_hard_for_owed_answers_too():
    assert "in person" in DROP_ON_RETRY and "in person" in OWED_HARD_TELLS
    assert "in person" in OWED_REFUSED
    tells = {t.reason for t in find_tells("still wearing your jumper, it smells like you",
                                          summons_ok=False, far=True)}
    assert "in person" in tells
    assert "in person" not in {t.reason for t in find_tells(
        "still wearing your jumper, it smells like you", summons_ok=False, far=False)}


def test_without_meeting_keeps_the_rest_of_her_answer():
    text = ("honestly today was long. still wearing your jumper, it smells like you. "
            "the cat knocked a plant over again")
    assert without_meeting(text) == "honestly today was long. the cat knocked a plant over again"
    assert without_meeting("come home to me") == ""
    two = "missed you today\nyou left your scarf at mine btw\nsleep well"
    assert without_meeting(two) == "missed you today\nsleep well"


def test_memories_that_see_him_in_person_stay_out_of_her_prompt():
    def mem(text, kind="reflection", actors=()):
        return SimpleNamespace(text=text, kind=kind, actors=actors)
    names = ("Testmage", "Tomasz")
    keep = [mem("I want to ask Testmage about the new raid schedule", "plan", (1,)),
            mem("Cooking dinner for the kids tomorrow, need to shop", "plan"),
            mem("He said the patch broke his addons", actors=(1,)),
            mem("We wiped twice on the last boss and Testmage laughed it off", "event", (1,)),
            # her own life, not his (review of #368, P2-4)
            mem("I found her keys under the sofa again", "event"),
            mem("His coat was soaked when he came in from school", "event"),
            mem("Dinner with my brother on saturday, book the table", "plan"),
            mem("I bought the ring from the vendor in Dalaran", "event", (1,)),
            mem("Testmage had dinner with his family before the raid", "event", (1,))]
    drop = [mem("Pin Testmage down on a time for dinner tomorrow, no vague answers", "plan", (1,)),
            mem("I need to work out when he is free for dinner", "plan", (1,)),
            mem("Going to the gig to see if his jokes work in person", "plan"),
            mem("I am still wearing his hoodie and it smells like him", actors=(1,)),
            mem("Tomasz and I should meet in person this summer", "plan")]
    assert _far_memories([*keep, *drop], 1, names) == keep
    assert meets_in_memory("take him out for coffee this weekend", about_him=True)
    assert not meets_in_memory("take him out for coffee this weekend")  # nobody says it is him
    assert meets_in_memory("lunch plans", names, about_him=True)
    assert not meets_in_memory("lunch plans", names)
    assert not meets_in_memory("dinner after the raid with him", about_him=True)


def test_her_dm_prompt_says_they_have_never_met_until_her_arc_has(store, clock, tmp_path):
    rig = rig_for(store, clock, tmp_path, script=numbered("mm {n}, go on then"))
    talk = talk_for(rig)
    mid = his_dm(rig, "what would you do if i was there")
    answer(rig, talk, mid)
    far = section(rig.prompts()[-1], SO_FAR_HEAD)
    line = NEVER_MET.format(name=rig.engine.human.called)
    assert far.splitlines()[0] == "- " + line
    assert "never met in person" in line and "outside the game" in line  # the game's mail is real
    rig.engine._met_him = lambda guid: True  # her arc has met him (#325's met state)
    mid = his_dm(rig, "ok and after that")
    answer(rig, talk, mid)
    assert "never met in person" not in rig.prompts()[-1]
    rig.close()


@pytest.mark.parametrize("claim", ["still wearing your jumper, it smells like you.",
                                   "you left your scarf at mine."])
def test_an_owed_dm_answer_never_goes_out_claiming_they_met(store, clock, tmp_path, caplog,
                                                            claim):
    """Both drafts say it: the claim's sentence is cut and the rest of her answer goes."""
    caplog.set_level(logging.INFO)
    rig = rig_for(store, clock, tmp_path)
    rig.script(f"long day honestly. {claim} how was yours",
               f"it was a long one. {claim} tell me about yours")
    talk = talk_for(rig)
    mid = his_dm(rig, "how was your day then")
    answer(rig, talk, mid)
    got = answers(rig, "dm:102", mid)
    assert got and all("jumper" not in m["text"] and "scarf" not in m["text"] for m in got)
    said = " ".join(m["text"] for m in got)
    assert "long one" in said and "yours" in said
    assert "say_owed_cut: guid=102" in caplog.text and "tells=in person" in caplog.text
    assert "jumper" not in caplog.text and "scarf" not in caplog.text
    hint = rig.chat_calls()[1]["messages"][-1]["content"]
    assert "never say you have met" in hint  # the rewrite was told why
    rig.close()


def test_a_draft_that_is_only_the_claim_falls_back_to_a_fresh_short_answer(store, clock,
                                                                             tmp_path, caplog):
    caplog.set_level(logging.INFO)
    rig = rig_for(store, clock, tmp_path)
    rig.script("still wearing your jumper, it smells like you",
               "you left your scarf at mine",
               "busy but fine, the cat broke another plant")
    talk = talk_for(rig)
    mid = his_dm(rig, "how was your day then")
    answer(rig, talk, mid)
    got = answers(rig, "dm:102", mid)
    assert [m["text"] for m in got] == ["busy but fine, the cat broke another plant"]
    assert "say_owed_fallback: guid=102" in caplog.text
    rig.close()


def test_a_whisper_answer_never_claims_they_met_either(store, clock, tmp_path, caplog):
    caplog.set_level(logging.INFO)

    async def main():
        rig = rig_for(store, clock, tmp_path)
        rig.world.human_online = True
        await rig.session()
        await rig.advance(120)  # the greetings of his login are said
        before = len(rig.prompts())
        rig.script("long day. when we met up you were so nervous. how was yours",
                   "long one. you left your scarf at mine. tell me about yours")
        await rig.human("how was your day then", channel="whisper", to_guid=102)
        await rig.advance(90)
        said = [s.text for s in rig.sent.says() if s.guid == 102 and s.channel == "whisper"]
        assert said and not any("met" in x or "scarf" in x for x in said)
        prompt = rig.prompts()[before]
        assert "never met in person" in section(prompt, SO_FAR_HEAD)
        assert "say_owed_cut: guid=102" in caplog.text
        rig.close()
    asyncio.run(main())
