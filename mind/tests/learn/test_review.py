"""Reward review regressions: synthetic lines, fake clock, no model/service calls."""
import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from tests.claims.conftest import context
from leeroy_mind.learn import night, recipe
from leeroy_mind.learn.reward import reward
from leeroy_mind.signals.store import feedback
from .test_night_cli import enable
from .test_recipe import rig, record
from .test_reward import line, signals, screened


def test_late_thumbs_down_updates_reward_after_first_night(context,store,clock,tmp_path):
    enable(tmp_path)
    lid = line(store,clock,ts=clock.now()-300)
    screened(store,lid,clock.now())
    signals(store,lid,clock.now(),explicit=1)
    async def exercise():
        async with context(learn_enabled=True) as (ctx,_):
            ctx.window = (clock.now()-86400,clock.now()+1)
            await night.stage_rewards(ctx)
        assert store.conn.execute("SELECT value FROM talk_reward WHERE line_id=?",(lid,)).fetchone()[0] > 0
        with store.transaction():
            feedback(store.conn,dict(line_id=lid,value=-1,reasons=[]),1,clock.now()+1)
        await clock.advance(86400,step=86400)
        async with context(night="2026-10-30",learn_enabled=True) as (ctx,_):
            ctx.window = (clock.now()-86400,clock.now()+1)
            await night.stage_rewards(ctx)
        assert store.conn.execute("SELECT value FROM talk_reward WHERE line_id=?",(lid,)).fetchone()[0] <= -.4
    asyncio.run(exercise())


def test_daily_rank_does_not_reset_at_utc_midnight(store,clock):
    zone = ZoneInfo("America/New_York")
    before = datetime(2026,11,10,18,59,tzinfo=zone).timestamp()
    for n in range(20):
        line(store,clock,ts=before-n)
    first = line(store,clock,ts=before+1)
    second = line(store,clock,ts=before+121)
    for lid in (first,second):
        signals(store,lid,before+500,explicit=1)
    a,b = (reward(store.conn,lid,now=before+500,local_at=lambda ts:datetime.fromtimestamp(ts,zone)) for lid in (first,second))
    assert a.parts["member_day_rank"] == 20 and b.parts["member_day_rank"] == 21
    assert a.value == b.value == .4


def test_discord_volume_does_not_discount_game_credit(store,clock):
    for n in range(20):
        line(store,clock,channel="dm:101",ts=clock.now()-100+n)
    lid = line(store,clock)
    signals(store,lid,clock.now(),explicit=1)
    scored = reward(store.conn,lid,now=clock.now())
    assert scored.value == .4 and scored.parts["member_day_rank"] == 0


def test_unscreened_game_line_stays_pending_and_retries(context,store,clock,tmp_path):
    enable(tmp_path)
    lid = line(store,clock,ts=clock.now()-300)
    signals(store,lid,clock.now(),explicit=1)
    async def exercise():
        async with context(learn_enabled=True) as (ctx,_):
            ctx.window = (clock.now()-86400,clock.now()+1)
            await night.stage_rewards(ctx)
            assert store.conn.execute("SELECT 1 FROM talk_reward WHERE line_id=?",(lid,)).fetchone() is None
            assert not ctx.done("rewards",str(lid))
            screened(store,lid,clock.now())
            await night.stage_rewards(ctx)
            assert store.conn.execute("SELECT value FROM talk_reward WHERE line_id=?",(lid,)).fetchone()[0] == pytest.approx(.6)
    asyncio.run(exercise())


def test_cold_engagement_is_neutral_and_ignored_is_negative(store,clock):
    a,b,c = [line(store,clock) for _ in range(3)]
    signals(store,b,clock.now(),replied=1)
    signals(store,c,clock.now(),ignored=1)
    assert reward(store.conn,a,now=clock.now()).value == 0
    assert reward(store.conn,b,now=clock.now()).parts["engagement"] == .5
    assert reward(store.conn,c,now=clock.now()).value < 0


