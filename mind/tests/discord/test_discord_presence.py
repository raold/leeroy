from datetime import datetime

import pytest

from leeroy_mind.discord.presence import Presence, local_time, presence_of
from tests.conftest import make_persona


def st(owl, day, hour, region="UK", minute=0, guid=101):
    p = make_persona(guid, "Arkon", night_owl=owl, region=region)
    return presence_of(p, local=datetime(2026, 11, day, hour, minute), body_online=False,
                       body_zone=0, rng_seed=1).state


MON, SAT = 9, 14


def test_a_member_in_his_group_is_always_online_and_playing():
    p = make_persona(101, "Arkon", night_owl=True)
    assert presence_of(p, local=datetime(2026, 11, 9, 10, 0), body_online=True, body_zone=4395,
                       rng_seed=1, in_his_group=True) == Presence("online", True, 4395)


@pytest.mark.parametrize("region", ["UK", "US"])
def test_night_owls_are_up_late_and_workers_idle_at_work(region):
    for day in (MON, SAT):
        for h in (18, 21, 23, 0, 2):
            assert st(True, day, h, region) == "online"
        for h in (5, 8, 11):
            assert st(True, day, h, region) == "offline"
        assert st(True, day, 4, region) == "idle"
    assert st(False, MON, 7, region) == "online"
    assert st(False, MON, 8, region) == "online"
    assert st(False, MON, 8, region, 45) == "idle"
    assert st(False, MON, 17, region) == "online"
    assert st(False, MON, 23, region) == "online"
    assert st(False, MON, 23, region, 45) == "idle"
    assert st(False, MON, 3, region) == "offline"
    assert st(False, MON, 6, region) == "offline"
    assert st(False, SAT, 11, region) == "online"
    assert st(False, SAT, 3, region) == "offline"
    work = {st(False, MON, h, region, guid=g) for h in range(9, 17) for g in range(100, 140)}
    assert work == {"idle", "dnd"}


def test_dnd_is_about_one_day_in_seven():
    n = sum(st(False, MON, 11, guid=g) == "dnd" for g in range(1000, 1700))
    assert 60 < n < 140


def test_presence_is_stable_within_an_hour():
    p = make_persona(101, "Arkon")
    a = presence_of(p, local=datetime(2026, 11, 9, 11, 5), body_online=True, body_zone=7, rng_seed=3)
    b = presence_of(p, local=datetime(2026, 11, 9, 11, 55), body_online=True, body_zone=7, rng_seed=3)
    assert a == b


def test_playing_needs_online_state_and_body():
    p = make_persona(101, "Arkon")
    ev = datetime(2026, 11, 9, 20, 0)
    assert presence_of(p, local=ev, body_online=True, body_zone=5, rng_seed=1) == Presence("online", True, 5)
    assert not presence_of(p, local=ev, body_online=False, body_zone=5, rng_seed=1).playing
    day = datetime(2026, 11, 9, 11, 0)
    assert not presence_of(p, local=day, body_online=True, body_zone=5, rng_seed=1).playing


def test_local_time_shifts_uk_five_hours_ahead_of_us():
    base = datetime(2026, 11, 9, 20, 0)
    assert local_time(base, "US") == base
    assert local_time(base, "UK") == datetime(2026, 11, 10, 1, 0)
