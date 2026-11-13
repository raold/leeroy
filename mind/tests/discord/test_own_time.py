"""Replies on her own time ([talk] dm_own_time; discord/owntime.py): a member answers his DM when
her Discord presence says she would see it, and says she is only now catching up. Invented
members only; the test cast lives in the US (America/New_York), not night owls."""
from dataclasses import replace
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from discord.test_discord_talk import FakeArbiter, answers, post, rig_for, talk_for
from leeroy_mind.discord import owntime
from leeroy_mind.discord.talk import ANSWERED_KEY, OWED_TICK_S
from leeroy_mind.testing.fake_clock import FakeClock

NY = ZoneInfo("America/New_York")


def at(year, month, day, hour, minute=0):
    return FakeClock(datetime(year, month, day, hour, minute, tzinfo=NY), yields=64)


def own_time(rig, on=True):
    cfg = rig.engine.cfg
    rig.engine.cfg = replace(cfg, talk=replace(cfg.talk, dm_own_time=on))


def hand(rig, talk, mid):
    """His post handed to the talk, as the desk does, inside the mind's loop."""
    async def go():
        talk.on_his_post(mid)
    rig.sync(go())


def run_for(rig, talk, seconds, step=OWED_TICK_S):
    """`seconds` of the mind's ticker: the owed DMs looked at every OWED_TICK_S."""
    async def go():
        left = seconds
        while left > 0:
            await rig.advance(min(step, left))
            left -= step
            talk.tick(rig.clock.now())
    rig.sync(go())


def test_asleep_she_answers_after_she_wakes_and_says_she_is_catching_up(store, tmp_path, caplog):
    rig = rig_for(store, at(2026, 10, 30, 3), tmp_path)  # a Friday, 03:00: asleep
    own_time(rig)
    talk = talk_for(rig)
    caplog.set_level("INFO", logger="leeroy_mind.discord")
    mid = post(rig, "dm:101", "did you ever find that recipe")
    hand(rig, talk, mid)
    run_for(rig, talk, 600)
    assert answers(rig, "dm:101", mid) == []
    assert talk.owes(101) and not talk.owes(102)
    assert "discord_dm_later: guid=101" in caplog.text
    run_for(rig, talk, 4 * 3600 + 20 * 60)  # 07:20: awake since 07:00, phone checked
    assert len(answers(rig, "dm:101", mid)) == 1
    assert not talk.owes(101)
    assert "discord_dm_answered_later: guid=101" in caplog.text
    assert "did you ever find" not in caplog.text  # logs carry ids, never words
    note = talk.instruction(101, "dm:101", mid, None)
    assert "ago while you were asleep" in note and "only now seeing it" in note


def test_with_the_switch_off_she_answers_at_once_as_before(store, tmp_path):
    rig = rig_for(store, at(2026, 10, 30, 3), tmp_path)
    talk = talk_for(rig)
    mid = post(rig, "dm:101", "did you ever find that recipe")
    hand(rig, talk, mid)
    run_for(rig, talk, 200)
    assert len(answers(rig, "dm:101", mid)) == 1
    assert "only now" not in (talk.instruction(101, "dm:101", mid, None) or "")


def test_online_she_answers_at_once(store, tmp_path):
    rig = rig_for(store, at(2026, 10, 29, 20), tmp_path)  # Thursday evening: online
    own_time(rig)
    talk = talk_for(rig)
    mid = post(rig, "dm:101", "you around tonight?")
    hand(rig, talk, mid)
    run_for(rig, talk, 200)
    assert len(answers(rig, "dm:101", mid)) == 1


def test_urgent_wakes_her(store, tmp_path):
    rig = rig_for(store, at(2026, 10, 30, 3), tmp_path)
    own_time(rig)
    talk = talk_for(rig)
    mid = post(rig, "dm:101", "are you up? need help with something")
    hand(rig, talk, mid)
    run_for(rig, talk, owntime.URGENT_S[1] + 200)
    assert len(answers(rig, "dm:101", mid)) == 1


def test_a_conversation_keeps_her_answering(store, tmp_path):
    rig = rig_for(store, at(2026, 10, 30, 3), tmp_path)
    own_time(rig)
    talk = talk_for(rig)
    first = post(rig, "dm:101", "are you up? need help with something")
    hand(rig, talk, first)
    run_for(rig, talk, owntime.URGENT_S[1] + 200)
    assert answers(rig, "dm:101", first)
    second = post(rig, "dm:101", "thanks, that sorted it")  # not urgent, but they are talking
    hand(rig, talk, second)
    run_for(rig, talk, 200)
    assert len(answers(rig, "dm:101", second)) == 1


def test_a_late_answer_waits_for_a_lease_and_never_wakes_the_night(store, tmp_path):
    rig = rig_for(store, at(2026, 10, 30, 3), tmp_path)
    own_time(rig)
    arbiter = FakeArbiter()
    talk = talk_for(rig, arbiter=arbiter)
    mid = post(rig, "dm:101", "did you ever find that recipe")
    hand(rig, talk, mid)  # his post takes his Discord lease, released LEASE_IDLE_S later
    run_for(rig, talk, 5 * 3600)  # 08:00: she is up, but nothing holds the live model
    assert not arbiter.holders and answers(rig, "dm:101", mid) == []
    arbiter.hold("server")
    run_for(rig, talk, 300)
    assert len(answers(rig, "dm:101", mid)) == 1


