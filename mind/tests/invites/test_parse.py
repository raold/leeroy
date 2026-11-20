"""His invitation read from one game line by patterns: what, who and where, a line the model must
read, and the many lines that are no invitation at all."""
import pytest

from leeroy_mind.invites import parse as P
from leeroy_mind.invites import spots as S

NAMES = {101: "Arkon", 102: "Belra", 103: "Coros"}
ORGRIMMAR, DALARAN, STORMWIND = 1637, 4395, 1519


def read(text, channel="whisper", zone=ORGRIMMAR):
    return P.parse(text, channel=channel, names=NAMES, his_zone=zone)


@pytest.mark.parametrize("text,what,spot", [
    ("let's go fishing in Dalaran", "fish", "dal_fountain"),
    ("wanna go fishing?", "fish", None),
    ("fishing in booty bay, you in?", "fish", "booty_bay_docks"),
    ("come to the inn", "meet", "org_inn"),
    ("meet me at the bank", "meet", "org_bank"),
    ("meet me at krasus", "meet", "dal_krasus"),
    ("see you at the eventide fountain", "meet", "dal_fountain"),
    ("stop by the bank", "meet", "org_bank"),
    ("let's go to dalaran", "meet", "dal_krasus"),
    ("come here", "come", None),
    ("c'mere", "come", None),
    ("come keep me company", "come", None),
    ("come join me", "come", None),
    ("fancy a drink?", "sit", None),
    ("come sit with me at the inn", "sit", "org_inn"),
    ("follow me", "follow", None),
    ("come with me", "follow", None),
    ("join my group", "join", None),
    ("group up", "join", None),
])
def test_what_and_where(text, what, spot):
    ask = read(text)
    assert isinstance(ask, P.Ask) and ask.what == what and ask.who == "you" and not ask.later
    assert (ask.place.name if isinstance(ask.place, S.Spot) else ask.place) == spot


@pytest.mark.parametrize("text", [
    "i need to go to bed", "see you tomorrow", "the bank is so crowded", "i'm heading to the inn",
    "don't come to the inn", "lol", "gz", "how was your day", "never mind, come here", "nvm",
    "i went fishing yesterday", ".playerbots bot add5", "/sit",
])
def test_no_invitation(text):
    assert read(text, channel="guild") is None


def test_a_place_none_of_ours_is_unknown_and_she_asks_where():
    assert read("meet me at the docks").place is S.UNKNOWN  # Orgrimmar has no docks of ours
    assert read("meet me").place is S.UNKNOWN
    assert read("meet me at the inn", zone=STORMWIND).place is S.UNKNOWN  # no Stormwind inn known
    assert read("wanna go to the fountain?").place is S.UNKNOWN  # no fountain in Orgrimmar


def test_the_place_he_names_follows_the_city_he_is_in_or_names():
    assert read("meet me at the inn", zone=DALARAN).place.name == "dal_inn"
    assert read("meet me at the bank in uc").place.name == "uc_bank"
    assert read("meet me at the filthy animal").place.name == "dal_horde_inn"
    assert read("let's go to tb").place.name == "tb_inn"


def test_later_moves_nobody():
    for text in ("lets go fishing later", "meet me at the inn tonight", "fishing tomorrow?",
                 "come to the inn after the raid", "meet me at the bank in 20 min"):
        ask = read(text)
        assert isinstance(ask, P.Ask) and ask.later, text


@pytest.mark.parametrize("channel,text,who,named", [
    ("whisper", "come here", "you", ()),
    ("guild", "anyone wanna go fishing?", "anyone", ()),
    ("guild", "who wants to sit at the inn with me", "anyone", ()),
    ("guild", "let's go fishing", "anyone", ()),
    ("say", "anyone up for a drink at the inn?", "anyone", ()),
    ("party", "follow me", "group", ()),
    ("party", "anyone wanna fish?", "anyone", ()),
    ("raid", "come here guys", "group", ()),
    ("guild", "Arkon come here", "named", (101,)),
    ("guild", "belra and arkon, meet me at the bank", "named", (102, 101)),
    ("party", "Coros, Belra and Arkon come here", "named", (103, 102)),  # two at most
])
def test_who(channel, text, who, named):
    ask = read(text, channel=channel)
    assert isinstance(ask, P.Ask) and (ask.who, ask.named) == (who, named)


def test_a_line_the_patterns_cannot_settle_is_for_the_model():
    assert isinstance(read("do you fish?"), P.Maybe)
    assert isinstance(read("inn?", channel="guild"), P.Maybe)
    assert read("i like the inn", channel="guild") is None  # no ask in it: plain talk


def test_the_models_class_becomes_an_ask_with_the_patterns_who_and_where():
    ask = P.ask_of("sit", "the inn in dalaran, you?", channel="whisper", names=NAMES,
                   his_zone=ORGRIMMAR)
    assert (ask.what, ask.who, ask.place.name, ask.later) == ("sit", "you", "dal_inn", False)
    later = P.ask_of("later", "fishing at some point?", channel="guild", names=NAMES,
                     his_zone=ORGRIMMAR)
    assert later.later and later.what == "fish"
    assert P.ask_of("none", "inn?", channel="guild", names=NAMES, his_zone=ORGRIMMAR) is None
    assert P.ask_of("dance", "inn?", channel="guild", names=NAMES, his_zone=ORGRIMMAR) is None
    assert P.ask_of("meet", "meet?", channel="guild", names=NAMES, his_zone=ORGRIMMAR).place is S.UNKNOWN


def test_come_on_is_no_invitation_and_no_reading():
    for text in ("come on guys, pull", "oh come on", "come on, really?"):
        assert read(text, channel="guild") is None, text


@pytest.mark.parametrize("text", [
    "anyone have spare fish?", "anyone need drinks?", "anyone got drinks", "where's the fishing trainer?",
    "anyone have a fishing pole?", "who wants water and drinks",
])
def test_things_he_wants_are_no_invitation(text):
    assert not isinstance(read(text, channel="guild"), P.Ask), text


@pytest.mark.parametrize("text,what", [
    ("anyone fishing?", "fish"), ("wanna fish?", "fish"), ("fish with me", "fish"),
    ("anyone up for a drink?", "sit"), ("wanna grab a pint?", "sit"), ("sit down with me", "sit"),
])
def test_things_to_do_together_are(text, what):
    ask = read(text, channel="guild")
    assert isinstance(ask, P.Ask) and ask.what == what, text
