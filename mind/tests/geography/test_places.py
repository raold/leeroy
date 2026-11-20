"""The place sheet and where each member lives on it (geography/places.py). Invented
personas and texts only."""
from dataclasses import replace
from zoneinfo import ZoneInfo

import pytest

from conftest import make_persona
from leeroy_mind.geography import places
from leeroy_mind.geography.places import lookup, place_of, sheet, uk_region
from leeroy_mind.personas.states import US_STATES


def test_the_sheet_holds_every_state_dc_and_the_uk():
    s = sheet()
    us = {k for k, p in s.items() if p.country == "US"}
    assert us == set(US_STATES) | {"District of Columbia"}
    uk = {k for k, p in s.items() if p.country == "UK"}
    assert uk == {"uk.uk", "uk.london", "uk.south_east", "uk.south_west", "uk.east",
                  "uk.east_midlands", "uk.west_midlands", "uk.yorkshire", "uk.north_west",
                  "uk.north_east", "uk.wales", "uk.scotland", "uk.northern_ireland"}


@pytest.mark.parametrize("key", sorted(sheet()))
def test_every_place_is_complete_and_sane(key):
    p = sheet()[key]
    assert len(p.highs) == len(p.lows) == 12
    assert all(lo < hi for lo, hi in zip(p.lows, p.highs))
    assert -30 < min(p.lows) and max(p.highs) < 115  # degrees F
    ZoneInfo(p.zone)
    assert p.coast in ("ocean", "lakes", "none")
    assert p.terrain and p.cities and p.touchstones and p.geography
    assert p.snow <= set(range(1, 13))
    assert all(e.months <= set(range(1, 13)) and e.what for e in p.events)
    assert -90 <= p.point[0] <= 90 and -180 <= p.point[1] <= 180
    assert len(p.terrain) <= 100
    assert all(s in ("winter", "spring", "summer", "autumn") for s, _ in p.notes)
    if p.country == "UK":
        assert p.zone == "Europe/London" and p.celsius
    # the warmest months are summer's, the coldest winter's (south of the equator: none)
    assert p.highs.index(max(p.highs)) in (5, 6, 7, 8)
    assert p.lows.index(min(p.lows)) in (11, 0, 1)


def test_a_few_climates_people_know():
    s = sheet()
    assert s["Florida"].highs[6] > s["Colorado"].highs[6] - 10
    assert s["Florida"].lows[0] > s["Colorado"].lows[0] + 25  # Florida's winter is mild
    assert 7 not in s["Florida"].snow and 7 in s["Colorado"].snow  # the Rockies, any month
    assert s["Florida"].coast == "ocean" and not s["Florida"].mountains
    assert s["Colorado"].coast == "none" and s["Colorado"].mountains
    assert s["Arizona"].zone == "America/Phoenix"  # no daylight saving time
    assert s["Hawaii"].zone == "Pacific/Honolulu"
    assert s["uk.scotland"].lows[0] < s["uk.london"].lows[0]


@pytest.mark.parametrize("name,key", [
    ("Montana", "Montana"), ("montana", "Montana"), ("  New   Mexico ", "New Mexico"),
    ("NV", "Nevada"), ("wy", "Wyoming"), ("DC", "District of Columbia"),
    ("Washington, D.C.", "District of Columbia"), ("Washington", "Washington"),
    ("Scotland", "uk.scotland"), ("the North West of England", "uk.north_west"),
    ("North West England", "uk.north_west"), ("north west", "uk.north_west"),
    ("Yorkshire", "uk.yorkshire"), ("England", "uk.uk"), ("UK", "uk.uk"),
    ("Glasgow", "uk.scotland"), ("East Midlands", "uk.east_midlands")])
def test_lookup_reads_the_usual_spellings(name, key):
    assert lookup(name).key == key


@pytest.mark.parametrize("name", ["", "   ", "Atlantis", "Springfield", "XX", None, 7,
                                  "Welsh", "the moon"])
def test_lookup_refuses_anything_else(name):
    assert lookup(name) is None


def _uk(**real_life):
    p = make_persona(105, "Ebbo")
    return replace(p, real_life=replace(p.real_life, **real_life))


def test_a_uk_member_lives_where_her_own_words_put_her():
    assert uk_region(_uk(job="nurse at a hospital in Leeds")) == "uk.yorkshire"
    assert uk_region(_uk(household="flat in Manchester with her cat")) == "uk.north_west"
    assert uk_region(_uk(household="lives near Cardiff", job="teacher")) == "uk.wales"
    assert place_of(_uk(job="barista in Glasgow")).key == "uk.scotland"


def test_a_relatives_place_counts_only_when_she_names_none_of_her_own():
    assert uk_region(_uk(household="her mum lives in Bristol, she rents in Leeds")) \
        == "uk.yorkshire"
    assert uk_region(_uk(household="her mum lives in Bristol")) == "uk.south_west"


def test_where_she_is_from_counts_only_when_no_place_is_named():
    assert uk_region(_uk(household="a Welsh lass sharing a house in Bristol")) == "uk.south_west"
    assert uk_region(_uk(household="a proud Welsh lass")) == "uk.wales"


def test_words_that_are_ordinary_words_need_a_place_around_them():
    assert uk_region(_uk(life_thread="Reading more books this year")) is None
    assert uk_region(_uk(household="rents a flat in Reading")) == "uk.south_east"
    assert uk_region(_uk(job="works shifts in Hull")) == "uk.yorkshire"
    assert uk_region(_uk(job="Hull of a ship")) is None


def test_a_uk_member_without_a_place_lives_in_the_uk_as_a_whole():
    p = make_persona(105, "Ebbo")
    assert uk_region(p) is None and place_of(p).key == "uk.uk"


def test_a_us_member_lives_in_her_home_state_or_nowhere_on_the_sheet():
    p = make_persona(101, "Arkon", region="US", accent="en-us")
    assert place_of(p) is None  # no home yet: today's behaviour everywhere
    assert place_of(replace(p, identity=replace(p.identity, home="Montana"))).key == "Montana"
    assert place_of(None) is None


def test_distances_are_great_circle_kilometres():
    s = sheet()
    km = places.km_between(s["uk.london"], s["uk.scotland"])
    assert 450 < km < 650
    assert places.km_between(s["Nevada"], s["Nevada"]) == 0


def test_all_names_hold_cities_and_states_but_not_the_uk_itself():
    names = places.all_names()
    assert {"denver", "colorado", "glasgow", "scotland", "district of columbia"} <= names
    assert "uk" not in names
