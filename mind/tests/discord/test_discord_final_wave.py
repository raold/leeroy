"""D1 review regressions; invented cast/text, fake model admin, no live services."""
import asyncio
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from leeroy_mind.discord import talk as discord_talk
from leeroy_mind.discord.presence import local_time
from leeroy_mind.discord.text import is_one_emoji
from leeroy_mind.memory.privacy import restricted_channels
from leeroy_mind.nightly.gpu import GpuArbiter
from tests.discord.test_discord_talk import (FakeAdmin, FakeArbiter, answer, answers, clock,
                                           post, rig_for, speakers, talk_for)


@pytest.mark.parametrize("month,day,offset", [(1, 5, 5), (3, 15, 4), (7, 5, 5), (10, 29, 4)])
def test_regional_clock_handles_different_dst_change_dates(month, day, offset):
    naive = datetime(2026, month, day, 12)
    assert local_time(naive, "UK").hour == 12 + offset
    aware = naive.replace(tzinfo=ZoneInfo("America/New_York"))
    assert local_time(aware, "UK").hour == 12 + offset
    assert local_time(aware, "UK").timestamp() == aware.timestamp()
    assert local_time(naive, "US") == naive


@pytest.mark.parametrize("emoji", ["‼️", "⁉️", "™️", "ℹ️", "↔️", "Ⓜ️", "▪️", "⤴️", "〰️", "〽️", "㊗️", "㊙️", "©️", "®️"])
def test_bmp_emoji_bases_are_accepted(emoji):
    assert is_one_emoji(emoji)
    assert not is_one_emoji(emoji + "x")


def test_game_afk_does_not_hide_a_discord_member_but_away_still_does(store, clock, tmp_path, monkeypatch):
    rig = rig_for(store, clock, tmp_path, reply="hello there")
    talk = talk_for(rig)
    rig.world.afk.update(rig.world.online)
    mid = post(rig, "general", "anyone around?")
    assert speakers(talk, mid)[0]
    named = post(rig, "general", "@Arkon you there?")
    answer(rig, talk, named)
    assert [m["author"] for m in answers(rig, "general", named)] == [101]
    monkeypatch.setattr(discord_talk, "away_now", lambda *_: frozenset(rig.world.members))
    assert speakers(talk, mid) == [[]]
    turn, meta = talk.plan(named)[0]
    monkeypatch.setattr(rig.engine, "_away_now", lambda: frozenset({101}))
    assert rig.engine._blocked(turn, meta, clock.now(), first=True) == "away"
    rig.close()


def test_his_post_is_not_repeated_in_recent_context(store, clock, tmp_path):
    rig = rig_for(store, clock, tmp_path)
    talk = talk_for(rig)
    mid = post(rig, "general", "@Arkon the invented garden gate is blue")
    turn, _ = talk.plan(mid)[0]
    assert turn.instruction.count("invented garden gate is blue") == 1
    rig.close()


def test_no_event_loop_takes_no_unreleasable_lease(store, clock, tmp_path):
    rig = rig_for(store, clock, tmp_path)
    arbiter = FakeArbiter()
    talk = talk_for(rig, arbiter=arbiter)
    talk._take_lease()
    assert not arbiter.holders and not talk._held
    rig.close()


def test_shutdown_waits_for_the_final_let_go(store, clock, tmp_path):
    async def go():
        rig = rig_for(store, clock, tmp_path)
        gate, entered = asyncio.Event(), asyncio.Event()

        class Admin(FakeAdmin):
            async def preload(self, model, keep_alive, *, options=None):
                if keep_alive != -1:
                    entered.set()
                    await gate.wait()
                await super().preload(model, keep_alive, options=options)

        cfg = rig.cfg.models
        admin = Admin([cfg.live])
        arbiter = GpuArbiter(admin, SimpleNamespace(live_model=cfg.live,
            reflection_model=cfg.reflection, embedding_model=cfg.embedding,
            ollama_url="http://127.0.0.1:1"))
        talk = talk_for(rig, arbiter=arbiter)
        talk._take_lease()
        await talk.handback()
        closing = asyncio.create_task(talk.close())
        try:
            await asyncio.wait_for(entered.wait(), 2)
            assert not closing.done()
        finally:
            gate.set()
            await closing
            await arbiter.settled()
        assert not arbiter.holders
    asyncio.run(go())


def test_restriction_walk_batches_direct_refs_and_skips_empty_refs(store):
    reads = []
    store.conn.set_trace_callback(reads.append)
    try:
        assert restricted_channels(store.conn, {1: ["owner:1"], 2: []}) == {}
        assert not [q for q in reads if q.startswith("SELECT")]
        assert restricted_channels(store.conn, {1: ["chat:101", "chat:102"], 2: ["chat:103"]}) == {}
        assert len([q for q in reads if q.startswith("SELECT")]) == 1
    finally:
        store.conn.set_trace_callback(None)
