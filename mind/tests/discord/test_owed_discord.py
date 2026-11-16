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


def test_a_silent_model_still_answers_his_dm(store, clock, tmp_path):
    """Her DM answer was asked again after a first "(nothing)" and a second one stood; now the
    owed answer gets one plainer ask, then the fallback line."""
    rig = rig_for(store, clock, tmp_path, script=lambda m, msgs: "(nothing)")
    talk = talk_for(rig)
    mid = post(rig, "dm:105", "how was work today?")
    answer(rig, talk, mid)
    said = answers(rig, "dm:105", mid)
    assert len(said) == 1 and said[0]["text"].strip() and "?" in said[0]["text"]
    rig.close()
