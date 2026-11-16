"""Game presence ([life] availability above 0, Task 7): who answers his line in game follows her
attention, how soon follows her day (late but bounded for an answer he is owed, missed for
another past NON_OWED_MAX_S), a second whisper joins the answer still waiting, urgent words, his
third line and his goodbye hurry it, and a whisper answer still waiting at his logout is carried
to her Discord DM. Availability 0 is exactly today's timing. Fakes only: FakeLLM, FakeClock, a
scratch mind.db. Invented text; the test cast's personas (UK, early shifts: asleep from 21:15 on
a weeknight)."""
import logging
import random
from dataclasses import replace
from types import SimpleNamespace

import pytest

from conftest import HUMAN, run
from engine_rig import engine_clock, make_rig, numbered, save_member
from leeroy_mind.life import config as life_config
from leeroy_mind.life.presence import Attention, game_cap_s
from leeroy_mind.life.service import CARRY_TEXT, NON_OWED_MAX_S
from leeroy_mind.life.state import State
from leeroy_mind.protocol import Group, Login, Logout
from leeroy_mind.talk.owed import OwedWatch

# main's rig (54e2f2a0) with this script and seed: (seconds after his first line, speaker,
# channel) of every Say
TODAY = [(11.0, 101, "guild"), (24.0, 103, "guild"), (34.0, 102, "guild"),
         (52.0, 102, "whisper"), (89.0, 104, "guild"), (101.0, 101, "guild"),
         (112.0, 105, "guild")]
NIGHT_S = 7200.0  # the rig starts at 20:00 UK time; 22:00 has the cast asleep (early shifts)


@pytest.fixture
def clock():
    return engine_clock()


def _watch(r):
    app = SimpleNamespace(store=r.store, clock=r.clock, engine=r.engine, world=r.world,
                          scheduler=r.engine.scheduler, discord_talk=None)
    w = OwedWatch(app)
    r.engine.owed = w
    return w


async def _tick(r, w, seconds):
    for _ in range(int(seconds)):
        await r.advance(1)
        w.tick(r.clock.now())


def _whispers_from(r, guid, since):
    return [(t, s) for t, s in r.sent.timed_says()
            if t >= since and s.guid == guid and s.channel == "whisper"]


def test_availability_zero_keeps_todays_delays(store, clock, tmp_path):
    """The same rig, script and seed at availability 0 says every line at main's times."""
    async def main():
        r = make_rig(store, clock, tmp_path, script=numbered("sure thing {n} here"), seed=5,
                     availability=0)
        assert r.engine.life.availability == 0
        await r.session()
        await r.event(Login(ts=0, guid=HUMAN, human=True))
        await r.advance(90, step=2)
        t0 = clock.now()
        await r.human("anyone up for a dungeon later on?")
        await r.advance(40, step=1)
        await r.human("how is the evening going over there", channel="whisper", to_guid=102)
        await r.advance(40, step=1)
        await r.human("gn all")
        await r.advance(40, step=1)
        said = [(round(t - t0, 3), s.guid, s.channel) for t, s in r.sent.timed_says() if t >= t0]
        assert said == TODAY
    run(main())


def test_asleep_member_whispered_answers_late_but_within_the_cap(store, clock, tmp_path, caplog):
    caplog.set_level(logging.INFO)

    async def main():
        r = make_rig(store, clock, tmp_path, script=numbered("sorry {n}, was out cold"),
                     availability=6)
        await r.session()
        await r.advance(NIGHT_S, step=60)
        lp = r.engine.life.presence(101, medium="game")
        assert lp is not None and lp.attention is Attention.NONE
        t0 = clock.now()
        await r.human("still awake over there?", channel="whisper", to_guid=101)
        await r.advance(game_cap_s(6) + 60, step=1)
        said = _whispers_from(r, 101, t0)
        assert len(said) == 1
        assert 30.0 <= said[0][0] - t0 <= game_cap_s(6) + 30.0
        assert any("reply_latency" in m and "guid=101" in m and "owed=1" in m and "att=NONE" in m
                   for m in caplog.messages)
    run(main())


