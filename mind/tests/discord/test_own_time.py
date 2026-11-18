"""Replies on her own time ([talk] dm_own_time; discord/owntime.py): a member answers his DM when
her Discord presence says she would see it, and says she is only now catching up. Invented
members only; the test cast lives in the US (America/New_York), not night owls."""
from dataclasses import replace
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from discord.test_discord_talk import FakeArbiter, answers, post, rig_for, talk_for
from leeroy_mind.discord import owntime
from leeroy_mind.discord.talk import ANSWERED_KEY, DEFERRED_KEY, OWED_TICK_S
from leeroy_mind.testing.fake_clock import FakeClock

NY = ZoneInfo("America/New_York")


@pytest.fixture(autouse=True)
def _no_cap(monkeypatch):
    """Her own time as it is without the cap ([life] dm_wait_cap_s = 0): the cap's own tests
    are in test_dm_wait_cap.py."""
    from leeroy_mind.life import config as life_config
    monkeypatch.setattr(life_config, "dm_wait_cap", lambda: 0)


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
    store.set_meta(DEFERRED_KEY.format(guid=101), str(mid))  # deferred, then never answered
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


def test_a_photo_dm_is_answered_once(store, tmp_path):
    """The tick answers only a post she deferred, so never one the desk has not handed over
    yet (a photo's look runs first), and the hand-over answers it once."""
    rig = rig_for(store, at(2026, 10, 30, 3), tmp_path)  # asleep: her answer is deferred
    own_time(rig)
    talk = talk_for(rig)
    first = post(rig, "dm:101", "did you ever find that recipe")
    hand(rig, talk, first)  # deferred
    photo = post(rig, "dm:101", "look at this")  # its look is running: not handed over yet
    run_for(rig, talk, 5 * 3600)  # morning: her deferred answer is due, the photo is not owed
    assert answers(rig, "dm:101", photo) == [] and answers(rig, "dm:101", first) == []
    hand(rig, talk, photo)  # the look is done: answered (or deferred) once, covering both
    run_for(rig, talk, 1800)
    assert len(answers(rig, "dm:101", photo)) == 1


def test_her_answer_to_an_earlier_post_is_no_answer_to_a_later_one(store, tmp_path):
    from discord.test_discord_talk import member_post
    rig = rig_for(store, at(2026, 10, 30, 3), tmp_path)
    own_time(rig)
    talk = talk_for(rig)
    first = post(rig, "dm:101", "did you ever find that recipe")
    second = post(rig, "dm:101", "the soup one")
    member_post(rig, 101, "dm:101", "oh yes", reply_to=first)  # landed after his second post
    store.set_meta(DEFERRED_KEY.format(guid=101), str(second))  # she deferred his second
    assert [m for _, m, _ in talk._owed()] == [second]


def test_switching_it_off_answers_what_it_deferred(store, tmp_path):
    rig = rig_for(store, at(2026, 10, 30, 3), tmp_path)
    own_time(rig)
    talk = talk_for(rig)
    mid = post(rig, "dm:101", "did you ever find that recipe")
    hand(rig, talk, mid)
    run_for(rig, talk, 120)
    assert answers(rig, "dm:101", mid) == []
    own_time(rig, on=False)
    run_for(rig, talk, 200)
    assert len(answers(rig, "dm:101", mid)) == 1


def test_what_she_deferred_survives_a_restart_with_the_switch_off(store, tmp_path):
    rig = rig_for(store, at(2026, 10, 30, 3), tmp_path)
    own_time(rig)
    mid = post(rig, "dm:101", "did you ever find that recipe")
    hand(rig, talk_for(rig), mid)
    own_time(rig, on=False)
    again = talk_for(rig, seed=2)  # a restart: nothing in memory
    run_for(rig, again, 200)
    assert len(answers(rig, "dm:101", mid)) == 1
    assert store.get_meta(DEFERRED_KEY.format(guid=101)) is None


def test_turning_it_on_never_answers_old_dms_she_let_be(store, tmp_path):
    rig = rig_for(store, at(2026, 10, 30, 3), tmp_path)
    old = post(rig, "dm:101", "an old one she never answered")
    own_time(rig)
    talk = talk_for(rig)
    run_for(rig, talk, 8 * 3600)
    assert answers(rig, "dm:101", old) == [] and not talk.owes(101)


def test_off_writes_nothing(store, tmp_path):
    rig = rig_for(store, at(2026, 10, 29, 20), tmp_path)
    talk = talk_for(rig)
    mid = post(rig, "dm:101", "you around tonight?")
    hand(rig, talk, mid)
    run_for(rig, talk, 200)
    assert store.get_meta(ANSWERED_KEY.format(guid=101)) is None


def test_waking_reads_her_clock_across_the_daylight_saving_change(store, tmp_path):
    from datetime import timedelta, timezone
    rig = rig_for(store, at(2026, 11, 1, 1), tmp_path)  # US clocks go back at 02:00 tonight
    p = rig.engine.personas.get(101)
    edt = datetime(2026, 11, 1, 1, 0, tzinfo=timezone(timedelta(hours=-4)))  # SystemClock-like
    now = edt.timestamp()
    assert owntime.state_at(p, edt) == "offline"
    due = owntime.due_at(p, post_ts=now, now=now, local_now=edt, message_id=9,
                         urgent=False, talking=False, insist=False)
    woke = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)  # 07:00 EST, not 07:00 EDT
    assert woke.timestamp() + owntime.BACK_S[0] <= due <= woke.timestamp() + owntime.BACK_S[1]


