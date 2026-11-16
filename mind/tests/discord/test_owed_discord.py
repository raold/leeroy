"""Every post of his on the guild's Discord gets a member's answer (talk/owed.py): a channel post
with nobody online or idle is still answered, a DM whose answer was lost is answered by the
watchdog after its grace (once), a DM she answers on her own time is held, and an owed DM answer
the model keeps silent on is the fallback line. FakeLLM, FakeClock, a scratch mind.db; invented
text."""
import logging
from types import SimpleNamespace

from discord.test_discord_talk import answer, answers, clock, post, rig_for, talk_for  # noqa: F401
from engine_rig import numbered
from leeroy_mind.talk import owed
from leeroy_mind.talk.owed import OwedWatch


def _watch(rig, talk):
    app = SimpleNamespace(store=rig.store, clock=rig.clock, engine=rig.engine, world=rig.world,
                          scheduler=rig.engine.scheduler, discord_talk=talk)
    return OwedWatch(app)


def _tick(rig, w, seconds):
    async def go():
        for _ in range(int(seconds)):
            await rig.advance(1)
            w.tick(rig.clock.now())
    rig.sync(go())


def test_a_channel_post_with_nobody_online_or_idle_is_still_answered(store, clock, tmp_path,
                                                                     caplog):
    """Everyone lives in the UK, where it is 01:00: before, nobody was online or idle and his
    post went unanswered."""
    caplog.set_level(logging.INFO)
    rig = rig_for(store, clock, tmp_path, us=(), script=numbered("still up, {n}"))
    talk = talk_for(rig)
    mid = post(rig, "general", "anyone awake?")
    answer(rig, talk, mid)
    said = answers(rig, "general", mid)
    assert len(said) == 1
    assert any("discord_draw_widened" in m for m in caplog.messages)
    rig.close()


def test_a_dm_whose_answer_was_lost_is_answered_by_the_watchdog(store, clock, tmp_path, caplog):
    caplog.set_level(logging.INFO)
    rig = rig_for(store, clock, tmp_path, script=numbered("sorry, just saw this {n}"))
    talk = talk_for(rig)
    w = _watch(rig, talk)
    _tick(rig, w, owed.START_GRACE_S)
    mid = post(rig, "dm:102", "you there?")  # handed to nobody: its answer is lost
    _tick(rig, w, owed.GRACE_S["discord"] - 20)
    assert answers(rig, "dm:102", mid) == []
    _tick(rig, w, 120)
    said = answers(rig, "dm:102", mid)
    assert [m["author"] for m in said] == [102]
    assert any("owed_rescue:" in m and "medium=discord" in m for m in caplog.messages)
    _tick(rig, w, 400)
    assert len(answers(rig, "dm:102", mid)) == 1 and not w._discord
    rig.close()


def test_an_answered_dm_and_a_channel_answer_are_left_alone(store, clock, tmp_path):
    rig = rig_for(store, clock, tmp_path, script=numbered("hey {n}"))
    talk = talk_for(rig)
    w = _watch(rig, talk)
    _tick(rig, w, owed.START_GRACE_S)
    dm = post(rig, "dm:103", "hey you")
    ch = post(rig, "general", "morning all")

    async def hand_over():
        talk.on_his_post(dm)
        talk.on_his_post(ch)
    rig.sync(hand_over())
    _tick(rig, w, 400)
    assert len(answers(rig, "dm:103", dm)) == 1
    assert 1 <= len(answers(rig, "general", ch)) <= 2
    assert not w._discord
    rig.close()


def test_a_dm_she_answers_on_her_own_time_is_held(store, clock, tmp_path, caplog):
    caplog.set_level(logging.INFO)
    rig = rig_for(store, clock, tmp_path, script=numbered("hey {n}"))
    talk = talk_for(rig)
    w = _watch(rig, talk)
    _tick(rig, w, owed.START_GRACE_S)
    mid = post(rig, "dm:104", "free tonight?")
    rig.store.set_meta("discord:dm:deferred:104", str(mid))
    _tick(rig, w, 300)
    assert answers(rig, "dm:104", mid) == []
    assert any("owed_held" in m and "reason=own_time" in m for m in caplog.messages)
    rig.close()


