"""The controller's response-only reward table, on screened and unscreened media."""
import asyncio
import logging
import pytest
from tests.claims.conftest import context
from leeroy_mind.learn import night
from leeroy_mind.learn.reward import reward
from leeroy_mind.nightly import budget
from .test_night_cli import enable
from .test_reward import line, signals, screened


@pytest.mark.parametrize("channel",["guild","whisper","discord:general","dm:101","voice:guild"])
@pytest.mark.parametrize("evidence,expected",[
    ({},0.), ({"explicit":1},1.), ({"explicit":-1},-1.),
    ({"replied":1},.5), ({"ignored":1},-.1), ({"muted":1},-1.),
    ({"dial_down":1},-.5), ({"nudged_against":1},-.5),
])
def test_actual_owner_responses_have_same_sign_and_scale(store,clock,channel,evidence,expected):
    lid=line(store,clock,channel=channel)
    if channel in ("guild","whisper"):
        screened(store,lid,clock.now())
    signals(store,lid,clock.now(),**evidence)
    scored=reward(store.conn,lid,now=clock.now())
    assert scored.value==pytest.approx(expected)
    assert scored.parts["quality"]==0


@pytest.mark.parametrize("evidence,expected",[
    ({"judge_repetition":1},-.2),
    ({"explicit":1,"judge_repetition":1},.8),
    ({"explicit":1,"replied":1},(.4+.25*.5)/.65),
    ({"judge_guard_rewrite":1},-.05),
])
def test_quality_only_deducts_from_measured_responses(store,clock,evidence,expected):
    lid=line(store,clock)
    screened(store,lid,clock.now())
    signals(store,lid,clock.now(),**evidence)
    assert reward(store.conn,lid,now=clock.now()).value==pytest.approx(expected)


def test_failed_precount_preserves_the_night_plan_without_private_error(context,store,clock,tmp_path,monkeypatch,caplog):
    enable(tmp_path)
    async def broken(*args):
        raise RuntimeError("PRIVATE_PATH_OR_THREADPOOL_ERROR")
    monkeypatch.setattr(night,"read_work",broken)
    async def go():
        async with context(learn_enabled=True) as (ctx,_):
            ctx._memo["rewards:count"]=500
            plan=await budget.plan_night(ctx,stages=frozenset({"rewards"}))
            assert night.count(ctx)==0 and "rewards" not in plan["units"]
    with caplog.at_level(logging.INFO):
        asyncio.run(go())
    assert "rewards_unavailable" in caplog.text
    assert "PRIVATE_PATH_OR_THREADPOOL_ERROR" not in caplog.text


def test_reward_budget_reserves_screening_and_optional_embedding_time():
    assert budget.estimate({"rewards":500},measured={})["rewards"]>=500


def test_night_stores_silence_and_negative_responses_as_ruled(context,store,clock,tmp_path):
    enable(tmp_path)
    ids=[]
    expected=[]
    for channel in ("guild","discord:general","voice:guild"):
        for evidence,value in (({},0.),({"muted":1},-1.),({"dial_down":1},-.5),({"explicit":-1},-1.)):
            lid=line(store,clock,channel=channel,ts=clock.now()-300,text="hello")
            signals(store,lid,clock.now(),**evidence)
            ids.append(lid)
            expected.append(value)
    async def go():
        async with context(learn_enabled=True) as (ctx,_):
            await night.stage_rewards(ctx)
    asyncio.run(go())
    assert [store.conn.execute("SELECT value FROM talk_reward WHERE line_id=?",(lid,)).fetchone()[0]
            for lid in ids]==pytest.approx(expected)