def test_first_guild_answer_is_the_most_attentive(store, clock, tmp_path, caplog):
    """102 plays with him (in his group: full attention); the others are asleep. 102 answers
    first; anyone else only on a draw under NON_OWED_MAX_S (or as the owed answer)."""
    caplog.set_level(logging.INFO)

    async def main():
        r = make_rig(store, clock, tmp_path, script=numbered("hmm {n}, maybe the coast"),
                     availability=6, seed=3)
        await r.session()
        await r.advance(NIGHT_S, step=60)
        await r.event(Group(ts=0, leader_guid=HUMAN, raid=False, members=(HUMAN, 102)))
        assert r.engine.life.weight(102) == 1.0 and r.engine.life.weight(103) == 0.05
        t0 = clock.now()
        await r.human("where is the best place for cloth around here?")
        await r.advance(NON_OWED_MAX_S + 120, step=1)
        said = [(t - t0, s.guid) for t, s in r.sent.timed_says() if t >= t0]
        assert said and said[0][1] == 102 and said[0][0] < 30
        delays = {int(m.split("guid=")[1].split()[0]): float(m.split("delay_s=")[1].split()[0])
                  for m in caplog.messages if m.startswith("reply_latency")}
        for at, guid in said[1:]:
            assert delays[guid] <= NON_OWED_MAX_S
        skipped = [m for m in caplog.messages if m.startswith("reply_skipped")]
        assert skipped and all("reason=away" in m for m in skipped)  # asleep, drawn too late
    run(main())


def test_second_whisper_coalesces_and_urgent_hurries(store, clock, tmp_path, caplog):
    """His second whisper joins the answer still waiting; its urgent words (his second line
    only, so not his third: review-336 P3-I) send it at once, one answer for both."""
    caplog.set_level(logging.INFO)

    async def main():
        r = make_rig(store, clock, tmp_path, script=numbered("here, {n} sorry"),
                     availability=6)
        await r.session()
        await r.advance(NIGHT_S, step=60)
        t0 = clock.now()
        first = await r.human("did the new bags come through", channel="whisper", to_guid=101)
        await r.advance(20, step=1)
        assert _whispers_from(r, 101, t0) == []
        t1 = clock.now()
        second = await r.human("hey r u around?", channel="whisper", to_guid=101)  # urgent
        assert any(f"reply_coalesced: line={second.id} guid=101" in m for m in caplog.messages)
        planned = [m for m in caplog.messages if m.startswith("reply_planned")]
        assert len(planned) == 1 and f"line={first.id}" in planned[0]
        await r.advance(10, step=1)
        said = _whispers_from(r, 101, t0)
        assert len(said) == 1 and t1 <= said[0][0] <= t1 + 10
        await r.advance(game_cap_s(6), step=2)
        assert len(_whispers_from(r, 101, t0)) == 1  # one answer for both lines
    run(main())


def test_a_second_whisper_without_urgent_words_waits(store, clock, tmp_path, caplog):
    caplog.set_level(logging.INFO)

    async def main():
        r = make_rig(store, clock, tmp_path, script=numbered("here, {n} sorry"),
                     availability=6)
        await r.session()
        await r.advance(NIGHT_S, step=60)
        t0 = clock.now()
        await r.human("did the new bags come through", channel="whisper", to_guid=101)
        await r.advance(20, step=1)
        second = await r.human("the ones from the auction", channel="whisper", to_guid=101)
        await r.advance(30, step=1)
        assert _whispers_from(r, 101, t0) == []
        assert any(f"reply_coalesced: line={second.id} guid=101" in m for m in caplog.messages)
        await r.advance(game_cap_s(6), step=2)
        assert len(_whispers_from(r, 101, t0)) == 1
    run(main())


def test_a_first_urgent_whisper_is_answered_at_once(store, clock, tmp_path):
    """review-336 P3-F: urgent words wake her for his first whisper too."""
    async def main():
        r = make_rig(store, clock, tmp_path, script=numbered("here, {n} sorry"),
                     availability=6)
        await r.session()
        await r.advance(NIGHT_S, step=60)
        assert r.engine.life.presence(101, medium="game").attention is Attention.NONE
        t0 = clock.now()
        await r.human("hey r u around?", channel="whisper", to_guid=101)
        await r.advance(12, step=1)
        said = _whispers_from(r, 101, t0)
        assert len(said) == 1 and said[0][0] - t0 <= 12
    run(main())


def test_his_third_line_hurries_the_waiting_answer(store, clock, tmp_path):
    async def main():
        r = make_rig(store, clock, tmp_path, script=numbered("back now {n}"), availability=6)
        await r.session()
        await r.advance(NIGHT_S, step=60)
        t0 = clock.now()
        for n, text in enumerate(("got a sec later", "about the raid roster",
                                  "need a healer for sunday")):
            if n == 2:
                assert _whispers_from(r, 101, t0) == []  # two lines: still waiting
            await r.human(text, channel="whisper", to_guid=101)
            await r.advance(5, step=1)
        await r.advance(15, step=1)
        said = _whispers_from(r, 101, t0)
        assert len(said) == 1 and said[0][0] - t0 <= 30
    run(main())


