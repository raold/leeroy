"""Her own clock (discord.presence.her_local): her home's zone with [geography] on, today's
region rule otherwise. Daylight saving on both sides of the Atlantic; invented homes only."""
from dataclasses import replace
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from conftest import make_persona
from leeroy_mind.discord import presence
from leeroy_mind.discord.presence import OWNER_ZONE, her_local, local_time
from leeroy_mind.geography import config as geo_config

EASTERN = ZoneInfo("America/New_York")


def us(home=None, guid=101):
    p = make_persona(guid, "Arkon", region="US", accent="en-us")
    return replace(p, identity=replace(p.identity, home=home)) if home else p


def uk(job="warehouse supervisor"):
    p = make_persona(105, "Ebbo")
    return replace(p, real_life=replace(p.real_life, job=job))


def test_a_us_member_with_a_home_keeps_her_states_time():
    owner = datetime(2026, 11, 20, 21, 0, tzinfo=EASTERN)
    assert her_local(owner, us("Colorado")).hour == 19
    assert her_local(owner, us("California")).hour == 18
    assert her_local(owner, us("Hawaii")).hour == 16
    assert her_local(owner, us("Texas")).hour == 20
    assert her_local(owner, us("Ohio")).hour == 21
    assert her_local(owner, us("Colorado")).timestamp() == owner.timestamp()


def test_arizona_keeps_no_daylight_saving_time():
    summer = datetime(2026, 7, 1, 21, 0, tzinfo=EASTERN)  # EDT
    winter = datetime(2026, 1, 15, 21, 0, tzinfo=EASTERN)  # EST
    assert her_local(summer, us("Arizona")).hour == 18  # MST, three behind EDT
    assert her_local(winter, us("Arizona")).hour == 19  # two behind EST
    assert her_local(summer, us("Colorado")).hour == 19  # MDT
    assert her_local(winter, us("Colorado")).hour == 19  # MST


def test_the_night_the_clocks_change():
    # 2026-03-08: the US springs forward at 02:00 local; 06:30 UTC is 01:30 EST, 23:30 PST (the
    # 7th), and an hour later 03:30 EDT but still 00:30 PST in California
    before = datetime(2026, 3, 8, 6, 30, tzinfo=timezone.utc)
    after = datetime(2026, 3, 8, 8, 30, tzinfo=timezone.utc)
    assert her_local(before, us("California")).strftime("%d %H:%M") == "07 22:30"
    assert her_local(after, us("California")).strftime("%d %H:%M") == "08 00:30"
    assert her_local(after, us("Ohio")).strftime("%H:%M") == "04:30"  # already EDT


def test_a_uk_member_in_the_weeks_the_clocks_differ():
    # 2026-03-16: the US is on daylight time (from 03-08), Britain not yet (03-29): four hours
    owner = datetime(2026, 3, 16, 20, 0, tzinfo=EASTERN)
    assert her_local(owner, uk()).hour == 0
    assert her_local(owner, uk("nurse in Glasgow")).hour == 0
    # 2026-04-15, both on summer time: five hours, as local_time said before
    owner = datetime(2026, 4, 15, 20, 0, tzinfo=EASTERN)
    assert her_local(owner, uk("nurse in Glasgow")).hour == 1
    assert her_local(owner, uk()) == local_time(owner, "UK")


def test_naive_owner_time_is_eastern_and_stays_naive():
    naive = datetime(2026, 11, 20, 21, 0)
    got = her_local(naive, us("Colorado"))
    assert got.tzinfo is None and got.hour == 19
    assert her_local(naive, uk()) == local_time(naive, "UK")


def test_without_a_home_or_with_geography_off_nothing_changes():
    owner = datetime(2026, 11, 20, 21, 0, tzinfo=EASTERN)
    assert her_local(owner, us()) == owner  # no home: the owner's clock, as before
    geo_config.pin(False)
    try:
        assert her_local(owner, us("California")) == owner
        assert her_local(owner, us("California"), enabled=True).hour == 18  # the console's read
    finally:
        geo_config.pin(True)
    assert her_local(owner, us("California"), enabled=False) == owner


def test_presence_follows_her_own_clock():
    """At 21:00 on the owner's (Eastern) clock a Hawaii member is mid-afternoon at work on a
    weekday, while today's clock had her in her evening."""
    owner = datetime(2026, 11, 18, 21, 0, tzinfo=EASTERN)  # a Wednesday
    hawaii = us("Hawaii")
    there = presence.presence_of(hawaii, local=her_local(owner, hawaii), body_online=False,
                                 body_zone=0, rng_seed=0)
    before = presence.presence_of(hawaii, local=local_time(owner, "US"), body_online=False,
                                  body_zone=0, rng_seed=0)
    assert before.state == "online" and there.state in ("idle", "dnd")


def test_owner_zone_is_the_naive_default():
    assert OWNER_ZONE.key == "America/New_York"
