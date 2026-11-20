"""Lines that get places wrong (geography/tells.py): her own place contradicted here and now,
and his home said where others read. Invented lines and homes only."""
from datetime import datetime

import pytest

from leeroy_mind.geography.config import OwnerHome
from leeroy_mind.geography.places import sheet
from leeroy_mind.geography.tells import his_home, slip

S = sheet()


def at(key, *args):
    return datetime(*args, tzinfo=S[key].tz)


JULY_FL = (S["Florida"], at("Florida", 2026, 7, 14, 15, 0))
JULY_CO = (S["Colorado"], at("Colorado", 2026, 7, 14, 15, 0))
JAN_MN = (S["Minnesota"], at("Minnesota", 2026, 1, 14, 15, 0))
JAN_LONDON = (S["uk.london"], at("uk.london", 2026, 1, 14, 15, 0))


@pytest.mark.parametrize("where,text,code", [
    (JULY_FL, "ugh its snowing again here", "snow"),
    (JULY_FL, "we got a foot of snow overnight lol", "snow"),
    (JULY_FL, "had to scrape the frost off my car this morning", "frost"),
    (JULY_FL, "its like 20 degrees out, freezing my butt off", "temperature"),
    (JAN_MN, "its 95 degrees out today", "temperature"),
    (JAN_MN, "this heatwave is killing me", "heat"),
    (JAN_LONDON, "its 30 degrees out, proper scorcher", "temperature"),
    (JULY_CO, "walked down to the ocean this morning", "coast"),
    (JULY_CO, "walked to the beach this morning", None),  # a lake or a reservoir has one
    (JULY_CO, "the beach is five minutes away", None),
    (JULY_CO, "we live on the coast", "coast"),
    (JULY_CO, "the ocean breeze here is lovely", "coast"),
    (JULY_CO, "i miss the ocean so much", None),  # no claim the sea is near her
    (JULY_CO, "i live by the sea so its always windy", "coast"),
    ((S["Ohio"], at("Ohio", 2026, 7, 14, 15, 0)), "i live right by the ocean", "coast"),
    ((S["Ohio"], at("Ohio", 2026, 7, 14, 15, 0)), "walked down to the beach after work", None),
    ((S["Kansas"], at("Kansas", 2026, 7, 14, 15, 0)), "the mountains behind my house are gorgeous",
     "mountains"),
    (JULY_CO, "the mountains behind my house are gorgeous", None),
    (JULY_CO, "its 3am here and i cant sleep", "clock"),
    (JULY_CO, "its 3pm here, still at work", None),
    (JULY_CO, "its 2:30pm lol", None),
    (JULY_CO, "it's the middle of the night here", "clock"),
    (JULY_CO, "its afternoon here", None),
    (JULY_CO, "its snowing up in the high country", None),  # the Rockies get summer snow
])
def test_slips(where, text, code):
    place, local = where
    assert slip(text, place, local) == code


@pytest.mark.parametrize("text", [
    "its snowing in icecrown lol",                # the game
    "my sister says its snowing in denver",       # another place
    "is it snowing where you are",                # him
    "last winter we got a foot of snow",          # the past
    "if it snowed here id lose my mind",          # a what-if
    "on our trip it was snowing every day",       # a trip
    "its like 30 degrees in here, the ac died",   # indoors
    "i wish i lived by the sea",                  # a wish
    "gear it snowing loot",                       # game words
    "its 90 at the raid lol",                     # not a temperature claim about her place
])
def test_never_a_slip(text):
    place, local = JULY_FL
    assert slip(text, place, local) is None


def test_her_own_city_counts_as_her_place():
    place, local = JULY_FL
    assert slip("snowing here in orlando, insane", place, local) == "snow"


def test_no_place_no_slip():
    assert slip("its snowing", None, datetime(2026, 7, 14, 15, 0)) is None
    assert slip("", S["Florida"], datetime(2026, 7, 14, 15, 0)) is None


def test_a_uk_member_reads_degrees_as_celsius_unless_she_says_f():
    place, local = JAN_LONDON
    assert slip("its about 5 degrees out", place, local) is None
    assert slip("its about 40 degrees F out", place, local) is None
    assert slip("its 25c out", place, local) == "temperature"


HOME = OwnerHome(S["Montana"], "Lakeview")


@pytest.mark.parametrize("text,said", [
    ("how's the weather in montana for you", True),
    ("you're in montana right, must be cold", True),
    ("zan lives in montana now", True),
    ("isn't lakeview where your folks are", True),
    ("montana is gorgeous in summer", False),       # not about him
    ("my cousin lives in montana", False),
    ("you good?", False),
])
def test_his_home_said_where_others_read(text, said):
    assert his_home(text, HOME, him=["Zan"]) is said


def test_his_home_is_his_to_make_public():
    shown = ["Zan: snowing like crazy here in montana"]
    assert not his_home("how cold is montana for you right now", HOME, shown=shown)
    assert his_home("lakeview sounds cold for you", HOME, shown=shown)  # the town he did not say


def test_no_owner_home_nothing_to_say():
    assert not his_home("you're in montana right", None)
