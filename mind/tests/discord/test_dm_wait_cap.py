"""His DMs are answered promptly whatever the dial ([life] dm_wait_cap_s; discord/owntime.py,
owner 2026-11-17): the wait for her own time is capped for idle and dnd members, twice that for
one asleep, she still says where she was, and a group DM line that names her is capped too.
Invented members only; the test cast lives in the US."""
from dataclasses import replace
from datetime import datetime

import pytest

from discord.test_discord_talk import answers, post, rig_for, talk_for
from discord.test_own_time import at, hand, life, own_time, run_for
from leeroy_mind.discord import owntime
from leeroy_mind.discord.talk import DEFERRED_KEY, OWED_TICK_S
from leeroy_mind.life import config as life_config


def capped(rig, cap=75, availability=6):
    cfg = rig.engine.cfg
    rig.engine.apply_config(replace(cfg, life=replace(cfg.life, availability=availability,
                                                      dm_wait_cap_s=cap)))


def test_the_default_is_75_seconds_and_the_engine_pins_it(store, tmp_path):
    rig = rig_for(store, at(2026, 11, 18, 12), tmp_path)
    assert rig.engine.cfg.life.dm_wait_cap_s == 75
    assert life_config.dm_wait_cap() == 75  # pinned at engine start, before any reload
    assert life_config.stored_cap(store.conn) == 75  # and written for health
    capped(rig, cap=0)
    assert life_config.dm_wait_cap() == 0 and life_config.stored_cap(store.conn) == 0


@pytest.mark.parametrize("hour,asleep", [(11, False), (3, True)])
def test_due_at_is_capped_for_idle_dnd_and_asleep(store, tmp_path, hour, asleep):
    rig = rig_for(store, at(2026, 10, 30, hour), tmp_path)
    p = rig.engine.personas.get(101)
    now, local = rig.clock.now(), rig.clock.local()
    state = owntime.state_at(p, local)
    assert (state == "offline") if asleep else state in owntime.PHONE_S
    args = dict(post_ts=now, now=now, local_now=local, urgent=False, talking=False,
                insist=False)
    life_config.pin(0, False, 0)
    free = [owntime.due_at(p, message_id=m, **args) - now for m in range(40)]
    assert max(free) > 300  # without the cap she takes her time
    life_config.pin(0, False, 75)
    cap = 150.0 if asleep else 75.0
    waits = [owntime.due_at(p, message_id=m, **args) - now for m in range(40)]
    assert all(w <= cap for w in waits)
    assert all(w >= cap * owntime.CAP_SPAN[0] or w == f for w, f in zip(waits, free))
    assert len({round(w) for w in waits}) > 5  # drawn, never one fixed second
    # the same post is due at the same moment after a restart, and a wait already under the
    # cap is her own
    assert waits == [owntime.due_at(p, message_id=m, **args) - now for m in range(40)]
    assert all(w == f for w, f in zip(waits, free) if f <= cap)
    assert owntime.due_at(p, message_id=1, capped=False, **args) - now == free[1]


def test_online_she_still_answers_at_once(store, tmp_path):
    rig = rig_for(store, at(2026, 10, 29, 20), tmp_path)
    life_config.pin(0, False, 75)
    p, now = rig.engine.personas.get(101), rig.clock.now()
    assert owntime.cap_for("online") == 0.0
    assert owntime.due_at(p, post_ts=now, now=now, local_now=rig.clock.local(), message_id=3,
                          urgent=False, talking=False, insist=False) == now