def test_his_third_line_counts_only_the_last_ten_minutes(store, clock, tmp_path):
    """review-336 P3-G: two whispers of his from 20 minutes ago she never answered make no
    "third line" of today's second."""
    from leeroy_mind.protocol import Chat

    async def main():
        r = make_rig(store, clock, tmp_path, script=numbered("back now {n}"), availability=6)
        await r.session()
        await r.advance(NIGHT_S, step=60)
        for text, ago in (("left you a note in the bank", 1300), ("check the bank tab", 1250)):
            ev = Chat(ts=0, channel="whisper", from_guid=HUMAN, from_name="Testmage",
                      to_guid=101, text=text, map=0, zone=10)
            store.insert_chat_line(ev, session_id=None, origin="human", ts=clock.now() - ago)
        t0 = clock.now()
        await r.human("got a sec later", channel="whisper", to_guid=101)
        await r.advance(5, step=1)
        await r.human("about the raid roster", channel="whisper", to_guid=101)
        await r.advance(25, step=1)
        assert _whispers_from(r, 101, t0) == []
    run(main())


def test_goodbye_hurries_and_logout_carries_to_dm(store, clock, tmp_path, caplog):
    """Review Focus 1: his goodbye sends the waiting whisper answer at once; at his logout a
    whisper answer still waiting is never whispered to him offline: her Discord DM answers it,
    after what was left of its delay, and the owed watch keeps the stretch until it lands."""
    caplog.set_level(logging.INFO)

    async def main():
        r = make_rig(store, clock, tmp_path, script=numbered("night {n}, sleep well"),
                     availability=6)
        w = _watch(r)
        await r.session()
        await r.event(Login(ts=0, guid=HUMAN, human=True))
        await r.advance(NIGHT_S, step=60)
        t0 = clock.now()
        await r.human("you around tomorrow?", channel="whisper", to_guid=101)
        await r.advance(10, step=1)
        assert _whispers_from(r, 101, t0) == []
        t1 = clock.now()
        await r.human("gn all")
        await r.advance(12, step=1)
        hurried = _whispers_from(r, 101, t0)
        assert len(hurried) == 1 and hurried[0][0] - t1 <= 12

        whisper = await r.human("save me a seat at the raid", channel="whisper", to_guid=103)
        await r.advance(5, step=1)
        assert _whispers_from(r, 103, t0) == []
        t2 = clock.now()
        await r.event(Logout(ts=0, guid=HUMAN, human=True))
        await r.engine.end_session()  # the app's _end_session, after the logout's event
        assert any(f"life_carried: guid=103 line={whisper.id}" in m for m in caplog.messages)
        assert any(f"owed_carried: line={whisper.id} guid=103" in m for m in caplog.messages)
        dropped = r.store.conn.execute(
            "SELECT dropped FROM utterance WHERE guid = 103 AND channel = 'whisper'").fetchall()
        assert [d[0] for d in dropped] == ["carried"]
        await _tick(r, w, game_cap_s(6) + 60)
        assert [s for t, s in r.sent.timed_says() if t >= t2 and s.channel == "whisper"] == []
        rows = r.store.conn.execute(
            "SELECT author, origin, ts FROM discord_message WHERE channel = 'dm:103'").fetchall()
        assert [(a, o) for a, o, _ in rows] == [(103, "mind")]
        assert rows[0][2] - t2 <= game_cap_s(6) + 30
        assert ("whisper", 103) not in w._game  # the DM answer closed the stretch
        assert not any("owed_given_up" in m and "guid=103" in m for m in caplog.messages)
        assert not any(m.startswith("owed_rescue:") for m in caplog.messages)
        user = [c["messages"] for c in r.chat_calls()][-1][-1]["content"]
        assert CARRY_TEXT.format(He="He") in user and "save me a seat at the raid" in user
    run(main())


