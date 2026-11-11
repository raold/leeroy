"""Review regressions: fake time, fake transport, invented members only."""
import asyncio
from dataclasses import replace
import random

import pytest

from test_timing_realism import rig,clock,Always,turn,LONG,plan
from leeroy_mind.config import load_config
from leeroy_mind.protocol import Boss
from leeroy_mind.talk.pipeline import TurnMeta
from leeroy_mind.talk.realism import Plan
from leeroy_mind.talk.raid import RaidPhase
from leeroy_mind.talk.states import Altered, effects_for
from leeroy_mind.talk.style import Styled
from leeroy_mind.talk.turns import TurnKind


@pytest.mark.parametrize("tail",["correction","burst"])
@pytest.mark.parametrize("new_medium",["game","discord"])
def test_newer_same_member_line_prevents_stale_tail(rig,monkeypatch,tail,new_medium):
    async def exercise():
        e = rig.engine
        e.rng = Always()
        e.realism.member(101).credit = 1
        monkeypatch.setattr(e,"_style",lambda *a,**kw:[Styled("ready with a flask","ready with a flask")])
        if tail == "burst":
            monkeypatch.setattr(e.realism,"plan",lambda *a:Plan(
                (Styled("first point","first point"),Styled("old second point","old second point")),
                (0,8),2,6,burst=True))
        job = asyncio.create_task(e.speak(turn("whisper")))
        for _ in range(80):
            await rig.advance(.5,.5)
            if rig.sent.says():
                break
        assert len(rig.sent.says()) == 1
        assert ("ready with a flask" if tail=="correction" else "first point") in " ".join(e._own_lines(turn("whisper")))
        newer = TurnMeta(e._new_id(),whisper_reply=True,answers_human=True)
        assert await e._send_line(turn("whisper" if new_medium=="game" else "dm:101"),newer,e.personas.get(101),"omw now",[],first=True) is None
        await rig.advance(20)
        await job
        expected = ["reqdy with a flask" if tail=="correction" else "first point"]
        assert [s.text for s in rig.sent.says()] == expected + (["omw now"] if new_medium=="game" else [])
        assert "realism_stale" in [r[0] for r in rig.store.conn.execute("SELECT dropped FROM utterance")]
    rig.sync(exercise())


def test_complete_natural_burst_keeps_order_and_visible_history(rig,monkeypatch):
    e = rig.engine
    e.rng = Always()
    monkeypatch.setattr(e,"_style",lambda *a,**kw:[Styled(LONG,LONG)])
    said = rig.speak_turn(turn(),seconds=60)
    assert len(said) == 3 and " ".join(said) == LONG
    assert " ".join(e._own_lines(turn())) == LONG
    assert len(rig.sent.says()) == 3 and not e.typing_now()


def test_correction_is_absent_from_own_history_and_next_prompt(rig,monkeypatch):
    e = rig.engine
    e.rng = Always()
    e.realism.member(101).credit = 1
    monkeypatch.setattr(e,"_style",lambda *a,**kw:[Styled("ready with a flask","ready with a flask")])
    assert rig.speak_turn(turn(),seconds=50)[-1] == "*ready"
    assert "*ready" not in " ".join(e._own_lines(turn()))
    composed = rig.sync(e._compose(turn(),TurnMeta(e._new_id()),live=True))
    assert "*ready" not in str(composed)
    e._own.clear()
    assert "*ready" not in " ".join(e._own_lines(turn()))


@pytest.mark.parametrize("state",[Altered.SOBER,Altered.TIPSY,Altered.DRUNK])
def test_realism_preserves_authored_and_altered_typos(rig,state):
    e = rig.engine
    e._altered[101] = state
    p = e.personas.get(101)
    p = replace(p,typing=replace(p.typing,typo_rate=.15))
    text = "ready with a flask tonight and bringing water for the whole group"
    def styled(enabled):
        e.rng = random.Random(12)
        return [e._style(turn(),p,text,close=False,realism=enabled) for _ in range(30)]
    original = styled(False)
    assert any(s.line != s.plain for row in original for s in row)
    assert styled(True) == original


@pytest.mark.parametrize("state",[Altered.DRUNK,Altered.HIGH,Altered.WIRED,Altered.HUNGOVER])
def test_realism_preserves_altered_typing_speed(rig,state):
    e = rig.engine
    e.rng = Always()
    e._altered[101] = Altered.SOBER
    sober = plan(rig,baseline=(1000,)).delays[0]
    e._altered[101] = state
    changed = plan(rig,baseline=(1000,)).delays[0]
    assert changed == pytest.approx(sober*effects_for(state,turn().kind).delay_mult)