@pytest.mark.parametrize("hour,limit,where", [(12, 75, "at work"), (3, 150, "asleep")])
def test_his_dm_is_answered_within_the_cap_and_she_says_where_she_was(store, tmp_path, caplog,
                                                                     hour, limit, where):
    rig = rig_for(store, at(2026, 11, 18, hour), tmp_path)  # a Wednesday, her own clock (US)
    capped(rig)
    talk = talk_for(rig)
    caplog.set_level("INFO", logger="leeroy_mind.discord")
    mid = post(rig, "dm:101", "the kettle finally died this morning")
    hand(rig, talk, mid)
    posted = rig.clock.now()
    if store.get_meta(DEFERRED_KEY.format(guid=101)) is not None:
        assert "discord_dm_later: guid=101" in caplog.text
    run_for(rig, talk, limit + OWED_TICK_S + 30)  # the tick's step and her typing
    got = answers(rig, "dm:101", mid)
    assert len(got) == 1
    assert got[0]["ts"] - posted <= limit + OWED_TICK_S + 30
    if "discord_dm_answered_later" in caplog.text:  # it waited for her: she says so
        row = store.conn.execute("SELECT ts FROM discord_message WHERE id = ?", (mid,)).fetchone()
        note = owntime.late_note(rig.engine.personas.get(101), post_ts=row[0],
                                 now=row[0] + 60, local_now=rig.clock.local(),
                                 forms=rig.engine.human.forms, after=owntime.LATE_OWN_S)
        assert "a minute ago while you were" in note and "only now seeing it" in note
    assert "the kettle finally died" not in caplog.text


def test_with_no_cap_her_day_decides_as_before(store, tmp_path):
    rig = rig_for(store, at(2026, 11, 18, 12), tmp_path)
    capped(rig, cap=0)
    talk = talk_for(rig)
    mids = [post(rig, "dm:101", f"kettle update {n}") for n in range(1)]
    hand(rig, talk, mids[0])
    p, now = rig.engine.personas.get(101), rig.clock.now()
    due = talk._due_of(101, mids[0], now)
    assert due == owntime.due_at(p, post_ts=now, now=now, local_now=rig.clock.local(),
                                 message_id=mids[0], urgent=False, talking=False, insist=False,
                                 capped=False)


def test_the_late_note_starts_at_45_seconds_for_an_answer_that_waited(store, tmp_path):
    rig = rig_for(store, at(2026, 11, 18, 12), tmp_path)
    capped(rig)
    talk = talk_for(rig)
    mid = post(rig, "dm:101", "the kettle finally died this morning")

    async def go():
        await rig.advance(50)
    rig.sync(go())
    assert "only now" not in talk.instruction(101, "dm:101", mid, None)  # answered at once: none
    waited = talk.instruction(101, "dm:101", mid, None, waited=True)
    assert "wrote this a minute ago while you were at work" in waited
    assert "only now seeing it" in waited and waited.count("only now") == 1
    assert "only now" not in talk.instruction(101, "dm:101", mid, None, away="shape",
                                              waited=True)  # the LATE shape says it instead


def test_stepped_away_she_is_back_within_the_cap(store, tmp_path):
    """His DM while she stepped away mid-conversation (a brb of up to 45 minutes): due within
    the cap, not when her away ends."""
    from leeroy_mind.life.interrupt import Away
    rig = rig_for(store, at(2026, 11, 18, 20), tmp_path)  # evening: online, in a conversation
    capped(rig)
    talk = talk_for(rig)
    now = rig.clock.now()
    rig.engine.life._away[101] = Away(since=now - 5, until=now + 1800, channel="dm:101",
                                      reason="work", night="n", minute=0)
    mid = post(rig, "dm:101", "so what happened with the vendor")
    due = talk._due_of(101, mid, now)
    assert now < due <= now + 150
    life_config.pin(6, False, 0)
    talk._due.clear()
    assert talk._due_of(101, mid, now) >= now + 1800  # without the cap: when she is back


def test_health_bound_follows_the_cap(store, tmp_path):
    from leeroy_mind.health import OWNER_LATE_MARGIN_S, _own_time_due
    rig = rig_for(store, at(2026, 11, 18, 3), tmp_path)  # asleep
    capped(rig)
    mid = post(rig, "dm:101", "the kettle finally died this morning")
    ts = store.conn.execute("SELECT ts FROM discord_message WHERE id = ?", (mid,)).fetchone()[0]
    due = _own_time_due(store.conn, 1, rig.clock.now(), 6)
    assert due("dm:101", mid, ts) <= ts + 150 + OWNER_LATE_MARGIN_S
    capped(rig, cap=0)
    assert _own_time_due(store.conn, 1, rig.clock.now(), 6)("dm:101", mid, ts) > ts + 3600
    assert life_config.dm_wait_cap() == 0  # health puts the mind's pin back
