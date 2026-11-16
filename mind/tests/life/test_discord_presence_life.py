"""Discord presence from her day (`[life] availability` above 0): her state, playing, the status
line's blocks and the own-time waits; at 0 exactly today's presence. Invented members only."""
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from conftest import make_persona
from leeroy_mind.discord import owntime, presence
from leeroy_mind.life import config as life_config
from leeroy_mind.life.day import Block, day_plan


def grid():
    start = datetime(2026, 11, 16, 0, 0)
    return [start + timedelta(minutes=15 * i) for i in range(7 * 96)]


# computed once on main's presence.py (da55102e) before this task changed it
TODAY = "592555a847d3b11dbb873bc8e84b320edd726139faba3757d5535898db93570f"


def test_availability_zero_is_todays_presence():
    import hashlib
    life_config.pin(0, False)
    seen = "".join(presence.presence_of(make_persona(101, "Arkon", night_owl=owl), local=t,
                                        body_online=True, body_zone=1, rng_seed=0).state[:2]
                   for owl in (False, True) for t in grid())
    assert hashlib.sha256(seen.encode()).hexdigest() == TODAY


def test_her_day_drives_state():
    p = make_persona(101, "Arkon", rhythm="weekday desk hours")
    life_config.pin(6, False)
    at = lambda h: presence.presence_of(p, local=datetime(2026, 11, 19, h), body_online=True,
                                        body_zone=1, rng_seed=0).state
    assert at(3) == "offline" and at(12) in ("idle", "dnd")


def test_uk_member_on_the_split_dst_week():   # Review Focus 2
    p = make_persona(101, "Arkon", rhythm="weekday desk hours")  # UK, wake 07:30, commute 08:15
    life_config.pin(6, False)
    ny = ZoneInfo("America/New_York")
    split = datetime(2026, 10, 28, 4, 30, tzinfo=ny)          # UK 08:30 (offset 4 h)
    normal = datetime(2026, 11, 4, 4, 30, tzinfo=ny)          # UK 09:30 (offset 5 h)
    blk = lambda t: presence.local_time(t, "UK")
    assert (blk(split).hour, blk(normal).hour) == (8, 9)
    s = lambda t: presence.presence_of(p, local=presence.local_time(t, "UK"), body_online=False,
                                       body_zone=0, rng_seed=0).state
    assert s(split) == "idle" and s(normal) in ("idle", "dnd")   # commute, then work


def _minute_in(p, day, block):
    plan = day_plan(p, day)
    span = next(s for s in plan.spans if s.block is block)
    return datetime(day.year, day.month, day.day) + timedelta(minutes=span.start)


def _first(block):
    """A member of the busy cast (BUSY_LIFE) and the first minute of `block` in her week."""
    for g in range(100, 160):
        p = make_persona(g, "Arkon", **BUSY_LIFE)
        for d in range(16, 23):
            if block in {s.block for s in day_plan(p, date(2026, 11, d)).spans}:
                return p, _minute_in(p, date(2026, 11, d), block)
    raise AssertionError(block)


def test_the_state_follows_each_block():
    p = make_persona(101, "Arkon", rhythm="weekday desk hours", night_owl=False)
    life_config.pin(6, False)
    day = date(2026, 11, 18)
    plan = day_plan(p, day)
    seen = {}
    for span in plan.spans:
        t = datetime(2026, 11, 18) + timedelta(minutes=span.start)
        seen[span.block] = presence.presence_of(p, local=t, body_online=False, body_zone=0,
                                                rng_seed=0).state
    want = {Block.ASLEEP: {"offline"}, Block.WORK: {"idle", "dnd"}, Block.COMMUTE: {"idle"},
            Block.FREE: {"online"}, Block.GAMING: {"online"}}
    for block, states in want.items():
        if block in seen:
            assert seen[block] in states, block


def test_playing_needs_online_her_body_and_a_gaming_block():
    p = make_persona(101, "Arkon", rhythm="weekday desk hours")
    life_config.pin(6, False)
    for d in range(16, 23):
        plan = day_plan(p, date(2026, 11, d))
        for span in plan.spans:
            t = datetime(2026, 11, d) + timedelta(minutes=span.start)
            on = presence.presence_of(p, local=t, body_online=True, body_zone=3, rng_seed=0)
            off = presence.presence_of(p, local=t, body_online=False, body_zone=3, rng_seed=0)
            assert on.playing == (span.block is Block.GAMING and on.state == "online")
            assert not off.playing and on.zone == 3
    asleep = datetime(2026, 11, 19, 3)
    assert presence.presence_of(p, local=asleep, body_online=True, body_zone=4, rng_seed=0,
                                in_his_group=True) == presence.Presence("online", True, 4)