def test_goodbye_then_logout_carries_the_answer_being_typed(store, clock, tmp_path, caplog):
    """review-336 P2-1: his goodbye sends her waiting whisper answer on its way, and he logs off
    2 s later while it is still being typed: it is never whispered to him offline; her DM
    answers it at once instead."""
    caplog.set_level(logging.INFO)

    async def main():
        r = make_rig(store, clock, tmp_path, script=numbered("night {n}, sleep well"),
                     availability=6)
        w = _watch(r)
        await r.session()
        await r.event(Login(ts=0, guid=HUMAN, human=True))
        await r.advance(NIGHT_S, step=60)
        t0 = clock.now()
        whisper = await r.human("you around tomorrow?", channel="whisper", to_guid=101)
        await r.advance(10, step=1)
        await r.human("gn all")
        await r.advance(2, step=1)
        assert any(t.speaker == 101 and t.channel == "whisper" and phase in ("composing", "typing")
                   for t, phase, _ in r.engine.turn_phases())
        t1 = clock.now()
        await r.event(Logout(ts=0, guid=HUMAN, human=True))
        await r.engine.end_session()
        assert any(f"life_carried: guid=101 line={whisper.id}" in m for m in caplog.messages)
        assert any(f"owed_carried: line={whisper.id} guid=101" in m for m in caplog.messages)
        await _tick(r, w, 30)
        assert _whispers_from(r, 101, t0) == []
        rows = r.store.conn.execute(
            "SELECT author, ts FROM discord_message WHERE channel = 'dm:101'").fetchall()
        assert [a for a, _ in rows] == [101] and rows[0][1] - t1 <= 15
        dropped = r.store.conn.execute(
            "SELECT dropped FROM utterance WHERE guid = 101 AND channel = 'whisper'").fetchall()
        assert [d[0] for d in dropped] == ["carried"]
        assert ("whisper", 101) not in w._game
    run(main())


def test_a_relog_and_logout_keep_the_carried_answer(store, clock, tmp_path, caplog):
    """review-336 P3-B: he logs back in and out again before her DM answer lands: it still
    lands."""
    caplog.set_level(logging.INFO)

    async def main():
        r = make_rig(store, clock, tmp_path, script=numbered("ok {n}"), availability=6)
        w = _watch(r)
        await r.session()
        await r.event(Login(ts=0, guid=HUMAN, human=True))
        await r.advance(NIGHT_S, step=60)
        r.engine.life.answer_delay = lambda guid, *, rng, owed: 200.0
        await r.human("can you craft me a cloak", channel="whisper", to_guid=104)
        await r.advance(5, step=1)
        await r.event(Logout(ts=0, guid=HUMAN, human=True))
        await r.engine.end_session()
        await _tick(r, w, 20)
        await r.session()
        await r.event(Login(ts=0, guid=HUMAN, human=True))
        await _tick(r, w, 20)
        await r.event(Logout(ts=0, guid=HUMAN, human=True))
        await r.engine.end_session()
        await _tick(r, w, 200)
        rows = r.store.conn.execute(
            "SELECT author FROM discord_message WHERE channel = 'dm:104'").fetchall()
        assert [a[0] for a in rows] == [104]
        cancelled = r.store.conn.execute(
            "SELECT count(*) FROM utterance WHERE channel = 'dm:104' AND dropped IS NOT NULL"
            ).fetchone()[0]
        assert cancelled == 0
    run(main())


# -- a mind stop: the answers still owed are kept for the next start ---------------------------

def _record(store):
    import json
    from leeroy_mind.talk.owed import PERSIST_KEY
    raw = store.get_meta(PERSIST_KEY)
    return None if raw is None else json.loads(raw)


def test_a_clean_stop_keeps_a_waiting_whisper_answer_and_the_next_start_whispers_it(
        store, clock, tmp_path, caplog):
    caplog.set_level(logging.INFO)

    async def main():
        r = make_rig(store, clock, tmp_path, script=numbered("sorry {n}, was out cold"),
                     availability=6)
        _watch(r)
        await r.session()
        await r.event(Login(ts=0, guid=HUMAN, human=True))
        await r.advance(NIGHT_S, step=60)
        whisper = await r.human("still awake over there?", channel="whisper", to_guid=101)
        await r.advance(10, step=1)
        r.engine.stopping = True  # MindApp._shutdown, him online
        await r.engine.end_session()
        assert not any("life_carried" in m for m in caplog.messages)
        assert _record(store) == [{"line": whisper.id, "guid": 101, "dm": 0}]
        assert "still awake" not in store.get_meta("life:carried")

        r2 = make_rig(store, clock, tmp_path, script=numbered("sorry {n}, was out cold"),
                      availability=6)
        w2 = _watch(r2)
        assert _record(store) is None  # cleared once read
        assert any(f"owed_restored: line={whisper.id} guid=101 dm=0" in m
                   for m in caplog.messages)
        t1 = clock.now()
        await _tick(r2, w2, 80)
        assert len(_whispers_from(r2, 101, t1)) == 1
        assert ("whisper", 101) not in w2._game
    run(main())


