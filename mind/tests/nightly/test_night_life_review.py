"""Independent checks for grounded life text, failure recovery and day-state effects."""
import asyncio
import random

import pytest

from conftest import run
from nightly.conftest import yes
from leeroy_mind.nightly.life import ensure_thread, stage_life
from leeroy_mind.nightly.motives import sample_traits
from leeroy_mind.nightly.wowlife import WowLife, put_wow_life, stage_wowlife


def due(h, *, beats=2):
    h.persona(101, life_thread="thesis deadline")
    tid = ensure_thread(h.store, 101, "thesis deadline", h.T - 5 * 86400, random.Random(3))
    with h.store.transaction():
        h.store.conn.execute("UPDATE life_thread SET beat_n=?,beats_max=8 WHERE id=?", (beats, tid))
    return tid


def context(h, **fields):
    reply = {"beat": "My supervisor pushed the deadline.", "mood": .19,
             "resolved": True, "next_title": "new job", **fields}
    return h.ctx({"life_beat": [reply], "judge": [yes]})


@pytest.mark.parametrize("field", ["beat", "next_title"])
@pytest.mark.parametrize("text", ["Bel|cffffffffra|r visited me.",
                                  "Quil|cfffffffflon|r visited me."])
def test_rendered_names_are_grounded_before_any_life_text_is_written(h, field, text):
    tid = due(h)
    run(stage_life(context(h, **{field: text})))
    assert h.count("life_beat") == 0
    assert h.count("emotion") == 0
    assert h.rows(101) == []
    row = h.store.conn.execute("SELECT status,beat_n FROM life_thread WHERE id=?", (tid,)).fetchone()
    assert tuple(row) == ("active", 2)
    assert h.store.conn.execute("SELECT 1 FROM grounding_log WHERE verdict='rejected'").fetchone()


def test_safe_generated_text_is_normalized_consistently(h):
    due(h)
    run(stage_life(context(h, beat="  My |cffffffffsupervisor|r  pushed\n the deadline. ",
                                next_title="  a |cffffffffnew|r  job  ")))
    assert h.store.conn.execute("SELECT text FROM life_beat").fetchone()[0] == "My supervisor pushed the deadline."
    assert h.rows(101)[0].text == "My supervisor pushed the deadline."
    assert h.store.conn.execute("SELECT title FROM life_thread WHERE guid=101 AND status='active'").fetchone()[0] == "a new job"
    assert h.store.get_state(101).life_thread == "a new job"


@pytest.mark.parametrize("field,value", [("next_beat_at", 9_000_000_000.0), ("beats_max", 7)])
def test_life_does_not_overwrite_a_reschedule_while_embedding(h, monkeypatch, field, value):
    tid = due(h)
    ctx = context(h)
    original = ctx.embed

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()

        async def gated(texts):
            entered.set()
            await release.wait()
            return await original(texts)

        monkeypatch.setattr(ctx, "embed", gated)
        task = asyncio.create_task(stage_life(ctx))
        await asyncio.wait_for(entered.wait(), 5)
        assert h.store._depth == 0
        with h.store.transaction():
            h.store.conn.execute(f"UPDATE life_thread SET {field}=? WHERE id=?", (value, tid))
        release.set()
        await task

    run(scenario())
    assert h.count("life_beat") == h.count("emotion") == 0
    row = h.store.conn.execute("SELECT * FROM life_thread WHERE id=?", (tid,)).fetchone()
    assert row[field] == value and row["beat_n"] == 2 and row["status"] == "active"
    assert not ctx.done("life", "101") and ctx.failures["life"] == 1


def test_cancelled_embedding_leaves_the_life_unit_retryable(h, monkeypatch):
    due(h)
    ctx = context(h)

    async def scenario():
        entered = asyncio.Event()

        async def gated(texts):
            entered.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(ctx, "embed", gated)
        task = asyncio.create_task(stage_life(ctx))
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    run(scenario())
    assert not ctx.done("life", "101")
    assert h.count("life_beat") == h.count("emotion") == 0 and h.rows(101) == []
    run(stage_life(context(h)))
    assert h.count("life_beat") == 1 and len(h.rows(101)) == 1


@pytest.mark.parametrize("mood", [.1, -.1])
def test_actual_life_then_wowlife_uses_small_mood_and_new_title_after_window_close(h, mood):
    due(h)
    with h.store.transaction():
        put_wow_life(h.store, WowLife(101, pressure=.9, guilt=.2))
    ctx = h.ctx({"life_beat": [{"beat": "My supervisor approved the thesis.", "mood": mood,
                                "resolved": True, "next_title": "new job"}],
                 "wow_line": [{"line": "Enough game time tonight", "mood": 0.0}],
                 "judge": [yes, yes]})
    # The night can write a beat after the immutable evidence window closed.
    # Baseline trait sampling uses the same seed but no life memory yet.
    ctx.window = (h.T - 1000, h.T - 1)
    before = sample_traits(ctx, 101)
    with h.store.transaction():
        h.store.conn.execute("DELETE FROM trait_state WHERE night=? AND guid=101", (ctx.night,))
    run(stage_life(ctx))
    run(stage_wowlife(ctx))
    after = sample_traits(ctx, 101)
    assert h.count("emotion") == 0
    assert after["extraversion"] == pytest.approx(before["extraversion"] + .5 * mood)
    assert after["neuroticism"] == pytest.approx(before["neuroticism"] - mood)
    assert all(after[name] == before[name] for name in
               ("openness", "conscientiousness", "agreeableness"))
    assert h.store.get_state(101).life_thread == "Enough game time tonight; new job"
    assert h.rows(101)[0].valence == mood
    tables = ("life_thread", "life_beat", "emotion", "motive", "trait_state", "wow_hours", "night_step")
    counts = {table: h.count(table) for table in tables}
    calls = len(ctx.reasoner.llm.calls)
    run(stage_life(ctx))
    run(stage_wowlife(ctx))
    assert {table: h.count(table) for table in tables} == counts
    assert len(ctx.reasoner.llm.calls) == calls
    assert sample_traits(ctx, 101) == after
