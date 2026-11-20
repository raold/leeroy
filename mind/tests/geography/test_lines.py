"""The words a prompt says about places (geography/lines.py): her home, her "where you live,
right now" line, and where he lives (invented homes only)."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from leeroy_mind.geography import lines
from leeroy_mind.geography.places import sheet
from leeroy_mind.text import estimate_tokens

S = sheet()


def at(place, *args):
    return datetime(*args, tzinfo=S[place].tz)


def test_home_phrase():
    assert lines.home_phrase(S["Colorado"]) == "You live in Colorado."
    assert lines.home_phrase(S["uk.north_west"]) == "You live in the North West of England."
    assert lines.home_phrase(S["uk.uk"]) == ""  # her card says "from the UK" already
    assert lines.home_phrase(None) == ""


@pytest.mark.parametrize("key", sorted(S))
def test_the_now_line_fits_its_budget_every_month_and_hour(key):
    for month in range(1, 13):
        for day in (1, 15, 28):
            for hour in (0, 4, 7, 12, 16, 19, 23):
                parts = lines.now_parts(S[key], at(key, 2026, month, day, hour, 30))
                text = " ".join(parts)
                assert estimate_tokens(text) <= lines.NOW_TOKENS
                assert parts[0].startswith(f"Where you live: {S[key].label}")
                assert text.isascii()


def test_colorado_on_a_november_evening():
    parts = lines.now_parts(S["Colorado"], at("Colorado", 2026, 11, 24, 21, 0))
    assert parts[0] == ("Where you live: Colorado, high plains in the east, the Rocky Mountains "
                        "in the west.")
    assert parts[1].startswith("Late November there: highs near ")
    assert "snow possible" in parts[1] and "freezing at this hour" in parts[1]
    assert parts[2] == "It's dark out now."


def test_florida_in_july_is_hot_humid_and_never_snowy():
    parts = lines.now_parts(S["Florida"], at("Florida", 2026, 7, 3, 14, 0))
    text = " ".join(parts)
    assert "Early July there" in text and "hot and humid at this hour" in text
    assert "snow" not in text and "hurricane season" in text
    assert parts[-1] == "It's light out now."


def test_the_uk_in_celsius_and_dark_early_in_winter():
    text = " ".join(lines.now_parts(S["uk.scotland"], at("uk.scotland", 2026, 12, 18, 17, 30)))
    assert "highs near 7C" in text and "lows near 1C" in text and "F," not in text
    assert "It's dark out now." in text  # Scotland in December: dark by half past five


def test_the_light_follows_the_sun_where_she_is():
    phx = S["Arizona"]
    assert lines.light_words(phx, at("Arizona", 2026, 6, 21, 12, 0)) == "It's light out now."
    assert lines.light_words(phx, at("Arizona", 2026, 6, 21, 2, 0)) == "It's dark out now."
    # the same instant is day in Honolulu and night in London
    instant = datetime(2026, 6, 21, 22, 0, tzinfo=timezone.utc)
    assert lines.light_words(S["Hawaii"], instant.astimezone(S["Hawaii"].tz)) == \
        "It's light out now."
    assert lines.light_words(S["uk.london"], instant.astimezone(S["uk.london"].tz)) != \
        "It's light out now."


def test_typical_temperatures_ease_between_months():
    co = S["Colorado"]
    mid = lines.typical(co, at("Colorado", 2026, 7, 15, 12, 0))
    assert mid == (co.highs[6], co.lows[6])
    early = lines.typical(co, at("Colorado", 2026, 7, 1, 12, 0))
    assert co.highs[5] < early[0] < co.highs[6]


def test_the_day_warms_to_mid_afternoon_and_cools_overnight():
    hi, lo = 80.0, 50.0
    temps = [lines.at_hour(hi, lo, datetime(2026, 7, 1, h, 0)) for h in range(24)]
    assert temps[6] == lo and temps[15] == hi
    assert temps[3] < temps[21] < temps[18] < temps[15]


@pytest.mark.parametrize("temp,word", [(10, "bitterly cold"), (30, "freezing"), (40, "cold"),
                                       (55, "cool"), (65, "mild"), (75, "warm"), (85, "hot"),
                                       (100, "very hot")])
def test_feel_words(temp, word):
    assert lines.feel(temp, humid=False) == word


def test_travel_between_places():
    assert lines.travel(S["Nevada"], S["Nevada"]) == "the same state as you"
    assert lines.travel(S["uk.wales"], S["uk.wales"]) == "the same part of the UK as you"
    assert lines.travel(S["Montana"], S["Wyoming"]) == "about 7 hours' drive"
    far = lines.travel(S["Montana"], S["Florida"])
    assert far.startswith("about ") and "hours' flight, or about" in far
    assert lines.travel(S["uk.scotland"], S["Montana"]).startswith("an ocean away")
    assert lines.travel(S["Hawaii"], S["Montana"]).startswith("a long way")
    assert lines.travel(S["New Jersey"], S["New York"]) == "under an hour's drive"


def test_his_clock_against_hers_keeps_daylight_saving_apart():
    summer = datetime(2026, 7, 1, 18, 0, tzinfo=timezone.utc)
    winter = datetime(2026, 1, 15, 18, 0, tzinfo=timezone.utc)
    # Arizona keeps no daylight saving time: level with Nevada in summer, an hour ahead in winter
    assert lines.clock_gap(S["Arizona"], S["Nevada"], summer, "his") == \
        "You keep the same time."
    assert lines.clock_gap(S["Arizona"], S["Nevada"], winter, "his") == \
        "His clock is 1 hour behind yours."
    assert lines.clock_gap(S["Nevada"], S["Montana"], winter, "their") == \
        "Their clock is 1 hour ahead of yours."
    # the week the US has changed its clocks and Britain has not
    march = datetime(2026, 3, 16, 18, 0, tzinfo=timezone.utc)
    assert lines.clock_gap(S["Montana"], S["uk.london"], march, "his") == \
        "His clock is 6 hours ahead of yours."
    assert lines.clock_gap(S["Montana"], S["uk.london"], winter, "his") == \
        "His clock is 7 hours ahead of yours."


def test_where_he_lives():
    assert lines.where_he_lives(S["Montana"], "") == "Montana"
    assert lines.where_he_lives(S["Montana"], "Lakeview") == "Lakeview, Montana"
    assert lines.where_he_lives(S["uk.north_west"], "Lakeview") == \
        "Lakeview in the North West of England"


def test_owner_line():
    instant = datetime(2026, 1, 15, 18, 0, tzinfo=timezone.utc)
    line = lines.owner_line(called="Zan", their="his", home=S["Montana"], detail="",
                            her=S["Nevada"], instant=instant, apart=True)
    assert line.startswith("Zan lives in Montana. From you that's about ")
    assert "His clock is 1 hour ahead of yours." in line
    assert line.endswith(lines.MEET_WHAT_IF)
    met = lines.owner_line(called="Zan", their="his", home=S["Montana"], detail="",
                           her=S["Nevada"], instant=instant, apart=False)
    assert lines.MEET_WHAT_IF not in met
    unknown = lines.owner_line(called="Zan", their="his", home=S["Montana"], detail="",
                               her=None, instant=instant, apart=True)
    assert unknown == "Zan lives in Montana. " + lines.MEET_WHAT_IF


def test_naive_local_time_is_read_in_her_own_zone():
    naive = datetime(2026, 12, 18, 16, 30)
    assert lines.light_words(S["uk.scotland"], naive) == \
        lines.light_words(S["uk.scotland"], naive.replace(tzinfo=ZoneInfo("Europe/London")))