def test_a_stop_after_the_bridge_dropped_carries_nothing_live_and_the_next_start_dms_it(
        store, clock, tmp_path, caplog):
    """review-336 P3-C: with the bridge gone (him unknown, read as offline) a stop carries no
    turn into the shutdown; the next start, with him offline, answers in her DM."""
    caplog.set_level(logging.INFO)

    async def main():
        r = make_rig(store, clock, tmp_path, script=numbered("ok {n}"), availability=6)
        _watch(r)
        await r.session()
        await r.event(Login(ts=0, guid=HUMAN, human=True))
        await r.advance(NIGHT_S, step=60)
        whisper = await r.human("save me a seat at the raid", channel="whisper", to_guid=103)
        await r.advance(5, step=1)
        r.world.human_online = False  # MindApp._on_connection(False)
        r.engine.stopping = True
        await r.engine.end_session()
        assert not any("life_carried" in m for m in caplog.messages)
        assert not [t for t in r.engine._tasks if not t.done()]
        assert _record(store) == [{"line": whisper.id, "guid": 103, "dm": 0}]

        r2 = make_rig(store, clock, tmp_path, script=numbered("ok {n}"), availability=6)
        r2.world.human_online = False
        w2 = _watch(r2)
        t1 = clock.now()
        await _tick(r2, w2, 80)
        rows = store.conn.execute(
            "SELECT author FROM discord_message WHERE channel = 'dm:103'").fetchall()
        assert [a[0] for a in rows] == [103]
        assert any("owed_rescue: medium=whisper channel=dm:103" in m and "reason=restored" in m
                   for m in caplog.messages)
        assert _whispers_from(r2, 103, t1) == []
        assert ("whisper", 103) not in w2._game
    run(main())


def test_a_carried_answer_pending_at_a_stop_is_answered_after_the_start(store, clock,
                                                                          tmp_path, caplog):
    caplog.set_level(logging.INFO)

    async def main():
        r = make_rig(store, clock, tmp_path, script=numbered("ok {n}"), availability=6)
        _watch(r)
        await r.session()
        await r.event(Login(ts=0, guid=HUMAN, human=True))
        await r.advance(NIGHT_S, step=60)
        r.engine.life.answer_delay = lambda guid, *, rng, owed: 300.0
        whisper = await r.human("can you craft me a cloak", channel="whisper", to_guid=104)
        await r.advance(5, step=1)
        await r.event(Logout(ts=0, guid=HUMAN, human=True))
        await r.engine.end_session()
        assert any("life_carried: guid=104" in m for m in caplog.messages)
        await r.advance(30, step=1)
        r.engine.stopping = True
        await r.engine.end_session()
        assert _record(store) == [{"line": whisper.id, "guid": 104, "dm": 1}]
        assert store.conn.execute(
            "SELECT count(*) FROM discord_message WHERE channel = 'dm:104'").fetchone()[0] == 0

        r2 = make_rig(store, clock, tmp_path, script=numbered("ok {n}"), availability=6)
        r2.world.human_online = False
        w2 = _watch(r2)
        await _tick(r2, w2, 80)
        rows = store.conn.execute(
            "SELECT author FROM discord_message WHERE channel = 'dm:104'").fetchall()
        assert [a[0] for a in rows] == [104]
        assert ("whisper", 104) not in w2._game
    run(main())


def test_availability_zero_keeps_nothing_at_a_stop(store, clock, tmp_path):
    async def main():
        r = make_rig(store, clock, tmp_path, script=numbered("ok {n}"))
        _watch(r)
        await r.session()
        await r.human("one sec", channel="whisper", to_guid=102)
        r.engine.stopping = True
        await r.engine.end_session()
        assert _record(store) is None
    run(main())


def test_a_carried_answer_that_fails_is_answered_at_his_next_login(store, clock, tmp_path,
                                                                   caplog):
    """The carried DM answer dropped (the model never answers): the stretch stays owed and is
    held for his next login, never given up (D8's whisper rule)."""
    caplog.set_level(logging.INFO)

    async def main():
        r = make_rig(store, clock, tmp_path, script=numbered("ok {n}"), availability=6)
        w = _watch(r)
        await r.session()
        await r.event(Login(ts=0, guid=HUMAN, human=True))
        await r.advance(NIGHT_S, step=60)
        await r.human("can you craft me a cloak", channel="whisper", to_guid=104)
        await r.advance(3, step=1)
        r.llm.set_script(lambda model, messages: "(nothing)")
        await r.event(Logout(ts=0, guid=HUMAN, human=True))
        await r.engine.end_session()
        await _tick(r, w, game_cap_s(6) + 120)
        assert ("whisper", 104) in w._game
        assert any("owed_held" in m and "reason=human_offline" in m for m in caplog.messages)
        assert not any("owed_given_up" in m for m in caplog.messages)
    run(main())