def test_an_old_dm_is_let_go(store, tmp_path, caplog):
    rig = rig_for(store, at(2026, 10, 30, 3), tmp_path)
    own_time(rig)
    talk = talk_for(rig)
    caplog.set_level("INFO", logger="leeroy_mind.discord")
    mid = post(rig, "dm:101", "did you ever find that recipe")
    store.conn.execute("UPDATE discord_message SET ts = ? WHERE id = ?",
                       (rig.clock.now() - owntime.LET_GO_S - 60, mid))
    talk.tick(rig.clock.now())
    assert store.get_meta(ANSWERED_KEY.format(guid=101)) == str(mid)
    assert "discord_dm_let_go: guid=101" in caplog.text and not talk.owes(101)


def test_the_moment_survives_a_restart(store, tmp_path):
    rig = rig_for(store, at(2026, 10, 30, 3), tmp_path)
    own_time(rig)
    mid = post(rig, "dm:101", "did you ever find that recipe")
    ts = store.conn.execute("SELECT ts FROM discord_message WHERE id = ?", (mid,)).fetchone()[0]
    first, second = talk_for(rig, seed=1), talk_for(rig, seed=2)
    assert first._due_of(101, mid, ts) == second._due_of(101, mid, ts)


@pytest.mark.parametrize("hour,minute,state,lo,hi", [
    (3, 0, "offline", 4 * 3600 + owntime.BACK_S[0], 4 * 3600 + owntime.BACK_S[1]),
    (20, 0, "online", 0, 0),
])
def test_due_at_by_presence(store, tmp_path, hour, minute, state, lo, hi):
    rig = rig_for(store, at(2026, 10, 30, hour, minute), tmp_path)
    p = rig.engine.personas.get(101)
    now = rig.clock.now()
    assert owntime.state_at(p, rig.clock.local()) == state
    due = owntime.due_at(p, post_ts=now, now=now, local_now=rig.clock.local(), message_id=7,
                         urgent=False, talking=False, insist=False)
    assert lo <= due - now <= hi


def test_at_work_she_glances_at_her_phone(store, tmp_path):
    rig = rig_for(store, at(2026, 10, 30, 11), tmp_path)  # a weekday at 11:00
    p = rig.engine.personas.get(101)
    now, local = rig.clock.now(), rig.clock.local()
    state = owntime.state_at(p, local)
    assert state in owntime.PHONE_S
    due = owntime.due_at(p, post_ts=now, now=now, local_now=local, message_id=7,
                         urgent=False, talking=False, insist=False)
    lo, hi = owntime.PHONE_S[state]
    assert lo <= due - now <= hi
    insisted = owntime.due_at(p, post_ts=now, now=now, local_now=local, message_id=7,
                              urgent=False, talking=False, insist=True)
    assert insisted - now <= owntime.URGENT_S[1]
    assert "at work" in owntime.late_note(p, post_ts=now - 3600, now=now, local_now=local,
                                          forms=("he", "him", "his"))


def test_urgent_words():
    for text in ("are you up?", "u up?", "need help asap", "call me", "emergency", "you there?"):
        assert owntime.URGENT_RE.search(text), text
    for text in ("did you ever find that recipe", "help with the raid tomorrow would be nice"):
        assert not owntime.URGENT_RE.search(text), text


def test_a_whisper_with_him_counts_as_talking(store, tmp_path):
    from leeroy_mind.protocol import Chat
    rig = rig_for(store, at(2026, 10, 30, 3), tmp_path)  # up late in the game with him
    own_time(rig)
    talk = talk_for(rig)
    lid = store.insert_chat_line(Chat(0, "whisper", 101, "Invented", 1, "still up?", 0, 10),
                                 session_id=None, origin="mind")
    store.conn.execute("UPDATE chat_line SET ts = ? WHERE id = ?", (rig.clock.now() - 120, lid))
    mid = post(rig, "dm:101", "did you ever find that recipe")
    hand(rig, talk, mid)
    run_for(rig, talk, 200)
    assert len(answers(rig, "dm:101", mid)) == 1


def test_dm_openers_wait_while_she_owes_him_an_answer(store, tmp_path):
    from types import SimpleNamespace
    from leeroy_mind.server.dms import DmOpeners
    rig = rig_for(store, at(2026, 10, 30, 3), tmp_path)
    own_time(rig)
    talk = talk_for(rig)
    mid = post(rig, "dm:101", "did you ever find that recipe")
    hand(rig, talk, mid)
    openers = DmOpeners.__new__(DmOpeners)
    openers.app = SimpleNamespace(discord_talk=talk, store=store, human_guid=1)
    openers._human_until = {}
    now = rig.clock.now()
    assert not openers._can_open(101, now, "2026-10-30", {})
    assert openers._can_open(102, now, "2026-10-30", {})
