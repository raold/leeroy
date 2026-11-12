import asyncio
import json

import pytest

from tests.claims.conftest import context
from .test_recipe import rig
from .test_reward import line, signals
from leeroy_mind.learn import night, recipe, timing
from leeroy_mind.learn.reward import reward
from leeroy_mind.nightly import budget
from leeroy_mind.talk.pipeline import TurnMeta
from leeroy_mind.talk.turns import Turn, TurnKind


@pytest.mark.parametrize('channel', ['guild', 'whisper', 'discord:general', 'dm:101', 'voice:room'])
def test_fragments_share_one_recipe_and_pool_feedback_without_silence_dilution(rig, channel):
    rig.engine.learn_enabled = True
    turn = Turn(TurnKind.REPLY_HUMAN, 101, channel, to_guid=1)
    meta = TurnMeta(rig.engine._new_id(), realism_planned=True)
    p = rig.engine.personas.get(101)
    ids = [rig.engine._record(turn,meta,p,text,[]) for text in ['a road', 'over the hills', 'past the lake']]
    assert rig.store.conn.execute('SELECT count(*) FROM talk_recipe').fetchone()[0] == 1
    assert [tuple(r) for r in rig.store.conn.execute('SELECT * FROM talk_recipe_line')] == [(i,ids[0]) for i in ids]
    assert reward(rig.store.conn,ids[0],now=rig.clock.now()).value == 0
    signals(rig.store,ids[2],rig.clock.now(),explicit=1)
    assert reward(rig.store.conn,ids[0],now=rig.clock.now()).value == 1
    signals(rig.store,ids[1],rig.clock.now(),explicit=-1)
    assert reward(rig.store.conn,ids[2],now=rig.clock.now()).value == -1
    # Another generation by the same speaker is never folded into this one.
    newer = rig.engine._record(turn,TurnMeta(rig.engine._new_id()),p,'new thought',[])
    assert reward(rig.store.conn,newer,now=rig.clock.now()).value == 0
    assert rig.store.conn.execute('SELECT count(*) FROM talk_recipe').fetchone()[0] == 2


def test_recipe_savepoint_failure_does_not_leave_a_stale_anchor(rig, monkeypatch):
    rig.engine.learn_enabled = True
    turn = Turn(TurnKind.REPLY_HUMAN,101,'guild')
    meta = TurnMeta('fixture')
    p = rig.engine.personas.get(101)
    original = recipe.record
    def broken(*args):
        original(*args)
        raise RuntimeError('fixture')
    monkeypatch.setattr(recipe,'record',broken)
    first = rig.engine._record(turn,meta,p,'first',[])
    monkeypatch.setattr(recipe,'record',original)
    second = rig.engine._record(turn,meta,p,'second',[])
    assert meta.recipe_line_id == second != first
    assert [tuple(r) for r in rig.store.conn.execute('SELECT * FROM talk_recipe_line')] == [(second,second)]


@pytest.mark.parametrize('channel', ['guild','discord:general','dm:101'])
def test_night_waits_for_last_fragment_and_late_vote_revises_single_credit(context,store,clock,tmp_path,channel):
    (tmp_path/'mind.toml').write_text('[learn]\nenabled=true\n')
    async def go():
        async with context(learn_enabled=True) as (ctx,_):
            ctx.window = (0,clock.now()+1000)
            ids = [line(store,clock,channel=channel,ts=clock.now()-t) for t in [300,60]]
            from leeroy_mind.config import load_config
            meta=TurnMeta('fixture')
            turn=Turn(TurnKind.REPLY_HUMAN,101,channel)
            with store.transaction():
                for i in ids:
                    recipe.record(store.conn,i,turn,meta,ctx.persona(101),load_config(tmp_path/'mind.toml'))
            assert night._pending(ctx) == []
            await clock.advance(61)
            assert night._pending(ctx) == [ids[0]]
            signals(store,ids[1],clock.now(),explicit=1)
            await night.stage_rewards(ctx)
            row = store.conn.execute('SELECT * FROM talk_reward').fetchone()
            assert row['line_id'] == ids[0] and row['value'] == 1 and row['revision'] == 0
            # Same-timestamp vote changes must also invalidate the old generation.
            from leeroy_mind.signals.store import feedback
            with store.transaction():
                feedback(store.conn,dict(line_id=ids[1],value=-1,reasons=[]),1,clock.now())
            await night.stage_rewards(ctx)
            row = store.conn.execute('SELECT * FROM talk_reward').fetchone()
            assert row['value'] == -1 and row['revision'] == 1
            assert json.loads(row['parts'])['line_count'] == 2
            await night.stage_rewards(ctx)
            assert store.conn.execute('SELECT count(*) FROM talk_reward').fetchone()[0] == 1
            assert store.conn.execute('SELECT sum(units) FROM reward_timing').fetchone()[0] == 2
    asyncio.run(go())


def test_reward_planner_learns_separate_path_costs_and_keeps_seven_nights(context,store,tmp_path):
    async def go():
        async with context(learn_enabled=True) as (ctx,_):
            assert timing.planning_seconds(ctx) == .02
            with store.transaction():
                timing.record_timing(store.conn,'2026-11-01','lexical',.03)
                timing.record_timing(store.conn,'2026-11-01','lexical',.05)
                timing.record_timing(store.conn,'2026-11-01','embedding',.4)
            assert timing.planning_seconds(ctx) == pytest.approx(.04)
            (tmp_path/'mind.toml').write_text('[claims]\nenabled=true\n')
            assert timing.planning_seconds(ctx) == .4
            for day in range(2,10):
                with store.transaction():
                    timing.record_timing(store.conn,f'2026-11-{day:02}','embedding',.1)
            assert timing.planning_seconds(ctx) == .1
            assert store.conn.execute('SELECT count(DISTINCT night) FROM reward_timing').fetchone()[0] == 7
            await night.prepare_count(ctx)
            assert ctx._memo['rewards:seconds'] == .1
            assert budget.estimate({'rewards':500},measured={},unit_seconds={'rewards':.1})['rewards'] == 50
    asyncio.run(go())


@pytest.mark.parametrize('embedding',[False,True])
def test_completed_unit_records_its_own_path_timing(context,store,clock,tmp_path,monkeypatch,embedding):
    (tmp_path/'mind.toml').write_text('[learn]\nenabled=true\n' + ('[claims]\nenabled=true\n' if embedding else ''))
    ticks=iter([10.,10.125])
    monkeypatch.setattr(night,'perf_counter',lambda:next(ticks))
    async def go():
        async with context(learn_enabled=True) as (ctx,_):
            line(store,clock,ts=clock.now()-300)
            await night.stage_rewards(ctx)
            assert tuple(store.conn.execute('SELECT mode,units,seconds FROM reward_timing').fetchone()) == (
                'embedding' if embedding else 'lexical',1,.125)
    asyncio.run(go())