def test_owed_watchdog_does_not_rescue_a_waiting_answer(store, clock, tmp_path, caplog):
    caplog.set_level(logging.INFO)

    async def main():
        r = make_rig(store, clock, tmp_path, script=numbered("yep {n}"), availability=6)
        w = _watch(r)
        await r.session()
        await r.advance(NIGHT_S, step=60)
        r.engine.life.answer_delay = lambda guid, *, rng, owed: 300.0
        t0 = clock.now()
        await r.human("are the bags still on sale", channel="whisper", to_guid=105)
        await _tick(r, w, 290)
        assert not any(m.startswith("owed_rescue:") for m in caplog.messages)
        assert _whispers_from(r, 105, t0) == []
        await _tick(r, w, 40)
        said = _whispers_from(r, 105, t0)
        assert len(said) == 1 and said[0][0] - t0 >= 300
        assert not any(m.startswith("owed_rescue:") for m in caplog.messages)
    run(main())


def test_a_whisper_answer_never_goes_to_him_offline(store, clock, tmp_path):
    """With availability above 0, `_blocked` refuses a whisper to him while he is offline for
    every turn, his whisper's own answer included (at 0 it is today's rule)."""
    async def main():
        r = make_rig(store, clock, tmp_path, script=numbered("ok {n}"), availability=6)
        await r.session()
        await r.event(Login(ts=0, guid=HUMAN, human=True))
        await r.human("one sec", channel="whisper", to_guid=102)
        r.world.human_online = False
        await r.advance(game_cap_s(6) + 30, step=2)
        assert [s for s in r.sent.says() if s.channel == "whisper"] == []
        rows = r.store.conn.execute(
            "SELECT dropped FROM utterance WHERE channel = 'whisper'").fetchall()
        assert [d[0] for d in rows] == ["human_offline"]
    run(main())


def test_a_line_he_spoke_is_answered_on_todays_timing(store, clock, tmp_path, caplog):
    """A line he spoke in a voice room is answered by the members in the room with him: her
    day's latency is not drawn for it (at 22:00 the cast is asleep by schedule)."""
    caplog.set_level(logging.INFO)

    async def main():
        r = make_rig(store, clock, tmp_path, script=numbered("yeah {n}"), availability=6)
        r.world.room_audience = lambda channel: frozenset({101})
        queued = []
        r.engine._queue = lambda items: queued.extend(items)
        await r.advance(NIGHT_S, step=60)
        await r.human("arkon you still with us", channel="voice:guild_hall")
        assert [t.speaker for t, _ in queued] == [101]
        assert 1.0 <= queued[0][0].delay <= 4.0 and queued[0][1].wake is None
        assert not any(m.startswith("reply_latency") for m in caplog.messages)
    run(main())


def test_the_answer_he_is_owed_is_the_most_attentive_members(store, clock, tmp_path, caplog):
    """review-336 P3-A: an asleep member planned first is not the one owed while a member in his
    group is there: the owed answer is 102's, and 103's is skipped past NON_OWED_MAX_S or said
    sooner as any other."""
    from leeroy_mind.talk.select import ReplyTurn
    from leeroy_mind.talk.turns import TurnKind
    caplog.set_level(logging.INFO)

    async def main():
        r = make_rig(store, clock, tmp_path, availability=6)
        await r.advance(NIGHT_S, step=60)
        await r.event(Group(ts=0, leader_guid=HUMAN, raid=False, members=(HUMAN, 102)))
        row = await r.human("anyone know a good farming spot")
        for n in range(30):
            planned = [ReplyTurn(g, f"x{n}-{g}", TurnKind.REPLY_HUMAN, "guild", "guild")
                       for g in (103, 102)]
            caplog.clear()
            timed = r.engine._life_delays(replace(row, id=row.id + 1000 + n), planned,
                                          whisper=False, called=[], push=None)
            assert timed[0][0].speaker == 102
            assert all(d <= NON_OWED_MAX_S for rt, d, _ in timed if rt.speaker == 103)
            assert any("guid=102" in m and "owed=1" in m for m in caplog.messages)
            assert not any("guid=103" in m and "owed=1" in m for m in caplog.messages)
    run(main())