def test_a_lost_late_answer_is_tried_again_then_let_go(store, tmp_path, caplog):
    """Owed until her answer is stored: a queued turn that comes to nothing (a restart, a drop)
    is queued again after RETRY_S, and let go after TRIES."""
    from leeroy_mind.discord.talk import RETRY_S, TRIES
    rig = rig_for(store, at(2026, 10, 30, 3), tmp_path)
    own_time(rig)
    talk = talk_for(rig)
    caplog.set_level("INFO", logger="leeroy_mind.discord")
    mid = post(rig, "dm:101", "are you up? need help with something")
    hand(rig, talk, mid)
    queued = []
    rig.engine._queue = lambda items: queued.append(items)  # every turn is lost
    run_for(rig, talk, owntime.URGENT_S[1] + 60)
    assert len(queued) == 1 and talk.owes(101)
    run_for(rig, talk, RETRY_S)
    assert len(queued) == TRIES
    run_for(rig, talk, RETRY_S)
    assert len(queued) == TRIES and not talk.owes(101)
    assert "discord_dm_let_go: guid=101" in caplog.text


def life(rig, availability=6):
    """[life] availability through the engine's own config, as the mind applies mind.toml."""
    cfg = rig.engine.cfg
    rig.engine.apply_config(replace(cfg, life=replace(cfg.life, availability=availability)))


@pytest.mark.parametrize("hour,away", [(12, True), (20, False)])
def test_life_defers_a_dm_only_while_away(store, tmp_path, caplog, hour, away):
    from leeroy_mind.life.day import Block, block_at
    rig = rig_for(store, at(2026, 11, 18, hour), tmp_path)  # a Wednesday, her own clock (US)
    life(rig)
    assert rig.engine.cfg.talk.dm_own_time is False
    talk = talk_for(rig)
    assert talk.own_time()
    block = block_at(rig.engine.personas.get(101), datetime(2026, 11, 18, hour))[0]
    assert (block is Block.WORK) if away else block in (Block.FREE, Block.GAMING)
    caplog.set_level("INFO", logger="leeroy_mind.discord")
    mid = post(rig, "dm:101", "the kettle finally died this morning")
    hand(rig, talk, mid)
    run_for(rig, talk, 200)
    if away:
        assert answers(rig, "dm:101", mid) == []
        assert store.get_meta(DEFERRED_KEY.format(guid=101)) == str(mid)
        assert "discord_dm_later: guid=101" in caplog.text and talk.owes(101)
        run_for(rig, talk, owntime.PHONE_S["dnd"][1] + 120)  # she glances at her phone
        assert len(answers(rig, "dm:101", mid)) == 1
        assert "discord_dm_answered_later: guid=101" in caplog.text
        note = talk.instruction(101, "dm:101", mid, None)
        assert "ago while you were at work" in note and "only now seeing it" in note
    else:
        assert len(answers(rig, "dm:101", mid)) == 1
        assert store.get_meta(DEFERRED_KEY.format(guid=101)) is None
        assert "discord_dm_later" not in caplog.text
    assert "the kettle finally died" not in caplog.text


@pytest.mark.parametrize("hour", [3, 12, 20])
def test_availability_zero_and_own_time_off_answers_at_once(store, tmp_path, hour):
    rig = rig_for(store, at(2026, 11, 18, hour), tmp_path)
    life(rig, 0)
    talk = talk_for(rig)
    assert not talk.own_time()
    mid = post(rig, "dm:101", "the kettle finally died this morning")
    hand(rig, talk, mid)
    run_for(rig, talk, 200)
    assert len(answers(rig, "dm:101", mid)) == 1
    assert store.get_meta(DEFERRED_KEY.format(guid=101)) is None
    assert store.get_meta(ANSWERED_KEY.format(guid=101)) is None


def in_his_group(rig, *guids):
    """He plays, and `guids` are in his WoW group (presence: online and playing)."""
    from leeroy_mind.protocol import Group
    rig.world.human_online = True
    rig.world.group = Group(ts=0, leader_guid=1, raid=False, members=(1, *guids))


def test_in_his_group_she_answers_his_dm_at_once_whatever_her_schedule(store, tmp_path, caplog):
    from leeroy_mind.life.day import Block, block_at
    rig = rig_for(store, at(2026, 11, 18, 2), tmp_path)  # 02:00 for the US cast: asleep
    life(rig)
    in_his_group(rig, 101)
    talk = talk_for(rig)
    assert block_at(rig.engine.personas.get(101), datetime(2026, 11, 18, 2))[0] is Block.ASLEEP
    caplog.set_level("INFO", logger="leeroy_mind.discord")
    mine = post(rig, "dm:101", "the kettle finally died this morning")
    hand(rig, talk, mine)
    other = post(rig, "dm:102", "the kettle finally died this morning")
    hand(rig, talk, other)
    run_for(rig, talk, 200)
    assert len(answers(rig, "dm:101", mine)) == 1
    assert store.get_meta(DEFERRED_KEY.format(guid=101)) is None
    assert "discord_dm_later: guid=101" not in caplog.text
    assert answers(rig, "dm:102", other) == []  # not with him: her schedule still holds her
    assert store.get_meta(DEFERRED_KEY.format(guid=102)) == str(other)


def test_an_edit_to_urgent_words_reads_the_moment_again(store, tmp_path):
    from leeroy_mind.discord import store as dstore
    rig = rig_for(store, at(2026, 10, 30, 3), tmp_path)
    own_time(rig)
    talk = talk_for(rig)
    mid = post(rig, "dm:101", "did you ever find that recipe")
    hand(rig, talk, mid)
    run_for(rig, talk, 120)
    assert answers(rig, "dm:101", mid) == []
    dstore.edit(store, mid, author=1, text="are you up? need help with something",
                now=rig.clock.now())
    talk.on_his_action()
    run_for(rig, talk, owntime.URGENT_S[1] + 200)
    assert len(answers(rig, "dm:101", mid)) == 1
