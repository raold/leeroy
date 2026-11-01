"""Private fictional life advances once per night, with grounded text and atomic writes."""
import asyncio
import json
import random

import pytest

from conftest import run
from nightly.conftest import no_real_name, yes
from leeroy_mind.nightly import budget
from leeroy_mind.nightly.life import (BEAT_SCHEMA, advance, due_threads, ensure_thread,
                                     stage_life)
from leeroy_mind.nightly.llm import check_schema

DAY = 86400
BEAT = {"beat": "My supervisor pushed the deadline.", "mood": 0.5,
        "resolved": False, "next_title": None}


def due(h, *, age=5, beats=0, maximum=8):
    h.persona(101, life_thread="thesis deadline")
    tid = ensure_thread(h.store, 101, "thesis deadline", h.T - age * DAY, random.Random(1))
    with h.store.transaction():
        h.store.conn.execute("UPDATE life_thread SET beat_n=?, beats_max=? WHERE id=?",
                             (beats, maximum, tid))
    return tid


def ctx_for(h, **reply):
    return h.ctx({"life_beat": [{**BEAT, **reply}], "judge": [yes]})


def test_due_beat_written_as_offscreen_memory(h):
    tid = due(h)
    ctx = ctx_for(h)
    run(stage_life(ctx))
    memory, = [m for m in h.rows(101) if m.source == "offscreen"]
    assert (memory.kind, memory.importance, memory.actors, memory.valence) == ("event", 5, (), .5)
    assert memory.evidence[0].startswith("beat:") and memory.embedding
    assert (memory.scope, memory.scope_with) == ("open", 0)
    row = h.store.conn.execute("SELECT * FROM life_thread WHERE id=?", (tid,)).fetchone()
    assert row["beat_n"] == 1 and h.T + 2 * DAY <= row["next_beat_at"] <= h.T + 5 * DAY
    assert h.store.get_state(101).life_thread == "thesis deadline"
    emotion = h.store.conn.execute("SELECT type,intensity,source,evidence FROM emotion").fetchone()
    assert tuple(emotion[:3]) == ("joy", 1, "life")
    assert json.loads(emotion[3]) == list(memory.evidence)
    assert ctx.done("life", "101")


def test_one_beat_per_night_on_catch_up_and_fresh_context(h):
    due(h, age=30)
    run(stage_life(ctx_for(h)))
    retry = h.ctx()
    run(stage_life(retry))
    assert h.count("life_beat") == 1 and retry.reasoner.llm.calls == []


@pytest.mark.parametrize("beats,maximum,resolved", [(2, 8, True), (2, 3, False)])
def test_resolution_starts_new_thread_atomically(h, beats, maximum, resolved):
    tid = due(h, beats=beats, maximum=maximum)
    run(stage_life(ctx_for(h, resolved=resolved, next_title="new job")))
    old = h.store.conn.execute("SELECT * FROM life_thread WHERE id=?", (tid,)).fetchone()
    new = h.store.conn.execute("SELECT * FROM life_thread WHERE guid=101 AND status='active'").fetchone()
    assert old["status"] == "resolved" and old["resolved"] == h.T and old["beat_n"] == 3
    assert new["title"] == "new job" and new["beat_n"] == 0
    assert h.T + DAY <= new["next_beat_at"] <= h.T + 3 * DAY
    assert h.store.get_state(101).life_thread == "new job"
    assert h.rows(101)[0].importance == 6


def test_model_cannot_resolve_before_third_beat(h):
    tid = due(h)
    run(stage_life(ctx_for(h, resolved=True, next_title="new job")))
    row = h.store.conn.execute("SELECT status,beat_n FROM life_thread WHERE id=?", (tid,)).fetchone()
    assert tuple(row) == ("active", 1) and h.rows(101)[0].importance == 5


def test_resolution_without_next_title_uses_current_persona(h):
    due(h, beats=2)
    run(stage_life(ctx_for(h, resolved=True)))
    assert h.store.conn.execute("SELECT title FROM life_thread WHERE guid=101 AND status='active'").fetchone()[0] == "thesis deadline"


@pytest.mark.parametrize("field,text", [("beat", "Belra visited me."),
                                        ("next_title", "Belra is moving in"),
                                        ("next_title", "Quillon is moving in")])