def test_newest_lines_get_the_bounded_scoring_slots(context,store,clock,tmp_path,monkeypatch):
    enable(tmp_path)
    monkeypatch.setattr(night,"PER_NIGHT",2)
    ids = [line(store,clock,ts=clock.now()-300+n) for n in range(4)]
    for lid in ids:
        screened(store,lid,clock.now())
    async def exercise():
        async with context(learn_enabled=True) as (ctx,_):
            ctx.window = (clock.now()-86400,clock.now()+1)
            await night.stage_rewards(ctx)
    asyncio.run(exercise())
    assert [r[0] for r in store.conn.execute("SELECT line_id FROM talk_reward ORDER BY line_id")] == ids[-2:]


def test_recipe_failure_keeps_acknowledged_chat_and_rolls_back_only_recipe(rig,monkeypatch):
    rig.engine.learn_enabled = True
    original = recipe.record
    def broken(*args,**kwargs):
        original(*args,**kwargs)
        raise RuntimeError("private detail must not escape")
    monkeypatch.setattr(recipe,"record",broken)
    lid = record(rig)
    assert rig.store.conn.execute("SELECT 1 FROM chat_line WHERE id=?",(lid,)).fetchone()
    assert not rig.store.conn.execute("SELECT 1 FROM talk_recipe").fetchone()


def test_recipe_hash_tracks_template_edits_and_names_actual_turn(rig):
    from leeroy_mind.talk.turns import Turn,TurnKind
    p = rig.engine.personas.get(101)
    a = recipe.describe(Turn(TurnKind.REPLY_HUMAN,101,"guild",template="first invented template"),p,rig.cfg)
    b = recipe.describe(Turn(TurnKind.GREET,101,"guild",template="second invented template"),p,rig.cfg)
    assert a.template_id != b.template_id
    assert a.template_version != b.template_version


def test_same_night_veto_replaces_credit_even_at_same_receipt_time(context,store,clock,tmp_path):
    enable(tmp_path)
    lid = line(store,clock,ts=clock.now()-300)
    screened(store,lid,clock.now())
    signals(store,lid,clock.now(),explicit=1)
    async def exercise():
        async with context(learn_enabled=True) as (ctx,_):
            await night.stage_rewards(ctx)
            with store.transaction():
                feedback(store.conn,dict(line_id=lid,value=-1,reasons=[]),1,clock.now())
            await night.stage_rewards(ctx)
            assert store.conn.execute("SELECT value FROM talk_reward WHERE line_id=?",(lid,)).fetchone()[0] <= -.4
    asyncio.run(exercise())


def test_stored_raw_credit_and_cap_are_separate_and_pending_count_is_logged(context,store,clock,tmp_path,caplog):
    enable(tmp_path)
    for n in range(20):
        line(store,clock,ts=clock.now()-300+n)
    lid = line(store,clock,ts=clock.now()-100)
    screened(store,lid,clock.now())
    signals(store,lid,clock.now(),explicit=1)
    async def exercise():
        async with context(learn_enabled=True) as (ctx,_):
            await night.stage_rewards(ctx)
    import logging
    with caplog.at_level(logging.INFO):
        asyncio.run(exercise())
    row = store.conn.execute("SELECT value,cap_weight FROM talk_reward WHERE line_id=?",(lid,)).fetchone()
    assert row[0] == pytest.approx(.6) and row[1] == pytest.approx(.9**20)
    assert "rewards_pending" in caplog.text and "20" in caplog.text


@pytest.mark.parametrize("hour",[1,3,22])
def test_local_day_cap_survives_dst_change(store,clock,hour):
    zone = ZoneInfo("America/New_York")
    start = datetime(2026,11,1,0,1,tzinfo=zone).timestamp()
    line(store,clock,ts=start)
    ts = datetime(2026,11,1,hour,30,tzinfo=zone).timestamp()
    lid = line(store,clock,ts=ts)
    assert reward(store.conn,lid,now=ts,local_at=lambda t:datetime.fromtimestamp(t,zone)).parts["member_day_rank"] == 1