def test_the_voice_room_he_speaks_in_gives_him_full_attention(store, clock, tmp_path):
    """review-336 P3-E: an asleep member in the voice room with him weighs as one with him."""
    from leeroy_mind.talk.select import SpeakerContext
    from leeroy_mind.talk.budget import DayPhase
    r = make_rig(store, clock, tmp_path, availability=6)
    r.sync(r.advance(NIGHT_S, step=60))
    life = r.engine.life
    assert life.weight(101) == 0.05 and life.weight(101, with_him=True) == 1.0
    ctx = SpeakerContext(now=clock.now(), topic=set(), opener=False, phase=DayPhase.LATE)
    away, _ = r.engine._weigh([101], last=HUMAN, ctx=ctx, reply=True)
    room, _ = r.engine._weigh([101], last=HUMAN, ctx=ctx, reply=True, with_him=frozenset({101}))
    assert room[101] == pytest.approx(away[101] / 0.05)
    r.close()


# -- owner-replies health, with her day (review-336 P2-2) ------------------------------------

def _his(store, text, ts, channel="whisper", to_guid=101):
    from leeroy_mind.protocol import Chat
    ev = Chat(ts=0, channel=channel, from_guid=HUMAN, from_name="Testmage", to_guid=to_guid,
              text=text, map=0, zone=10)
    return store.insert_chat_line(ev, session_id=None, origin="human", ts=ts)


def _hers(store, guid, text, ts, channel="whisper"):
    from leeroy_mind.protocol import Chat
    ev = Chat(ts=0, channel=channel, from_guid=guid, from_name="M", to_guid=HUMAN, text=text,
              map=0, zone=10)
    store.insert_chat_line(ev, session_id=None, origin="mind", ts=ts)


def test_health_counts_her_day_late_answers_and_her_dm_answer(store):
    import time
    from conftest import seed_test_cast
    from leeroy_mind import health
    seed_test_cast(store)
    now = time.time()
    store.set_meta("human_guid", str(HUMAN))
    _his(store, "did the new bags come through", now - 7200)
    _hers(store, 101, "yes they did", now - 7200 + 360)  # 6 minutes: her day's bound
    _his(store, "save me a seat at the raid", now - 5400, to_guid=103)
    _hers(store, 103, "seat's yours, held it", now - 5400 + 90, channel="dm:103")  # carried to her DM
    at_zero = health._owner_replies(store.conn, now)
    assert at_zero.status == health.NOTE and "2 of 2 unanswered within 5 min" in at_zero.text
    store.set_meta(life_config.META_KEY, "6")
    check = health._owner_replies(store.conn, now)
    assert check.status == health.OK and "all 2 answered" in check.text
    _his(store, "can you craft me a cloak", now - 3600, to_guid=104)
    _hers(store, 104, "can do, give me an hour", now - 3600 + game_cap_s(6) + 200)  # past the bound and margin
    late = health._owner_replies(store.conn, now)
    assert late.status == health.NOTE and "1 of 3 unanswered in time" in late.text
    assert "craft me a cloak" not in late.text