def test_status_text_names_her_block():
    life_config.pin(6, False)
    phrase = {"work": set(presence.STATUS_PHRASES["work"]),
              "commute": set(presence.STATUS_PHRASES["commute"]),
              "home": set(presence.STATUS_PHRASES["home"]),
              "gym": {"at the gym"}, "out": {"out tonight"}}
    seen = set()
    for g in range(100, 160):
        p = make_persona(g, "Arkon", rhythm="weekday desk hours", household="two kids at home",
                         life_thread="training at the climbing wall")
        for d in range(16, 23):
            plan = day_plan(p, date(2026, 11, d))
            for span in plan.spans:
                t = datetime(2026, 11, d) + timedelta(minutes=span.start + 1)
                pr = presence.presence_of(p, local=t, body_online=False, body_zone=0, rng_seed=0)
                text = presence.status_text(p, t, pr, rng_seed=0)
                if text is None:
                    continue
                key = {Block.WORK: "work", Block.COMMUTE: "commute", Block.HOME_DUTY: "home",
                       Block.GYM: "gym", Block.OUT: "out"}.get(span.block)
                if key is not None:
                    assert text in phrase[key], (span.block, text)
                    seen.add(key)
                else:   # free and gaming: the clock's evening and late phrases, never work
                    assert span.block in (Block.FREE, Block.GAMING)
                    assert text in (presence.STATUS_PHRASES["evening"]
                                    + presence.STATUS_PHRASES["late"]), (span.block, text)
    assert seen == set(phrase)


def test_status_text_at_zero_is_todays():
    p = make_persona(101, "Arkon")
    life_config.pin(0, False)
    texts = []
    for t in grid():
        pr = presence.presence_of(p, local=t, body_online=False, body_zone=0, rng_seed=0)
        texts.append(presence.status_text(p, t, pr, rng_seed=0))
    assert "kids' bedtime" not in texts and "out tonight" not in texts
    assert {"at work", "in meetings", "working"} & set(texts)


def test_own_time_waits_scale_with_availability():
    p = make_persona(101, "Arkon", rhythm="weekday desk hours", region="US")
    ny = ZoneInfo("America/New_York")
    local = datetime(2026, 11, 18, 12, 0, tzinfo=ny)   # Wednesday noon: at work
    now = local.timestamp()

    def wait(a, mid):
        life_config.pin(a, False)
        return owntime.due_at(p, post_ts=now, now=now, local_now=local, message_id=mid,
                              urgent=False, talking=False, insist=False) - now
    life_config.pin(6, False)
    state = presence.presence_of(p, local=local, body_online=False, body_zone=0,
                                 rng_seed=owntime.RNG_SEED).state
    assert state in owntime.PHONE_S
    lo, hi = owntime.PHONE_S[state]
    for mid in range(1, 40):
        six, three, ten = wait(6, mid), wait(3, mid), wait(10, mid)
        assert lo <= six <= hi            # the design point keeps today's bounds
        assert three == pytest.approx(six / 2) and ten == pytest.approx(six * 10 / 6)
        assert max(30.0, lo / 6) <= wait(1, mid) <= max(30.0, hi / 6)
    assert owntime.PHONE_S == {"idle": (60.0, 1200.0), "dnd": (1200.0, 5400.0)}
    assert owntime.BACK_S == (120.0, 900.0)


# a cast with kids at home and a climbing habit, so home duty, the gym and nights out all occur
BUSY_LIFE = dict(rhythm="weekday desk hours", household="two kids at home",
                 life_thread="training at the climbing wall", region="US")
ONLINE = (Block.FREE, Block.GAMING, Block.HOME_DUTY)
AWAY = (Block.ASLEEP, Block.WORK, Block.COMMUTE, Block.GYM, Block.OUT)


@pytest.mark.parametrize("blocks,at_once", [(ONLINE, True), (AWAY, False)])
def test_his_dm_waits_only_while_she_is_away_by_her_day(blocks, at_once):
    """Availability 6: answered at once in free, gaming and home duty (Discord online), late
    only while asleep, at work, driving, at the gym or out."""
    ny = ZoneInfo("America/New_York")
    life_config.pin(6, False)
    seen = dict.fromkeys(blocks, 0)
    for g in range(100, 160):
        p = make_persona(g, "Arkon", **BUSY_LIFE)
        for d in range(16, 23):
            for span in day_plan(p, date(2026, 11, d)).spans:
                if span.block not in blocks or seen[span.block] >= 6:
                    continue
                local = datetime(2026, 11, d, tzinfo=ny) + timedelta(minutes=span.start)
                now = local.timestamp()
                due = owntime.due_at(p, post_ts=now, now=now, local_now=local,
                                     message_id=100 * g + d, urgent=False, talking=False,
                                     insist=False)
                assert due == now if at_once else due > now, (span.block, g, d)
                seen[span.block] += 1
    assert min(seen.values()) == 6, seen


@pytest.mark.parametrize("block,words", [(Block.WORK, "at work"), (Block.ASLEEP, "asleep"),
                                         (Block.COMMUTE, "driving"), (Block.GYM, "at the gym"),
                                         (Block.OUT, "out")])
def test_away_words_come_from_her_day(block, words):
    life_config.pin(6, False)
    p, local = _first(block)
    local += timedelta(minutes=1)
    assert owntime.away_words(p, local.replace(tzinfo=ZoneInfo("America/New_York"))) == words


def test_away_words_at_zero_are_todays():
    p = make_persona(101, "Arkon", region="US")
    life_config.pin(0, False)
    assert owntime.away_words(p, datetime(2026, 11, 18, 3, 0)) == "asleep"
    assert owntime.away_words(p, datetime(2026, 11, 18, 11, 0)) == "at work"
