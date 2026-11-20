"""His "you lead" read by patterns: what she is asked to do, who, where, and his own end."""
import pytest

from leeroy_mind.invites import spots as S
from leeroy_mind.lead.parse import End, Start, ends, parse

NAMES = {101: "Arkon", 102: "Belra", 103: "Coros"}
ORG = 1637
DAL = 4395


def p(text, channel="party", zone=ORG):
    return parse(text, channel=channel, names=NAMES, his_zone=zone)


@pytest.mark.parametrize("text", [
    "you lead", "you lead the way", "take the lead", "you take the lead", "lead the way",
    "lead on", "lead us", "you lead us", "show us the way", "your lead",
    "can you lead?", "wanna lead?", "could you take the lead", "mind leading?",
    "take us questing", "lead us questing", "take me to do quests", "find us some quests",
    "go questing, you lead", "ok you lead!"])
def test_lead_asks_are_questing_by_default(text):
    ask = p(text)
    assert isinstance(ask, Start) and ask.activity == "quest" and ask.place is None


@pytest.mark.parametrize("text", [
    "go grind", "go grind some mobs", "take us grinding", "lead us to grind", "you lead, let's grind",
    "find us some mobs to kill", "take the lead and find us mobs to farm"])
def test_grinding(text):
    ask = p(text)
    assert isinstance(ask, Start) and ask.activity == "grind"


def test_travel_to_one_of_our_spots():
    ask = p("lead us to the inn")
    assert ask.activity == "travel" and ask.place is S.BY_NAME["org_inn"]
    ask = p("take us to the bank in dalaran", zone=ORG)
    assert ask.activity == "travel" and ask.place is S.BY_NAME["dal_bank"]
    ask = p("Belra, lead the way to krasus landing", zone=DAL)
    assert ask.activity == "travel" and ask.place is S.BY_NAME["dal_krasus"]
    assert ask.who == "named" and ask.named == (102,)


def test_travel_somewhere_none_of_ours_moves_nobody():
    for text in ("lead us to the cave", "take us to stormwind", "lead us to the inn in thousand needles"):
        ask = p(text)
        assert ask.activity == "travel" and ask.place is S.UNKNOWN, text


def test_travel_to_an_activity_is_that_activity():
    assert p("take us to quest").activity == "quest"
    assert p("lead us to some mobs to grind").activity == "grind"


def test_who():
    assert p("you lead", channel="whisper").who == "you"
    assert p("Arkon you lead").who == "named" and p("Arkon you lead").named == (101,)
    assert p("take the lead, coros").named == (103,)
    assert p("who wants to lead?").who == "anyone"
    assert p("someone take the lead").who == "anyone"
    assert p("you lead").who == "group"


def test_another_time_is_not_the_leads():
    # "Ana, you're leading tonight" is the raid lead's designation (raidlead/)
    assert p("you lead tomorrow") is None
    assert p("take us questing after the raid") is None
    assert p("Arkon, you lead tonight") is None


@pytest.mark.parametrize("text", [
    "i'll lead", "i'll take the lead", "let me lead", "don't take the lead", "never mind, don't lead",
    "you lead a busy life", "lead ore is cheap", "who led the raid last night?", "nice lead",
    "what quest are you on?", "grinding is boring", "lead", ".lead", "follow me",
    "the lead singer was great", "you're in charge", "you're leading", "x" * 200])
def test_not_an_ask(text):
    assert p(text) is None


@pytest.mark.parametrize("text", [
    "i'll lead", "i'll lead now", "I'll take it from here", "i'll take over", "i'm leading",
    "let me lead", "my lead", "stop leading", "stop", "ok stop", "that's enough", "follow me",
    "back to me", "come back", "regroup on me"])
def test_his_end(text):
    assert isinstance(ends(text), End)


def test_follow_me_is_a_playerbots_command_too():
    assert ends("follow me").command
    assert not ends("stop leading").command


@pytest.mark.parametrize("text", ["stop being silly", "don't stop", "you lead", "follow the road", ""])
def test_not_an_end(text):
    assert ends(text) is None