def test_health_waits_for_her_own_time_on_a_dm(store):
    """A DM to a member asleep by her day is answered in time when she answers it after she
    wakes (discord.owntime.due_at), not within 5 minutes; one never answered past that is
    counted."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from conftest import seed_test_cast
    from leeroy_mind import health
    from leeroy_mind.discord import store as dstore
    seed_test_cast(store)
    store.set_meta("human_guid", str(HUMAN))
    store.set_meta(life_config.META_KEY, "6")
    ny = ZoneInfo("America/New_York")
    posted = datetime(2026, 11, 18, 18, 0, tzinfo=ny).timestamp()  # Wed 23:00 in the UK
    now = posted + 20 * 3600
    dm = dstore.post(store, channel="dm:105", author=HUMAN, origin="human",
                     text="are the bags still on sale", now=posted)
    due = health._own_time_due(store.conn, HUMAN, now, 6)("dm:105", dm, posted)
    assert due - posted > 3 * 3600  # asleep: she answers after she wakes
    dstore.post(store, channel="dm:105", author=105, origin="mind", text="still on, half price",
                now=due - 300, reply_to=dm)
    assert health._owner_replies(store.conn, now).status == health.OK
    other = dstore.post(store, channel="dm:102", author=HUMAN, origin="human",
                        text="got a sec later", now=posted)
    assert other
    check = health._owner_replies(store.conn, now)
    assert check.status == health.NOTE and "discord_dm 1" in check.text
    assert life_config.availability() == 0  # the health read leaves the process pin alone


# -- LifeService --------------------------------------------------------------------------------

def test_weight_and_answer_delay(store, clock, tmp_path):
    r = make_rig(store, clock, tmp_path, availability=0)
    life = r.engine.life
    assert life.weight(101) == 1.0
    with pytest.raises(ValueError):
        life.answer_delay(101, rng=random.Random(1), owed=True)
    life.configure(replace(r.cfg, life=replace(r.cfg.life, availability=6)))
    # 20:00 on a Thursday: a free or gaming evening, in game full or half
    assert life.weight(101) in (1.0, 0.6)
    late = replace(r.cfg, life=replace(r.cfg.life, availability=6))
    life.configure(late)
    rng = random.Random(4)
    owed = [life.answer_delay(101, rng=rng, owed=True) for _ in range(200)]
    assert all(0 < d <= game_cap_s(6) for d in owed)


def test_asleep_delays_are_capped_when_owed_and_missed_when_not(store, tmp_path):
    from leeroy_mind.testing.fake_clock import FakeClock
    from datetime import datetime, timezone
    clock = FakeClock(datetime(2026, 10, 29, 23, 0, tzinfo=timezone.utc), yields=64)
    r = make_rig(store, clock, tmp_path, availability=6)
    life = r.engine.life
    assert life.presence(101, medium="game").attention is Attention.NONE
    rng = random.Random(9)
    owed = [life.answer_delay(101, rng=rng, owed=True) for _ in range(300)]
    other = [life.answer_delay(101, rng=rng, owed=False) for _ in range(300)]
    assert all(d is not None and d <= game_cap_s(6) for d in owed)
    assert max(owed) == game_cap_s(6)
    assert None in other and all(d is None or d <= NON_OWED_MAX_S for d in other)
    r.world.apply(Group(ts=0, leader_guid=HUMAN, raid=False, members=(HUMAN, 101)), clock.now())
    assert life.presence(101, medium="game", in_his_group=True).attention is Attention.FULL
    assert life.weight(101) == 1.0
    assert all(life.answer_delay(101, rng=rng, owed=False) is not None for _ in range(50))


def test_state_scales_weight_and_is_read_once_per_snapshot(store, clock, tmp_path):
    r = make_rig(store, clock, tmp_path, availability=6, life_state=True)
    life = r.engine.life
    assert life.state is not None
    reads = []
    life.state.get = lambda g: (reads.append(g), State(0.0, 0.0, 0.0, 0.8, 0.0))[1]
    base = life.weight(101)
    life._states.clear()
    life.state.get = lambda g: (reads.append(g), State(0.0, 0.0, 0.0, -0.8, 0.0))[1]
    low = life.weight(101)
    assert base == pytest.approx(low * 1.4 / 0.6)
    for _ in range(20):
        life.weight(101)
        life.answer_delay(101, rng=random.Random(1), owed=True)
    assert reads == [101, 101]  # one read per member per snapshot (review-334 note 4)
    r.sync(r.advance(31))
    life.weight(101)
    assert reads == [101, 101, 101]
    r.close()


# -- Discord: a group DM goes to someone online first (review-335 P3-E) ----------------------

def test_group_dm_prefers_a_member_online_over_one_on_the_way_home(store, tmp_path):
    """Thursday 17:30 in the UK: 101 (weekday hours) is on the way home (idle), 102 (retired)
    is free (online). With availability above 0 his group DM is drawn to 102; at 0 the old
    schedule has 101 idle and 102 online, and both may be drawn, as today."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from conftest import seed_test_cast
    from leeroy_mind.discord import layout
    from leeroy_mind.discord.talk import DiscordTalk
    from leeroy_mind.testing.fake_clock import FakeClock
    clock = FakeClock(datetime(2026, 11, 19, 12, 30, tzinfo=ZoneInfo("America/New_York")),
                      yields=64)
    seed_test_cast(store)
    save_member(store, 101, job="parcel counter clerk", rhythm="regular weekday hours",
                night_owl=True)
    save_member(store, 102, job="", rhythm="retired, potters about the allotment")
    r = make_rig(store, clock, tmp_path, availability=6)
    app = SimpleNamespace(store=store, clock=clock, engine=r.engine, world=r.world,
                          personas=r.engine.personas, human_guid=HUMAN, arbiter=None,
                          rng=random.Random(2), realm=None)
    talk = DiscordTalk(app, rng=random.Random(2))

    def states():
        return {g: talk._presence(g, r.engine.personas.get(g), clock.local()).state
                for g in (101, 102)}

    def picks(ch, n=80):
        return {g for _ in range(n) for g in talk._draw(ch, frozenset({HUMAN, 101, 102}))}

    group = layout.Channel("gdm:1", "Group chat", "groups", "")
    assert states() == {101: "idle", 102: "online"}
    assert picks(group) == {102}
    general = layout.channel("general")
    assert general is not None and picks(general) == {101, 102}  # a channel: as today
    life_config.pin(0, False)
    assert states() == {101: "idle", 102: "online"}
    assert picks(group) == {101, 102}
