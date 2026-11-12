"""Busy-day, observation-window and measured-component review regressions."""
import asyncio
import json
import logging
import threading
import pytest
from tests.claims.conftest import context
from leeroy_mind.learn import night
from leeroy_mind.learn.reward import reward, DAY
from leeroy_mind.signals.store import REPLY_SECONDS, RETENTION_S
from .test_night_cli import enable
from .test_reward import line, signals, screened


def test_thousand_line_day_scores_newest_cap_without_signal_receipts(context,store,clock,tmp_path,caplog):
    enable(tmp_path)
    ids=[line(store,clock,ts=clock.now()-2000+i,text=f"invented event number {i}") for i in range(1000)]
    for lid in ids[:200]:
        screened(store,lid,clock.now())
    async def go():
        async with context(learn_enabled=True) as (ctx,_):
            await night.stage_rewards(ctx)
            await night.stage_rewards(ctx)
    with caplog.at_level(logging.INFO):
        asyncio.run(go())
    rows=store.conn.execute("SELECT line_id,parts FROM talk_reward ORDER BY line_id").fetchall()
    assert [r[0] for r in rows]==ids[-night.PER_NIGHT:]
    assert all(json.loads(r[1])["quality_measured"]==1 for r in rows)
    assert "scored=500" in caplog.text and "left_over=500" in caplog.text


def test_open_window_never_takes_a_slot_and_retries_after_close(context,store,clock,tmp_path,monkeypatch):
    enable(tmp_path)
    monkeypatch.setattr(night,"PER_NIGHT",1)
    old=line(store,clock,ts=clock.now()-REPLY_SECONDS-1)
    young=line(store,clock)
    for lid in (old,young):
        signals(store,lid,clock.now(),explicit=1)
    async def go():
        async with context(learn_enabled=True) as (ctx,_):
            await night.stage_rewards(ctx)
        assert [r[0] for r in store.conn.execute("SELECT line_id FROM talk_reward")]==[old]
        await clock.advance(DAY,step=DAY)
        async with context(learn_enabled=True,night="2026-10-30") as (ctx,_):
            await night.stage_rewards(ctx)
        assert store.conn.execute("SELECT 1 FROM talk_reward WHERE line_id=?",(young,)).fetchone()
    asyncio.run(go())


@pytest.mark.parametrize("text",["","an invented spoken line"])
def test_voice_muted_evidence_reaches_reward(context,store,clock,tmp_path,text):
    enable(tmp_path)
    lid=line(store,clock,channel="voice:guild",text=text,ts=clock.now()-300)
    signals(store,lid,clock.now(),muted=1)
    async def go():
        async with context(learn_enabled=True) as (ctx,_):
            await night.stage_rewards(ctx)
    asyncio.run(go())
    row=store.conn.execute("SELECT value,parts FROM talk_reward WHERE line_id=?",(lid,)).fetchone()
    assert row and row[0]==-1 and json.loads(row[1])["penalties"]==1


def test_thumbs_up_matches_across_game_and_discord_without_engagement(store,clock):
    game=line(store,clock)
    discord=line(store,clock,channel="discord:general")
    for lid in (game,discord):
        signals(store,lid,clock.now(),explicit=1)
    screened(store,game,clock.now())
    a,b=(reward(store.conn,lid,now=clock.now()) for lid in (game,discord))
    assert a.value==pytest.approx(b.value)==1
    assert a.parts["engagement_measured"]==b.parts["engagement_measured"]==0


def test_changed_scan_is_retention_bounded_and_all_scans_run_off_loop(context,store,clock,tmp_path,monkeypatch):
    enable(tmp_path)
    lid=line(store,clock,ts=clock.now()-300)
    signals(store,lid,clock.now(),explicit=1)
    main=threading.get_ident()
    threads=[]
    screen_threads=[]
    original=night._pending
    def inspect(*a,**kw):
        threads.append(threading.get_ident())
        return original(*a,**kw)
    monkeypatch.setattr(night,"_pending",inspect)
    screen=night.screening.screen_row
    def inspected_screen(*a,**kw):
        screen_threads.append(threading.get_ident())
        return screen(*a,**kw)
    monkeypatch.setattr(night.screening,"screen_row",inspected_screen)
    async def go():
        from leeroy_mind.nightly.budget import plan_night
        async with context(learn_enabled=True) as (ctx,_):
            await plan_night(ctx,stages=frozenset({"rewards"}))
            await night.stage_rewards(ctx)
        await clock.advance(RETENTION_S+1,step=RETENTION_S+1)
        signals(store,lid,clock.now(),explicit=-1)
        async with context(learn_enabled=True,night="2027-03-01") as (ctx,_):
            await night.stage_rewards(ctx)
    asyncio.run(go())
    assert threads and main not in threads
    assert screen_threads and main not in screen_threads
    assert store.conn.execute("SELECT revision FROM talk_reward WHERE line_id=?",(lid,)).fetchone()[0]==0


def test_old_saved_plan_cannot_bypass_observation_window(context,store,clock,tmp_path):
    enable(tmp_path)
    lid=line(store,clock)
    async def go():
        async with context(learn_enabled=True) as (ctx,_):
            with store.transaction():
                store.set_meta(night.PLAN+ctx.night,json.dumps([lid]))
            await night.stage_rewards(ctx)
            assert not ctx.done("rewards",str(lid))
            await clock.advance(REPLY_SECONDS,step=REPLY_SECONDS)
            await night.stage_rewards(ctx)
            assert ctx.done("rewards",str(lid))
    asyncio.run(go())