def test_a_silent_model_holds_his_dm_never_a_template(store, clock, tmp_path, caplog):
    """Her DM answer stays (nothing) through its retry and both fallback asks: the turn drops;
    the watchdog's rescue finds nothing either and holds the message (review-327 P2-1)."""
    caplog.set_level(logging.INFO)
    rig = rig_for(store, clock, tmp_path, script=lambda m, msgs: "(nothing)")
    talk = talk_for(rig)
    w = _watch(rig, talk)
    _tick(rig, w, owed.START_GRACE_S)
    mid = post(rig, "dm:105", "how was work today?")

    async def hand_over():
        talk.on_his_post(mid)
    rig.sync(hand_over())
    _tick(rig, w, 400)
    assert answers(rig, "dm:105", mid) == []
    assert any("owed_held" in m and "reason=no_clean_draft" in m for m in caplog.messages)
    assert not any("owed_given_up" in m for m in caplog.messages)
    rig.close()


def test_the_discord_lease_is_kept_while_answers_to_him_are_in_flight(store, clock, tmp_path,
                                                                     caplog):
    """Owner, 2026-11-16: the live model stays pinned while several members answer him."""
    from discord.test_discord_talk import FakeArbiter
    from leeroy_mind.discord.talk import LEASE, LEASE_IDLE_S
    from leeroy_mind.talk.turns import Turn, TurnKind
    caplog.set_level(logging.INFO)
    rig = rig_for(store, clock, tmp_path, script=numbered("hey {n}"))
    arbiter = FakeArbiter()
    talk = talk_for(rig, arbiter=arbiter)
    in_flight = [Turn(kind=TurnKind.REPLY_HUMAN, speaker=102, channel="dm:102", to_guid=1,
                      addressee=1)]
    rig.engine.planned_turns = lambda: list(in_flight)

    async def go():
        talk._take_lease()
        await rig.advance(LEASE_IDLE_S + 60, 10)
        assert LEASE in arbiter.holders
        assert any("discord_lease_kept" in m for m in caplog.messages)
        in_flight.clear()
        await rig.advance(120, 10)
        assert LEASE not in arbiter.holders
    rig.sync(go())
    rig.close()


def test_a_dm_to_a_member_her_arc_has_away_holds_as_away(store, clock, tmp_path, caplog,
                                                        monkeypatch):
    """review-327-r2 P3-N4."""
    import leeroy_mind.talk.owed as owed_mod
    caplog.set_level(logging.INFO)
    rig = rig_for(store, clock, tmp_path, script=numbered("hey {n}"))
    talk = talk_for(rig)
    w = _watch(rig, talk)
    _tick(rig, w, owed.START_GRACE_S)
    mid = post(rig, "dm:104", "free tonight?")
    import leeroy_mind.arc.live as live
    monkeypatch.setattr(live, "away_now", lambda store, now: frozenset({104}))
    _tick(rig, w, 300)
    assert answers(rig, "dm:104", mid) == []
    assert any("owed_held" in m and "reason=away" in m for m in caplog.messages)
    assert not any("reason=no_clean_draft" in m for m in caplog.messages)
    rig.close()


def test_an_old_channel_post_is_dropped_with_a_log_and_an_old_dm_is_kept(store, clock, tmp_path,
                                                                        caplog):
    """review-327-r2 P3-N1: a Discord channel post past 6 h is let go (`too_old`, logged); a
    DM is kept whatever its age and answered."""
    caplog.set_level(logging.INFO)
    rig = rig_for(store, clock, tmp_path, script=numbered("sorry, just saw this {n}"))
    talk = talk_for(rig)
    w = _watch(rig, talk)
    old = rig.clock.now() - 20 * 3600
    from leeroy_mind.discord import store as dstore
    ch = dstore.post(store, channel="general", author=1, origin="human", text="morning", now=old)
    dm = dstore.post(store, channel="dm:102", author=1, origin="human", text="you up?", now=old)
    _tick(rig, w, owed.START_GRACE_S + 200)
    assert any("owed_given_up" in m and "reason=too_old" in m for m in caplog.messages)
    assert answers(rig, "general", ch) == []
    assert [m["author"] for m in answers(rig, "dm:102", dm)] == [102]
    rig.close()