def test_correction_does_not_add_another_typo_to_already_styled_line(rig):
    e = rig.engine
    e.rng = Always()
    e.realism.member(101).credit = 1
    p = e.realism.plan(e,turn(),TurnMeta("t1"),e.personas.get(101),
        [Styled("ready wtih a flask","ready with a flask")],[2])
    assert not p.correction


def test_relogin_clears_boss_fight_missing_its_offline_end_event(rig):
    e = rig.engine
    e.realism.observe(Boss(ts=0,map=0,instance_id=1,boss_id=2,boss_name="Invented",state="engaged",members=(101,)),rig.clock.now())
    assert e.realism.combat(e,turn())
    rig.sync(e.end_session())
    rig.sync(e.start_session(999))
    assert not e.realism.combat(e,turn())


def test_other_talk_keys_do_not_disable_realism(tmp_path):
    path = tmp_path / "mind.toml"
    path.write_text("[talk]\nrealism=true\nselfie_feature=true\n")
    assert load_config(path).talk.realism


def test_wall_clock_step_does_not_extend_typing_wait(rig,monkeypatch):
    waits = []
    async def sleep(seconds):
        waits.append(seconds)
        if len(waits)>3:
            pytest.fail("wall time extended the bounded wait")
        rig.clock._t += seconds - (100 if len(waits)==1 else 0)
    monkeypatch.setattr(rig.clock,"sleep",sleep)
    rig.sync(rig.engine._realistic_sleep(turn(),21))
    assert waits == [10,10,1]


@pytest.mark.parametrize("kind",[TurnKind.KILL,TurnKind.WIPE_REACT])
def test_raid_event_turn_keeps_all_debrief_lines_and_original_timing(rig,monkeypatch,kind):
    e = rig.engine
    monkeypatch.setattr(e.raid,"phase",lambda _:RaidPhase.WIPE_DEBRIEF)
    assert not e.realism.eligible(e,turn("raid",kind),TurnMeta("t1"))
    lines = [Styled("first point","first point"),Styled("second point","second point")]
    assert e.realism.plan(e,turn("raid",kind),TurnMeta("t1"),e.personas.get(101),lines,[2,2]) is None


def test_mid_burst_combat_leaves_drop_receipt(rig,monkeypatch):
    async def exercise():
        e = rig.engine
        e.rng = Always()
        monkeypatch.setattr(e,"_style",lambda *a,**kw:[Styled(LONG,LONG)])
        job = asyncio.create_task(e.speak(turn()))
        for _ in range(80):
            await rig.advance(.5,.5)
            if rig.sent.says():
                break
        monkeypatch.setattr(e.raid,"phase",lambda _:RaidPhase.COMBAT)
        await rig.advance(40)
        await job
        assert len(rig.sent.says()) == 1
        assert [r[0] for r in rig.store.conn.execute("SELECT dropped FROM utterance")] == [None,"realism_busy","realism_busy"]
        assert not e.typing_now()
    rig.sync(exercise())


@pytest.mark.parametrize("channel",["discord:general","dm:101"])
def test_discord_correction_is_visible_in_feed_but_not_next_prompt(rig,monkeypatch,channel):
    from types import SimpleNamespace
    from leeroy_mind.discord.talk import DiscordTalk
    e = rig.engine
    e.rng = Always()
    e.realism.member(101).credit = 1
    monkeypatch.setattr(e,"_style",lambda *a,**kw:[Styled("ready with a flask","ready with a flask")])
    assert rig.speak_turn(turn(channel),seconds=50)[-1] == "*ready"
    assert rig.store.conn.execute("SELECT count(*) FROM discord_message WHERE text='*ready'").fetchone()[0] == 1
    talk = DiscordTalk(SimpleNamespace(engine=e,world=e.world,personas=e.personas,human_guid=e.human.guid))
    from leeroy_mind.discord.channels import channel_id
    history = talk.context_lines(rig.store.conn,channel_id(channel),None)
    assert "*ready" not in " ".join(history)
    assert "reqdy with a flask" in " ".join(history)


def test_pending_correction_expires_by_elapsed_time_when_wall_clock_moves_back(rig,monkeypatch):
    async def exercise():
        e = rig.engine
        e.rng = Always()
        e.realism.member(101).credit = 1
        monkeypatch.setattr(e,"_style",lambda *a,**kw:[Styled("ready with a flask","ready with a flask")])
        job = asyncio.create_task(e.speak(turn("whisper")))
        for _ in range(80):
            await rig.advance(.5,.5)
            if rig.sent.says():
                break
        async with e._send_lock:
            await rig.advance(11)
            rig.clock._t -= 100
        await rig.advance(1)
        await job
        assert len(rig.sent.says()) == 1
        assert "correction_expired" in [r[0] for r in rig.store.conn.execute("SELECT dropped FROM utterance")]
    rig.sync(exercise())