def test_schema_is_cached_before_per_line_work(context,store,clock,tmp_path):
    enable(tmp_path)
    assert night.ready(store)
    statements = []
    store.conn.set_trace_callback(statements.append)
    async def exercise():
        async with context(learn_enabled=True) as (ctx,_):
            lid = line(store,clock,ts=clock.now()-300)
            screened(store,lid,clock.now())
            await night.stage_rewards(ctx)
            night.count(ctx)
    try:
        asyncio.run(exercise())
    finally:
        store.conn.set_trace_callback(None)
    assert not any("sqlite_master" in s for s in statements)


def test_describe_failure_does_not_lose_composition_or_record(rig,monkeypatch,caplog):
    from leeroy_mind.talk.pipeline import TurnMeta
    from leeroy_mind.talk.turns import Turn,TurnKind
    rig.engine.learn_enabled = True
    def broken(*args):
        raise ValueError("private analytics detail")
    monkeypatch.setattr(recipe,"describe",broken)
    turn = Turn(TurnKind.REPLY_HUMAN,101,"guild",addressee=1)
    meta = TurnMeta(rig.engine._new_id())
    rig.sync(rig.engine._compose(turn,meta,live=True))
    lid = record(rig)
    assert store_count(rig.store,"chat_line") == 1 and lid
    assert "learn_recipe_describe_failed" in caplog.text
    assert "private analytics detail" not in caplog.text


def store_count(store,table):
    return store.conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


def test_builtin_prompt_edit_changes_captured_version(rig,monkeypatch):
    from leeroy_mind.talk import prompt
    from leeroy_mind.talk.pipeline import TurnMeta
    from leeroy_mind.talk.turns import Turn,TurnKind
    rig.engine.learn_enabled = True
    turn = Turn(TurnKind.REPLY_HUMAN,101,"guild",addressee=1)
    def compose():
        meta = TurnMeta(rig.engine._new_id())
        rig.sync(rig.engine._compose(turn,meta,live=True))
        return meta.recipe_snapshot
    first = compose()
    monkeypatch.setattr(prompt,"COMMS_RULES",prompt.COMMS_RULES+"\nInvented revision.")
    second = compose()
    assert first.template_version != second.template_version
    assert "talk.instruction.reply_human" in second.template_id
    assert "Invented revision" not in second.template_id


def test_active_overlay_edit_changes_recipe_before_delivery(rig,tmp_path,monkeypatch):
    from leeroy_mind.talk.pipeline import TurnMeta
    from leeroy_mind.talk.turns import Turn,TurnKind
    from leeroy_mind.talk.romance import _band_text
    rig.engine.learn_enabled = True
    # Exercise the real overlay selector inside the engine's composition trace.
    monkeypatch.setattr(rig.engine,"_romance",lambda *a,**kw:_band_text("suggestive",tmp_path,None))
    path = tmp_path / "prompts" / "romance_suggestive.txt"
    path.parent.mkdir(exist_ok=True)
    turn = Turn(TurnKind.REPLY_HUMAN,101,"guild",addressee=1)
    def compose(text):
        path.write_text(text)
        meta = TurnMeta(rig.engine._new_id())
        rig.sync(rig.engine._compose(turn,meta,live=True))
        return meta
    first = compose("Invented warm greeting.")
    second = compose("Invented friendly wave.")
    assert first.recipe_snapshot.template_version != second.recipe_snapshot.template_version
    assert "romance_suggestive" in second.recipe_snapshot.template_id
    with rig.store.transaction():
        lid = line(rig.store,rig.clock)
        recipe.record(rig.store.conn,lid,turn,first,rig.engine.personas.get(101),rig.cfg)
    assert rig.store.conn.execute("SELECT template_version FROM talk_recipe WHERE line_id=?",(lid,)).fetchone()[0] == first.recipe_snapshot.template_version


def test_late_implicit_evidence_at_same_timestamp_is_revisited(context,store,clock,tmp_path):
    enable(tmp_path)
    lid = line(store,clock,ts=clock.now()-300)
    screened(store,lid,clock.now())
    async def exercise():
        async with context(learn_enabled=True) as (ctx,_):
            await night.stage_rewards(ctx)
            signals(store,lid,clock.now(),muted=1)
            await night.stage_rewards(ctx)
            assert store.conn.execute("SELECT value FROM talk_reward WHERE line_id=?",(lid,)).fetchone()[0] == -.8
    asyncio.run(exercise())
