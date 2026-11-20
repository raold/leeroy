"""What the mind fed WoW talk wrong (the blind A/B, 2026-11-20): a raid's kills over its fights,
raid nights posted at 03:00 from his after-midnight relogs, and the raid night's time said as a
bare "03:00". Invented rows only."""
from datetime import datetime, timedelta, timezone

from leeroy_mind.guild.raids import shown_kills
from leeroy_mind.server.seeds import _raid_before
from leeroy_mind.wow import raid_fights


def test_a_raids_fights():
    assert raid_fights(724) == 4   # Ruby Sanctum, never its intro steps
    assert raid_fights(533) == 15  # Naxxramas
    assert raid_fights(631) == 13  # Icecrown Citadel's fights, not its two steps
    assert raid_fights(1) == 0


def test_shown_kills_never_pass_the_fights():
    assert shown_kills("rs", 7) == 4
    assert shown_kills("naxx", 13) == 13
    assert shown_kills("unknown", 9) == 9
    assert shown_kills("rs", None) == 0 and shown_kills("rs", -1) == 0


def test_the_raid_night_seed_says_when_in_words(store):
    tz = timezone(timedelta(hours=-5))
    local = datetime(2026, 11, 20, 18, 0, tzinfo=tz)
    with store.transaction():
        store.conn.execute(
            "INSERT INTO realm_calendar(kind,creator,at,title,ref,updated,event_id,state) "
            "VALUES ('raid',102,?,'Raid night','raid:test',?,7,'posted')",
            (datetime(2026, 11, 20, 21, 0, tzinfo=tz).timestamp(), local.timestamp()))
    c = type("C", (), {"now": local.timestamp(), "local": local})()
    seeds = list(_raid_before(store.conn, c))
    assert [s.fact for s in seeds] == ["Raid night tonight at 9pm server time"]