def test_unallowed_name_in_beat_or_replacement_title_rejected(h, field, text):
    tid = due(h, beats=2)
    ctx = ctx_for(h, resolved=True, **{field: text})
    run(stage_life(ctx))
    assert h.count("life_beat") == 0 and h.rows(101) == []
    assert h.store.conn.execute("SELECT beat_n FROM life_thread WHERE id=?", (tid,)).fetchone()[0] == 2
    assert h.store.conn.execute("SELECT count(*) FROM grounding_log WHERE verdict='rejected'").fetchone()[0] >= 1
    assert no_real_name(h.store) == []


@pytest.mark.parametrize("mood,kind,intensity", [(0.19, None, None), (-0.19, None, None),
                                                (0.2, "joy", 1), (-0.6, "distress", 2)])
def test_mood_thresholds_keep_small_valence_in_memory(h, mood, kind, intensity):
    due(h)
    run(stage_life(ctx_for(h, mood=mood)))
    assert h.rows(101)[0].valence == mood
    rows = h.store.conn.execute("SELECT type,intensity FROM emotion").fetchall()
    assert [tuple(r) for r in rows] == ([] if kind is None else [(kind, intensity)])


def test_memory_failure_rolls_back_beat_emotion_and_resolution(h, monkeypatch):
    tid = due(h, beats=2)
    ctx = ctx_for(h, resolved=True, next_title="new job")
    def fail(*args, **kwargs):
        raise RuntimeError("injected memory failure")
    monkeypatch.setattr(ctx.writer, "put", fail)
    run(stage_life(ctx))
    assert h.count("life_beat") == h.count("emotion") == h.count("grounding_log") == 0
    assert tuple(h.store.conn.execute("SELECT status,beat_n FROM life_thread WHERE id=?", (tid,)).fetchone()) == ("active", 2)
    assert not ctx.done("life", "101") and ctx.failures["life"] == 1


def test_thread_ensure_is_idempotent_and_due_is_bounded(h):
    h.persona(101, life_thread="thesis deadline")
    tid = ensure_thread(h.store, 101, "thesis deadline", h.T, random.Random(2))
    same = ensure_thread(h.store, 101, "ignored replacement", h.T + DAY, random.Random(8))
    assert same == tid and due_threads(h.store, h.T) == []
    row, = due_threads(h.store, h.T + 3 * DAY)
    assert row["id"] == tid and 3 <= row["beats_max"] <= 8


def test_only_three_latest_beats_enter_prompt(h):
    tid = due(h)
    with h.store.transaction():
        for i in range(5):
            h.store.conn.execute("INSERT INTO life_beat(thread_id,night,text,ts) VALUES (?,?,?,?)",
                                 (tid, "2026-10-28", f"Earlier detail {i}", h.T - 10 + i))
    ctx = ctx_for(h)
    run(stage_life(ctx))
    call = next(c for c in ctx.reasoner.llm.calls if (c.get("format") or {}).get("title") == "life_beat")
    prompt = "\n".join(m["content"] for m in call["messages"])
    assert all(f"Earlier detail {i}" in prompt for i in (2, 3, 4))
    assert all(f"Earlier detail {i}" not in prompt for i in (0, 1))
    assert call["options"]["temperature"] == .8


def test_life_budget_registered_and_counts_once_per_member(h):
    due(h, age=30)
    with h.store.transaction():
        h.store.conn.execute("INSERT INTO life_thread(guid,title,beats_max,next_beat_at,status,started) VALUES (101,'duplicate',5,?,'active',?)", (h.T, h.T))
        h.store.conn.execute("INSERT INTO life_thread(guid,title,beats_max,next_beat_at,status,started) VALUES (102,'future',5,?,'active',?)", (h.T + 5 * DAY, h.T))
    assert budget.COUNTERS["life"](h.ctx()) == 1


def test_schema_limits():
    check_schema(BEAT_SCHEMA, BEAT)
    for bad in ({**BEAT, "mood": 1.1}, {**BEAT, "beat": "x" * 201},
                {**BEAT, "next_title": "x" * 81}):
        with pytest.raises(ValueError):
            check_schema(BEAT_SCHEMA, bad)
