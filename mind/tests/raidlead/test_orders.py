"""His words as orders: the fixed list, the addressees, designations, and chatter that never
triggers anything."""

import json

import pytest

from leeroy_mind.raidlead.orders import (ORDERS, SYSTEM, WHO, AskPlan, Countdown, Designate, Order,
                                         Ready, Unclear, parse, read_answer, schema)

NAMES = {"arkon": 101, "belra": 102, "coros": 103, "dunna": 104, "ebbo": 105}


@pytest.mark.parametrize("text, action, who, member, on", [
    ("everyone stack on me", "stack", "everyone", None, None),
    ("stack on me", "stack", "everyone", None, None),
    ("Stack on Belra!", "stack", "everyone", None, 102),
    ("stack up on bel", "stack", "everyone", None, 102),
    ("on me", "stack", "everyone", None, None),
    ("collapse on me guys", "stack", "everyone", None, None),
    ("spread out", "spread", "everyone", None, None),
    ("healers, spread out please", "spread", "healers", None, None),
    ("ranged: spread", "spread", "ranged", None, None),
    ("Belra, run out", "run_out", "member", 102, None),
    ("get out of the raid ebbo", "run_out", "member", 105, None),
    ("melee back off", "back_off", "melee", None, None),
    ("fall back", "back_off", "everyone", None, None),
    ("everyone hold", "hold", "everyone", None, None),
    ("wait for my pull everyone", "hold", "everyone", None, None),
    ("dps stop attacking", "hold", "damage", None, None),
    ("don't pull yet", "hold", "everyone", None, None),
    ("kill skull", "kill_skull", "everyone", None, None),
    ("everyone on skull", "kill_skull", "everyone", None, None),
    ("tanks swap", "taunt", "tanks", None, None),
    ("taunt", "taunt", "everyone", None, None),
    ("Dunna, taunt it", "taunt", "member", 104, None),
    ("lust now", "lust", "everyone", None, None),
    ("Bloodlust!", "lust", "everyone", None, None),
    ("heroism now", "lust", "everyone", None, None),
    ("belra, bl", "lust", "member", 102, None),
    ("release and run back", "release", "everyone", None, None),
    ("everyone release", "release", "everyone", None, None),
    ("run back", "release", "everyone", None, None),
    ("come back", "come_back", "everyone", None, None),
    ("regroup on me", "come_back", "everyone", None, None),
    ("get back here guys", "come_back", "everyone", None, None),
    ("everyone follow me", "follow", "everyone", None, None),
    ("with me", "follow", "everyone", None, None),
    ("everyone stay here", "stay", "everyone", None, None),
    ("hold position", "stay", "everyone", None, None),
    ("don’t move", "stay", "everyone", None, None),
    ("attack my target", "attack", "everyone", None, None),
    ("nuke it", "attack", "everyone", None, None),
    ("go go go", "attack", "everyone", None, None),
    ("dps, burn it", "attack", "damage", None, None),
    ("ok tanks, taunt now", "taunt", "tanks", None, None),
])
def test_orders_in_many_phrasings(text, action, who, member, on):
    assert parse(text, NAMES) == Order(action, who, member, on)


@pytest.mark.parametrize("text, guid", [
    ("Arkon, you're leading tonight", 101), ("Arkon leads", 101), ("arkon is raid lead", 101),
    ("arkon’s the raid lead", 101), ("you lead, belra", 102), ("raid lead: coros", 103),
    ("let ebbo lead", 105), ("Dunna, take the lead", 104), ("new lead is arkon", 101),
    ("I'll lead", None), ("i'm calling it tonight", None), ("nobody leads", None),
])
def test_designations(text, guid):
    assert parse(text, NAMES) == Designate(guid)


@pytest.mark.parametrize("text, intent", [
    ("rc", Ready()), ("everyone ready?", Ready()), ("ready?", Ready()), ("Belra, ready?", Ready(102)),
    ("pull in 3", Countdown(3)), ("pulling in 5", Countdown(5)), ("10 sec to pull", Countdown(10)),
    ("pull", Countdown(3)), ("Arkon, pull in 5", Countdown(5, 101)),
    ("what's the plan?", AskPlan()), ("Belra, what's the plan for this boss?", AskPlan(102)),
    ("how does this fight work?", AskPlan()),
])
def test_callouts(text, intent):
    assert parse(text, NAMES) == intent


@pytest.mark.parametrize("text, who, member", [
    ("everyone get behind the boss", "everyone", None),
    ("healers focus the tank", "healers", None),
    ("Belra, go stand by the pillar", "member", 102),
    ("kite it around the room tanks", "tanks", None),
])
def test_an_addressed_order_no_pattern_resolves_goes_to_the_model(text, who, member):
    assert parse(text, NAMES) == Unclear(who, member)


@pytest.mark.parametrize("text", [
    "lol that was a fun pull", "i love stacking gold", "anyone want to go fishing after?",
    "belra you're awesome", "stay safe out there", "we should spread the word", "the boss hits hard",
    "ok", "gg", "brb", "can you lust?", "who's on skull?", "follow the river to the town", "on it",
    "thanks guys", "go away", "stop it lol", "back in 5", "pull in 30", "nice job everyone",
    "everyone did great", "arkon leads the way in the dungeon finder", "is arkon leading?",
    "should we spread out?", "stack on top of the crate", "", "x" * 250,
    "one two three four five six seven eight nine ten eleven twelve stack",
])
def test_chatter_never_triggers(text):
    assert parse(text, NAMES) is None


def test_a_name_outside_his_group_is_no_member():
    assert parse("stack on zed", NAMES) is None
    assert parse("zed leads", NAMES) is None
    assert parse("Arkon, run out", {}) is None or parse("Arkon, run out", {}).who == "everyone"


def test_the_model_schema_is_the_fixed_list_and_his_group():
    s = schema(NAMES)
    assert s["properties"]["order"]["enum"] == [*ORDERS, "none"]
    assert s["properties"]["who"]["enum"] == list(WHO)
    assert s["properties"]["member"]["enum"] == ["", "arkon", "belra", "coros", "dunna", "ebbo"]
    assert set(s["required"]) == {"order", "who", "member", "confidence"}
    assert all(order in SYSTEM for order in ORDERS)


@pytest.mark.parametrize("answer, expected", [
    ({"order": "spread", "who": "healers", "member": "", "confidence": 0.9},
     (Order("spread", "healers", None, None, by_model=True), 0.9)),
    ({"order": "run_out", "who": "member", "member": "belra", "confidence": 1},
     (Order("run_out", "member", 102, None, by_model=True), 1.0)),
    ({"order": "none", "who": "everyone", "member": "", "confidence": 0.8}, (None, 0.8)),
    ({"order": "dance", "who": "everyone", "member": "", "confidence": 0.9}, None),
    ({"order": "spread", "who": "pets", "member": "", "confidence": 0.9}, None),
    ({"order": "spread", "who": "member", "member": "zed", "confidence": 0.9}, None),
    ({"order": "spread", "who": "everyone", "member": "belra", "confidence": 0.9}, None),
    ({"order": "spread", "who": "everyone", "member": "", "confidence": 1.5}, None),
    ({"order": "spread", "who": "everyone", "member": ""}, None),
    ([], None),
])
def test_the_model_answer_is_checked_again(answer, expected):
    assert read_answer(json.dumps(answer), NAMES) == expected


def test_a_model_answer_that_is_no_json_is_none():
    assert read_answer("spread out!", NAMES) is None
