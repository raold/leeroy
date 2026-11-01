"""The night runner, its context, the night's GPU budget and the snapshot (P1c Task 5). Fakes
only: a FakeLLM answers for the models and a FakeAdmin stands in for Ollama's model admin, so no
model is loaded. Every name here is invented."""
import asyncio
import json
import logging
import random
import re
import sqlite3
import time
from dataclasses import replace

import pytest

from conftest import TEST_CAST, make_mem, make_persona, run
from leeroy_mind.app import CANCEL_WAIT_S
from leeroy_mind.llm.client import ChatResult
from leeroy_mind.llm.scheduler import Priority
from leeroy_mind.nightly import budget
from leeroy_mind.nightly.budget import (DEGRADE, PLAN_S, UNIT_CALLS, estimate, measured_call_s,
                                        night_budget_s, plan_night, register_counter)
from leeroy_mind.nightly.gpu import GpuArbiter
from leeroy_mind.nightly.llm import ReflectionError
from leeroy_mind.nightly.runner import (ATTITUDE_WORDS, ATTITUDES, RESUMES_MAX, NightRunner,
                                        Stage, stage_snapshot)
from leeroy_mind.nightly.scheduler import NightScheduler
from leeroy_mind.nightly.settings import night_bounds
from leeroy_mind.nightly.store import (commit_unit, refund_resume, run_begin, run_finish, run_get,
                                       set_run_plan, stage_record, step_done)
from leeroy_mind.personas.card import render_prompt_card
from leeroy_mind.personas.io import save_persona
from leeroy_mind.protocol import Member
from leeroy_mind.testing.fake_llm import FakeLLM
from leeroy_mind.wow import CLASSES, RACES
from nightly.conftest import UTC, FakeAdmin

TASK5_COUNTERS = ("reflect", "reflect_small", "encounters", "notes", "wowlife", "stories", "recap",
                  "chronicle", "eval")


class TimedLLM(FakeLLM):
    """A FakeLLM whose every chat took `seconds` (ChatResult.seconds; P1b's FakeLLM says 0)."""

    def __init__(self, seconds: float, **kw):
        super().__init__(**kw)
        self.seconds = seconds

    async def chat(self, *args, **kw):
        r = await super().chat(*args, **kw)
        return ChatResult(r.text, r.thinking, r.eval_count, self.seconds)


async def stray(*_) -> None:
    """A cancel nobody asked for (Task 4 re-review N2): an awaited future someone else
    cancelled, so the task itself is not being cancelled."""
    fut = asyncio.get_running_loop().create_future()
    fut.cancel()
    await fut


async def until(cond, tries: int = 5000) -> None:
    for _ in range(tries):
        if cond():
            return
        await asyncio.sleep(0)
    raise AssertionError("the condition never held")


def night_scheduler(h, stages, *, gate=None):
    """P1c's NightScheduler running a NightRunner over `stages` on h's store and clock, with
    one GpuArbiter (a FakeAdmin behind it) that every context shares, as Task 25 wires them.
    Each context is h.ctx's, its requests held until `gate` is set, and is kept in `ctxs`."""
    admin, ctxs = FakeAdmin([h.EMB]), []
    arbiter = GpuArbiter(admin, h.settings)

    def make(night, window):
        ctx = h.ctx(night=night, window=window, gate=gate)
        ctx.arbiter = arbiter
        ctxs.append(ctx)
        return ctx
    sched = NightScheduler(h.store, h.settings, arbiter, NightRunner(stages, make).run,
                           clock=h.clock)
    return sched, ctxs, admin


def seed_reflecting(h, n: int, *, new: int = 3) -> list[int]:
    """n more members at level 80 (guids 1001 on), each with `new` memories created tonight."""
    guids = list(range(1001, 1001 + n))
    names = {g: "Mem" + chr(97 + i // 26) + chr(97 + i % 26) for i, g in enumerate(guids)}
    h.store.upsert_cast([Member(guid=g, name=names[g], race=1, class_=1, gender=0, level=80,
                                rank=3, online=False, map=0, zone=0, spec_tab=-1) for g in guids],
                        h.T)
    with h.store.transaction():
        for g in guids:
            save_persona(h.store, make_persona(g, names[g]), source="seed", ts=h.T,
                         race=RACES[1], class_name=CLASSES[1])
            for i in range(new):
                h.mem(g, f"Cleared a heroic with the guild, take {i}.")
    return guids


def stage_row(h, night: str, stage: str):
    return h.store.conn.execute("SELECT * FROM night_stage WHERE night = ? AND stage = ?",
                                (night, stage)).fetchone()


def pin_task5_counters(monkeypatch) -> None:
    """budget.COUNTERS with Task 5's own counters only. Later stage modules register theirs at
    import (B7), which pytest does at collection, before any test runs, so a plan test that pins
    numbers must not see them (N3: Task 5's tests pass with Tasks 1-5 alone). A test's own
    setitem stubs then land in the pinned dict, and monkeypatch restores the real one after."""
    monkeypatch.setattr(budget, "COUNTERS", {s: budget.COUNTERS[s] for s in TASK5_COUNTERS})


@pytest.fixture
def task5_counters(monkeypatch):
    pin_task5_counters(monkeypatch)


# -- the plan's tests ---------------------------------------------------------------------------

def test_resume_skips_done_units(h):
    ctx, seen, release = h.ctx(), [], asyncio.Event()
    ctx.settings = replace(ctx.settings, parallel=1)
    async def fn(c):
        async def one(u):
            seen.append(u)
            if u == "2" and not release.is_set(): await asyncio.Event().wait()
            c.commit("s", u, lambda s: None)
        await c.map_units("s", ["1", "2", "3"], one)
    runner = NightRunner([Stage("s", fn, None)], lambda n, w: ctx)
    async def go():
        task = asyncio.create_task(runner.run(ctx.night, ctx.window))
        await until(lambda: "2" in seen)                 # bounded: a regression fails, never spins
        for _ in range(10): await asyncio.sleep(0)       # room for a unit parallel=1 must hold back
        assert seen == ["1", "2"]                        # "3" waits for "2"
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
        release.set(); await runner.run(ctx.night, ctx.window)
    run(asyncio.wait_for(go(), 10))
    assert seen == ["1", "2", "2", "3"]


def test_models_switched_per_stage(h):
    admin = FakeAdmin([h.LIVE, h.EMB, h.REFL]); ctx = h.ctx(admin=admin)
    async def nothing(c): pass
    run(NightRunner([Stage("a", nothing, "live"), Stage("b", nothing, "reflection")], lambda n, w: ctx).run(ctx.night, ctx.window))
    assert admin.calls == [("unload", h.REFL), ("unload", h.LIVE)]


def test_foreign_models_counted_not_unloaded(h):
    admin = FakeAdmin([h.REFL, h.EMB, "qwen3:8b"]); ctx = h.ctx(admin=admin)
    run_begin(h.store, ctx.night, *ctx.window, now=ctx.window[1])
    async def nothing(c): pass
    run(NightRunner([Stage("a", nothing, "reflection"), Stage("b", nothing, None)], lambda n, w: ctx).run(ctx.night, ctx.window))
    assert run_get(h.store, ctx.night).foreign_loads == 2 and ("unload", "qwen3:8b") not in admin.calls


def test_the_live_model_reloaded_mid_stage_is_counted_and_left_alone(h):
    # Review Focus 2: mod-ollama-chat still asks the live model about twice a minute, so during a
    # reflection stage the foreign load is usually the live model itself
    admin = FakeAdmin([h.REFL, h.EMB]); ctx = h.ctx(admin=admin)
    run_begin(h.store, ctx.night, *ctx.window, now=ctx.window[1])
    async def chatter(c):
        admin.loaded += [h.LIVE, "qwen3:8b"]                       # loaded by others mid-stage
    run(NightRunner([Stage("reflect", chatter, "reflection")], lambda n, w: ctx).run(ctx.night, ctx.window))
    assert run_get(h.store, ctx.night).foreign_loads == 2 and admin.calls == []


def test_snapshot_once_per_night(h):
    ctx = h.ctx(); run(stage_snapshot(ctx)); run(stage_snapshot(ctx))
    assert len(list((h.settings.data_dir / "snapshots").glob("mind-night-*.db"))) == 1     # PR4: the label "night"


def test_the_snapshot_is_labelled_night_and_holds_the_database(h):
    ctx = h.ctx()
    run(stage_snapshot(ctx))
    (path,) = (h.settings.data_dir / "snapshots").glob("mind-*.db")
    assert re.fullmatch(r"mind-night-\d{8}-\d{6}\.db", path.name)        # PR4: P1b prunes it as its own label
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("SELECT count(*) FROM persona_core").fetchone()[0] == 5
    finally:
        conn.close()
    assert ctx.done("snapshot", "db")


def test_rng_deterministic(h):
    ctx = h.ctx()
    assert ctx.rng("enc").random() == ctx.rng("enc").random() != ctx.rng("x").random()


def test_members_are_persona_cast_at_80_without_human_or_away(h):
    ctx = h.ctx()
    assert ctx.members() == [101, 102, 103, 104, 105]
    h.store.upsert_cast([Member(guid=106, name="Fenn", race=2, class_=1, gender=0, level=80, rank=3, online=False,
                                map=0, zone=0, spec_tab=-1)], h.T)                     # no persona: not a member
    with h.store.transaction(): h.store.conn.execute("UPDATE cast_member SET level = 70 WHERE guid = 105")
    with h.store.transaction(): h.store.conn.execute("INSERT INTO away(guid, reason, left_at, returns_at) VALUES (103, 'x', 0, 9e9)")
    assert h.ctx().members() == [101, 102, 104] and h.ctx().members(include_away=True) == [101, 102, 103, 104]


def test_attitudes_are_women_only(h):        # men stay friends (owner, 2026-10-30; P1b's talk/romance.py)
    for g, stance in ((101, "fond"), (102, "drawn"), (103, "lust")):
        h.persona(g, stance=stance, player_gender="female")
    h.persona(104, stance="fond")                                           # a man marked fond, as a seed may be
    ctx = h.ctx()
    assert ctx.attitudes() == {101: "fond", 102: "drawn", 103: "lust"} and 104 in ctx.men()
    assert ctx.human == 1 and ctx.name(1) == "Testy"
    assert ATTITUDE_WORDS["lust"].format(name="Coros", human="Testy") == "Coros openly wants Testy."


def test_a_bad_row_fails_only_its_unit(h):
    ctx = h.ctx()
    async def fn(c):
        async def one(u):
            if u == "3": raise KeyError("no such member")                # any exception is that unit's failure
            text = "x" * (201 if u == "2" else 20)                       # 201 characters break P1b's CHECK
            c.commit("s", u, lambda s: s.insert_memory(make_mem(owner=101, text=text)))
        await c.map_units("s", ["1", "2", "3", "4"], one)
    run_begin(h.store, ctx.night, *ctx.window, now=ctx.window[1])
    run(NightRunner([Stage("s", fn, None)], lambda n, w: ctx).run(ctx.night, ctx.window))
    assert [step_done(h.store, ctx.night, "s", u) for u in "1234"] == [True, False, False, True]
    assert ctx.failures["s"] == 2 and json.loads(run_get(h.store, ctx.night).error) == {"s": 2}


def test_plan_scales_with_the_window_and_degrades_in_order(h, task5_counters, monkeypatch, caplog):
    # Tasks 1-5 only: no later counter but Task 8's confide, stubbed here (monkeypatch undoes it; B7)
    monkeypatch.setitem(budget.COUNTERS, "confide", lambda ctx: 60)
    start, end = night_bounds(h.night, UTC)

    six = h.ctx(window=(end - 6 * 3600, end))
    plan = run(plan_night(six))
    assert plan["settings"] == {"encounters_per_night": 38} and plan["steps"] == []    # 150 x 6/24
    assert six.settings.encounters_per_night == 38 and six.plan == plan

    seed_reflecting(h, 100)
    h.clock.set(end - 8 * 3600)                                     # 22:00: 2 slots for 8 hours
    roomy = run(plan_night(h.ctx()))
    assert roomy["budget_s"] == 2 * 8 * 3600 and roomy["steps"] == [] and roomy["settings"] == {}
    # the brief's raid night: 100 reflect (15,000), 300 notes (12,000: two JSON calls each, review-task9
    # I2), 150 scenes (3,300), 60 confide (1,320), 20 WoW-vs-life lines (800) and 3 stories (390)
    assert roomy["estimate_s"] == pytest.approx(32810)

    h.clock.set(end - 3600)                                         # 05:00: the 2-hour floor
    ctx = h.ctx()
    run_begin(h.store, ctx.night, *ctx.window, now=h.clock.now())
    with caplog.at_level(logging.INFO, logger="leeroy_mind"):
        plan = run(plan_night(ctx))
    assert plan["budget_s"] == 2 * 2 * 3600
    assert plan["steps"] == [step.name for step in DEGRADE] == [
        "encounters", "notes", "wow_lines", "extras", "critic", "questions"]
    assert plan["settings"] == {
        "encounters_per_night": 20, "notes_per_night": 100, "wow_lines_per_night": 10,
        "stories_per_night": 1, "lexicon_candidates": 0, "profile_per_night": 0, "critic_lines": 0,
        "questions_min_new": 6}
    assert {k: getattr(ctx.settings, k) for k in plan["settings"]} == plan["settings"]
    # every floor reached and still over, so the night runs long: 100 small reflections (13,000),
    # 100 notes, 20 scenes, 5 lines, 1 story and confide
    assert plan["estimate_s"] == pytest.approx(13000 + 4000 + 440 + 200 + 130 + 1320)
    assert run_get(h.store, ctx.night).plan == plan and "nightly_budget" in caplog.text

    # a resumed run reuses the stored plan, though 8 hours would now need no step
    h.clock.set(end - 8 * 3600)
    seen = []
    async def look(c): seen.append(c.settings)
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="leeroy_mind"):
        run(NightRunner([Stage("look", look, None)],
                        lambda n, w: h.ctx(night=n, window=w)).run(ctx.night, ctx.window))
    assert {k: getattr(seen[0], k) for k in plan["settings"]} == plan["settings"]
    assert run_get(h.store, ctx.night).plan == plan and "nightly_budget" not in caplog.text


def test_stage_durations_recorded(h):
    ctx = h.ctx()
    ctx.reasoner.llm = TimedLLM(1.5, models=frozenset({h.REFL, h.LIVE, h.EMB}))
    async def ask_twice(c):
        await c.reasoner.think("system", "first"); h.clock.advance(30)
        await c.reasoner.think("system", "second"); h.clock.advance(30)
    async def nothing(c): pass
    run(NightRunner([Stage("ask", ask_twice, "reflection"), Stage("quiet", nothing, None)],
                    lambda n, w: ctx).run(ctx.night, ctx.window))
    ask, quiet = stage_row(h, ctx.night, "ask"), stage_row(h, ctx.night, "quiet")
    assert (ask["calls"], ask["call_s"], ask["finished"] - ask["started"]) == (2, 3.0, 60)
    assert quiet["calls"] == 0 and quiet["started"] <= quiet["finished"]

    assert measured_call_s(h.store, "ask") is None                      # one night is not enough
    stage_record(h.store, "2026-10-27", "ask", started=0, finished=1, calls=4, call_s=8.0)    # 2 s a call
    stage_record(h.store, "2026-10-26", "ask", started=0, finished=1, calls=0, call_s=0.0)    # no calls
    assert measured_call_s(h.store, "ask") is None                      # two nights that made calls
    stage_record(h.store, "2026-10-25", "ask", started=0, finished=1, calls=1, call_s=4.0)
    assert measured_call_s(h.store, "ask") == 2.0                        # the median of 1.5, 2 and 4
    for night in ("2026-10-21", "2026-10-22", "2026-10-23", "2026-10-24"):
        stage_record(h.store, night, "ask", started=0, finished=1, calls=2, call_s=18.0)     # 9 s a call
    for night in ("2026-10-18", "2026-10-19", "2026-10-20"):
        stage_record(h.store, night, "ask", started=0, finished=1, calls=10, call_s=1.0)     # older
    assert measured_call_s(h.store, "ask") == 9.0                        # the last 7 nights only


# -- the runner -----------------------------------------------------------------------------------

def test_preflight_comes_first_when_a_stage_uses_the_reflection_model(h):
    admin = FakeAdmin([h.LIVE, h.EMB]); ctx = h.ctx(admin=admin)
    ctx.reasoner.llm.set_models({h.LIVE, h.EMB})                         # the 27B model is not pulled
    ran = []
    async def stage(c): ran.append(c.night)
    run_begin(h.store, ctx.night, *ctx.window, now=h.T)
    with pytest.raises(ReflectionError) as e:
        run(NightRunner([Stage("a", stage, None), Stage("b", stage, "reflection")],
                        lambda n, w: ctx).run(ctx.night, ctx.window))
    assert e.value.fatal and ran == [] and admin.calls == [] and run_get(h.store, ctx.night).plan == {}
    run(NightRunner([Stage("a", stage, None)], lambda n, w: ctx).run(ctx.night, ctx.window))
    assert ran == [ctx.night]                                            # no reflection stage, no preflight


def test_the_model_is_switched_only_when_the_next_stage_needs_another(h):
    admin = FakeAdmin([h.LIVE, h.EMB]); ctx = h.ctx(admin=admin)
    asked, ensure = [], ctx.arbiter.ensure_model
    async def spy(model):
        asked.append(model)
        await ensure(model)
    ctx.arbiter.ensure_model = spy
    async def nothing(c): pass
    stages = [Stage("a", nothing, None), Stage("b", nothing, "reflection"), Stage("c", nothing, None),
              Stage("d", nothing, "reflection"), Stage("e", nothing, "live")]
    run(NightRunner(stages, lambda n, w: ctx).run(ctx.night, ctx.window))
    assert asked == [h.REFL, h.LIVE] and admin.calls == [("unload", h.LIVE)]
    with pytest.raises(ValueError, match="model"):
        NightRunner([Stage("a", nothing, "embedding")], lambda n, w: ctx)
    with pytest.raises(ValueError, match="twice"):
        NightRunner([Stage("a", nothing, None), Stage("a", nothing, "live")], lambda n, w: ctx)


def test_a_fatal_reflection_error_ends_the_night(h):
    ctx = h.ctx()
    async def one(u):
        if u == "1":
            raise ReflectionError("the reflection model 'x' is not pulled", fatal=True)
        await asyncio.Event().wait()                                     # cancelled when 1 fails
    async def stage(c): await c.map_units("s", ["1", "2"], one)
    runner = NightRunner([Stage("s", stage, None)], lambda n, w: ctx)
    with pytest.raises(ReflectionError) as e:                            # itself, not an ExceptionGroup
        run(asyncio.wait_for(runner.run(ctx.night, ctx.window), 5))       # bounded: a regression fails
    assert e.value.fatal and not ctx.failures


def test_commit_rolls_back_a_bad_row_and_says_so(h):
    ctx = h.ctx()
    assert ctx.commit("s", "1", lambda s: s.insert_memory(make_mem(owner=101, text="x" * 201))) is False
    assert ctx.failures["s"] == 1 and not ctx.done("s", "1") and h.count("memory") == 0
    assert ctx.commit("s", "1", lambda s: None) is True and ctx.done("s", "1")
    assert ctx.skip("s", "2") is True and ctx.done("s", "2") and ctx.failures["s"] == 1


def test_a_put_inside_a_unit_is_scoped_and_rolls_back_with_it(h):
    # every stage from Task 7 on writes through ctx.writer.put inside its unit's commit (B2)
    ctx = h.ctx()
    line = h.say(h.T, 1, "got the job at the hospital", channel="whisper", to=101)
    row = make_mem(owner=101, kind="reflection", text="Testy got the hospital job.", actors=(1,),
                   source="reflection", evidence=(f"chat:{line}",), t_event=h.T, t_created=h.T,
                   t_accessed=h.T)
    def boom(s):
        ctx.writer.put(row)
        raise ValueError("a later row in the unit failed")
    assert ctx.commit("reflect", "101", boom) is False
    assert h.count("memory") == 0 and not ctx.done("reflect", "101")      # rolled back with the unit
    assert ctx.commit("reflect", "101", lambda s: ctx.writer.put(row)) is True
    assert [tuple(r) for r in h.store.conn.execute("SELECT scope, scope_with FROM memory")] == [("private", 1)]


def test_every_run_that_returns_rewrites_the_failure_summary(h):
    ctx = h.ctx()
    run_begin(h.store, ctx.night, *ctx.window, now=h.T)
    run_finish(h.store, ctx.night, now=h.T, status="failed", error="RuntimeError('Ollama went away')")
    run_begin(h.store, ctx.night, *ctx.window, now=h.T + 3700)          # the retry after the back-off
    async def fine(c): c.skip("s", "1")
    run(NightRunner([Stage("s", fine, None)], lambda n, w: h.ctx(night=n, window=w)).run(ctx.night, ctx.window))
    assert run_get(h.store, ctx.night).error is None                    # no unit failed: no stale repr(e)


def test_an_interrupted_run_resumes_with_its_stored_plan(h):
    ctx = h.ctx()
    run_begin(h.store, ctx.night, *ctx.window, now=h.T)
    planned = {"settings": {"encounters_per_night": 20, "retired_knob": 3}, "estimate_s": 1.0,
               "budget_s": 2.0, "steps": ["encounters"]}               # a knob a later version dropped
    set_run_plan(h.store, ctx.night, planned)
    run_finish(h.store, ctx.night, now=h.T, status="interrupted")      # its cleanup raised (Task 4)
    run_begin(h.store, ctx.night, *ctx.window, now=h.T + 600, count_attempt=False)
    seen = []
    async def look(c): seen.append((c.settings.encounters_per_night, c.plan))
    run(NightRunner([Stage("s", look, None)], lambda n, w: h.ctx(night=n, window=w)).run(ctx.night, ctx.window))
    assert seen == [(20, planned)] and run_get(h.store, ctx.night).plan == planned


def test_a_resumed_stage_adds_its_new_calls_to_the_first_pass(h):
    ctx, second = h.ctx(), asyncio.Event()
    ctx.reasoner.llm = TimedLLM(2.0, models=frozenset({h.REFL, h.LIVE, h.EMB}))
    async def ask(c):
        async def one(u):
            await c.reasoner.think("system", u)
            if u == "b" and not second.is_set():
                raise RuntimeError("the model went quiet")             # b fails on the first pass
            c.skip("ask", u)
        await c.map_units("ask", ["a", "b"], one)
    async def quiet(c): pass
    async def held(c):
        if not second.is_set():
            await asyncio.Event().wait()
    runner = NightRunner([Stage("ask", ask, "reflection"), Stage("quiet", quiet, None),
                          Stage("held", held, None)], lambda n, w: ctx)
    async def go():
        task = asyncio.create_task(runner.run(ctx.night, ctx.window))
        await until(lambda: stage_row(h, ctx.night, "quiet") is not None)
        task.cancel()                                                    # his login, mid-night
        with pytest.raises(asyncio.CancelledError):
            await task
        h.clock.advance(3600)
        second.set()
        await runner.run(ctx.night, ctx.window)                         # b is retried, a is not
    run(go())
    ask_row, quiet_row = stage_row(h, ctx.night, "ask"), stage_row(h, ctx.night, "quiet")
    assert (ask_row["calls"], ask_row["call_s"]) == (3, 6.0)
    assert (ask_row["started"], ask_row["finished"]) == (h.T, h.T + 3600)
    assert (quiet_row["calls"], quiet_row["finished"]) == (0, h.T)      # a pass with nothing to do changes nothing
    assert stage_row(h, ctx.night, "held") is not None


# -- the context ----------------------------------------------------------------------------------

def test_map_units_runs_at_most_parallel_and_keeps_failures_to_their_unit(h, caplog):
    ctx = h.ctx()                                                         # settings.parallel 2
    running, peak, called = [0], [0], []
    async def one(u):
        called.append(u)
        running[0] += 1
        peak[0] = max(peak[0], running[0])
        try:
            for _ in range(3):
                await asyncio.sleep(0)
            if u == "2":
                raise ReflectionError("reflection request 'notes' failed: bad JSON twice", fatal=False)
            if u == "3":
                await stray()                                            # a cancel nobody asked for
            if u == "4":
                raise ValueError("no such member")
            ctx.skip("s", u)
        finally:
            running[0] -= 1
    ctx.skip("s", "6")                                                    # done on an earlier pass
    with caplog.at_level(logging.WARNING, logger="leeroy_mind"):
        run(ctx.map_units("s", ["1", "2", "3", "4", "5", "6"], one))
    assert peak[0] == 2 and called == ["1", "2", "3", "4", "5"] and ctx.failures["s"] == 3
    assert [step_done(h.store, ctx.night, "s", u) for u in "123456"] == [True, False, False, False, True, True]
    assert caplog.text.count("nightly_unit_failed") == 3 and "CancelledError" in caplog.text


def test_a_failed_unit_is_logged_with_the_real_name_redacted(h, caplog):
    ctx = h.ctx()
    with caplog.at_level(logging.WARNING, logger="leeroy_mind"):
        ctx.fail("reflect", "101", ValueError("QUILLON told Arkon about Quillon's new job"))
    assert "nightly_unit_failed" in caplog.text and "ValueError" in caplog.text
    assert "Quillon" not in caplog.text and "QUILLON" not in caplog.text
    assert "[name] told Arkon about [name]'s new job" in caplog.text and ctx.failures["reflect"] == 1


def test_card_name_and_persona_are_what_live_talk_uses(h):
    ctx = h.ctx()
    # PR28: the card live talk renders (Pipeline._card), race and class from the roster
    assert ctx.card(101) == render_prompt_card(ctx.persona(101), race=RACES[2], class_name=CLASSES[1])
    assert ctx.name(101) == "Arkon" and ctx.tz == UTC                    # the mind clock's zone
    with h.store.transaction():
        h.store.conn.execute("DELETE FROM cast_member WHERE guid = 102")
    assert h.ctx().card(102) == render_prompt_card(ctx.persona(102), race=RACES[4], class_name=CLASSES[11])
    with pytest.raises(KeyError):
        ctx.persona(106)                                                 # nobody stored
    with pytest.raises(KeyError):
        h.ctx().name(102)                                                # not in the roster any more


def test_attitudes_fresh_rereads_a_moved_band(h):
    h.woman(101, "fond")
    ctx = h.ctx()
    assert ctx.attitudes() == {101: "fond"} and ctx.women() == {101}
    h.woman(101, "drawn")                                                 # attraction moved her band
    ctx.personas.invalidate()                                             # the app's shared cache, as at a session
    assert ctx.attitudes() == {101: "fond"}                               # the night's own, kept for the night
    assert ctx.attitudes(fresh=True) == {101: "drawn"} and ctx.persona(101).romance.stance == "drawn"
    assert ATTITUDES == ("fond", "drawn", "lust")
    assert ATTITUDE_WORDS == {"fond": "{name} is warm toward {human}.",
                              "drawn": "{name} is clearly into {human}.",
                              "lust": "{name} openly wants {human}."}


def test_embed_goes_through_the_embedder_in_the_background_class(h):
    ctx, asked = h.ctx(), []
    embed = ctx.embedder.embed
    async def spy(texts, priority):
        asked.append(priority)
        return await embed(texts, priority)
    ctx.embedder.embed = spy
    (vec,) = run(ctx.embed(["Arkon tanked the gunship."]))
    assert isinstance(vec, bytes) and ctx.reasoner.llm.calls[-1]["model"] == h.EMB
    assert asked == [Priority.BACKGROUND]


def test_rng_is_seeded_by_the_night_and_the_parts(h):
    assert h.ctx().rng("enc", 101).random() == random.Random("2026-10-29|enc|101").random()
    assert h.ctx(night="2026-10-30").rng("enc", 101).random() != h.ctx().rng("enc", 101).random()


def test_the_human_is_never_a_member_even_with_a_stored_persona(h):
    save_persona(h.store, make_persona(1, "Testmage"), source="seed", ts=h.T, race=RACES[10],
                 class_name=CLASSES[8])
    assert h.ctx().members() == [101, 102, 103, 104, 105]


def test_a_break_that_starts_tonight_takes_her_out_of_the_cast_at_once(h):
    ctx = h.ctx()
    assert 103 in ctx.members()                                         # read before the breaks stage
    with h.store.transaction():
        h.store.conn.execute("INSERT INTO away(guid, reason, left_at, returns_at) VALUES (103, 'x', 0, 9e9)")
    assert 103 not in ctx.members() and 103 in ctx.members(include_away=True)   # so no scene tonight


def test_a_woman_on_a_break_keeps_her_attitude(h):
    h.woman(102, "lust")
    with h.store.transaction():
        h.store.conn.execute("INSERT INTO away(guid, reason, left_at, returns_at) VALUES (102, 'x', 0, 9e9)")
    ctx = h.ctx()
    assert 102 not in ctx.members() and ctx.attitudes() == {102: "lust"} and 102 in ctx.women()


# -- the budget -----------------------------------------------------------------------------------

def test_planning_costs_and_the_budget(h):
    assert PLAN_S == {"think": 90.0, "json": 20.0, "live": 6.0}
    assert estimate({s: 1 for s in UNIT_CALLS}, measured={}) == pytest.approx({
        "reflect": 150, "reflect_small": 130, "notes": 40, "encounters": 22, "confide": 22,
        "life": 40, "wowlife": 40, "follow": 22, "lexicon": 22, "profile": 22, "stories": 130,
        "recap": 130, "chronicle": 40, "breaks": 20, "critic": 22, "enrich": 110, "eval": 2386,
        "supersede": 40,                                                # a reflect unit's 2 decide calls (Task 10, K2)
        "core_judge": 10})                                              # half a core-judge call (Task 11, I1)
    assert estimate({"reflect": 2, "notes": 0}, measured={"reflect": 10.0}) == {"reflect": 80.0, "notes": 0.0}
    assert night_budget_s(0, 8 * 3600, 2, 2.0) == 57600
    assert night_budget_s(0, 3600, 2, 2.0) == night_budget_s(7200, 3600, 2, 2.0) == 14400   # the floor
    with pytest.raises(ValueError, match="no_such_stage"):
        register_counter("no_such_stage", lambda ctx: 1)


def test_the_counters_task_5_registers(h):
    for g, n in ((101, 7), (102, 4), (103, 2), (104, 6), (105, 3), (106, 9)):   # 106: no persona
        for i in range(n):
            h.mem(g, f"Something new happened tonight, take {i}.")
    h.mem(103, "An old line, archived.", status="archived")             # only active memories count
    for i in range(5):
        h.mem(103, f"Last week's raid, take {i}.", t=h.T - 7 * 86400)    # before the window
    with h.store.transaction():
        h.store.conn.execute("INSERT INTO away(guid, reason, left_at, returns_at) VALUES (105, 'x', 0, 9e9)")
    ctx = h.ctx()
    count = {s: budget.COUNTERS[s] for s in TASK5_COUNTERS}
    assert {s: fn(ctx) for s, fn in count.items()} == {
        "reflect": 4, "reflect_small": 0, "encounters": 150, "notes": 12, "wowlife": 20,
        "stories": 3, "recap": 0, "chronicle": 0, "eval": 0}            # 105 is away and still reflects
    ctx.settings = replace(ctx.settings, questions_min_new=6)
    assert (count["reflect"](ctx), count["reflect_small"](ctx)) == (2, 2)   # 101 (7) and 104 (6) ask
    assert count["notes"](ctx) == 12                                      # 3 for each who reflects
    ctx.settings = replace(ctx.settings, notes_per_night=5, wow_vs_life=3)
    assert (count["notes"](ctx), count["wowlife"](ctx)) == (5, 12)          # round(40 x 3/10)
    ctx.settings = replace(ctx.settings, wow_vs_life=0)
    assert count["wowlife"](ctx) == 0
    h.boss(h.T, 574, 0, "done", [101])                                     # a 5-man boss
    h.boss(h.T - 3 * 86400, 631, 0, "done", [101])                         # a raid fight, days ago
    assert count["recap"](h.ctx()) == 0
    h.boss(h.T, 631, 0, "wipe", [101, 102])                                # tonight's raid
    assert count["recap"](h.ctx()) == 1
    sunday = h.ctx(night="2026-11-01")
    assert (count["chronicle"](sunday), count["eval"](sunday)) == (1, 1)


def test_the_plan_prices_a_stage_at_its_measured_cost_after_three_nights(h, task5_counters):
    for g in TEST_CAST:
        for i in range(3):
            h.mem(g, f"Tonight's raid, take {i}.")
    for night in ("2026-10-25", "2026-10-26", "2026-10-27"):
        stage_record(h.store, night, "reflect", started=0, finished=1, calls=10, call_s=50.0)  # 5 s a call
    rest = 15 * 40 + 150 * 22 + 20 * 40 + 3 * 130                       # notes, scenes, lines, stories
    assert run(plan_night(h.ctx()))["estimate_s"] == pytest.approx(5 * 4 * 5 + rest)     # 4 calls each
    ctx = h.ctx()
    ctx.settings = replace(ctx.settings, questions_min_new=6)
    assert run(plan_night(ctx))["estimate_s"] == pytest.approx(5 * 3 * 5 + rest)    # reflect_small: the reflect stage's cost


def test_a_step_cuts_only_what_the_budget_needs(h, task5_counters, monkeypatch):
    end = night_bounds(h.night, UTC)[1]
    h.clock.set(end - 3600)                                             # the floor: 14,400 slot-seconds
    monkeypatch.setitem(budget.COUNTERS, "confide", lambda ctx: 500)    # 11,000 s, plus 1,190 s of lines and stories
    plan = run(plan_night(h.ctx()))
    assert plan["steps"] == ["encounters"] and plan["settings"] == {"encounters_per_night": 100}  # 101 is 12 s over
    monkeypatch.setitem(budget.COUNTERS, "confide", lambda ctx: 592)    # 13,024 s
    plan = run(plan_night(h.ctx()))
    assert plan["steps"] == ["encounters", "notes", "wow_lines"]         # notes cannot help, and still go to 100
    assert plan["settings"] == {"encounters_per_night": 20, "notes_per_night": 100,
                                "wow_lines_per_night": 20}               # halved once: 400 s of lines fit
    monkeypatch.setitem(budget.COUNTERS, "confide", lambda ctx: 610)
    plan = run(plan_night(h.ctx(window=(end - 2 * 3600, end))))         # 12 scenes, already below the floor
    assert plan["steps"] == ["notes", "wow_lines"] and plan["settings"] == {
        "encounters_per_night": 12, "notes_per_night": 100, "wow_lines_per_night": 10}


def test_a_counter_that_fails_counts_zero_and_is_logged_once(h, task5_counters, monkeypatch, caplog):
    def broken(ctx):
        raise sqlite3.OperationalError("no such table: life_thread")
    monkeypatch.setitem(budget.COUNTERS, "life", broken)
    h.clock.set(night_bounds(h.night, UTC)[1] - 3600)                   # a tight budget: every step evaluates
    seed_reflecting(h, 100)
    with caplog.at_level(logging.WARNING, logger="leeroy_mind"):
        plan = run(plan_night(h.ctx()))
    assert plan["steps"] and caplog.text.count("nightly_counter_failed") == 1


def test_a_failing_counter_is_logged_with_the_real_name_redacted(h, task5_counters, monkeypatch,
                                                                 caplog):
    def broken(ctx):
        raise ValueError("bad boss payload from Quillon's raid")
    monkeypatch.setitem(budget.COUNTERS, "recap", broken)
    with caplog.at_level(logging.WARNING, logger="leeroy_mind"):
        run(plan_night(h.ctx()))
    assert "nightly_counter_failed" in caplog.text and "[name]'s raid" in caplog.text
    assert "Quillon" not in caplog.text


def test_a_later_stage_counter_cannot_move_task_5s_pinned_plans(h, monkeypatch):
    # what a later stage module does at its import (B7; Task 14's breaks, say), before any test runs
    monkeypatch.setitem(budget.COUNTERS, "breaks", lambda ctx: 1000)
    tonight = 150 * 22 + 20 * 40 + 3 * 130                              # scenes, lines and stories
    assert run(plan_night(h.ctx()))["estimate_s"] == pytest.approx(tonight + 1000 * 20)   # it bites
    pin_task5_counters(monkeypatch)                                      # what task5_counters does
    assert run(plan_night(h.ctx()))["estimate_s"] == pytest.approx(tonight)
    assert sorted(budget.COUNTERS) == sorted(TASK5_COUNTERS)


# -- the crash-loop guard ---------------------------------------------------------------------------

def test_resumes_count_begins_without_an_attempt_and_a_unit_commit_clears_them(h):
    n = h.night
    run_begin(h.store, n, 0.0, 10.0, now=10.0)
    for _ in range(2):
        run_begin(h.store, n, 0.0, 10.0, now=20.0, count_attempt=False)           # two resumes
    assert (run_get(h.store, n).resumes, run_get(h.store, n).attempts) == (2, 1)
    def boom(store):
        raise RuntimeError("the unit failed")
    with pytest.raises(RuntimeError):
        commit_unit(h.store, n, "s", "1", boom, now=30.0)
    assert run_get(h.store, n).resumes == 2                                       # rolled back with the unit
    refund_resume(h.store, n)
    assert run_get(h.store, n).resumes == 1                                       # a requested stop: one back
    commit_unit(h.store, n, "s", "1", lambda s: None, now=30.0)
    assert run_get(h.store, n).resumes == 0                                       # progress: counted afresh
    refund_resume(h.store, n)
    assert run_get(h.store, n).resumes == 0                                       # never below 0
    run_begin(h.store, n, 0.0, 10.0, now=40.0, count_attempt=False)
    run_finish(h.store, n, now=50.0, status="failed", error="RuntimeError('x')")
    run_begin(h.store, n, 0.0, 10.0, now=60.0)                                    # the retry: an attempt
    assert (run_get(h.store, n).resumes, run_get(h.store, n).attempts) == (0, 2)


def test_a_night_that_stalls_through_three_resumes_is_marked_failed(h, caplog):
    async def go():
        sched, _, _ = night_scheduler(h, [Stage("s", stray, None)])
        await sched.on_event(h.hello(0))
        h.clock.advance(11 * 60)
        for n in range(RESUMES_MAX + 1):              # the first run, then 3 resumes: each one stalls
            await sched.tick(); await sched.wait_idle()
            r = run_get(h.store, h.night)
            assert (r.status, r.resumes, r.attempts) == ("running", n, 1)
        await sched.tick(); await sched.wait_idle()    # the 4th resume: marked failed instead
        r = run_get(h.store, h.night)
        assert (r.status, r.attempts) == ("failed", 1) and "NightStalled" in r.error
        h.clock.advance(61 * 60)                       # the retry after the back-off starts afresh
        await sched.tick(); await sched.wait_idle()
        r = run_get(h.store, h.night)
        assert (r.status, r.resumes, r.attempts) == ("running", 0, 2)
    with caplog.at_level(logging.WARNING, logger="leeroy_mind"):
        run(go())
    assert f"nightly_stalled: night={h.night}" in caplog.text


def test_a_committed_unit_restarts_the_count(h):
    async def creep(c):
        unit = next(str(i) for i in range(100) if not c.done("s", str(i)))
        c.skip("s", unit)                              # one unit of progress
        await stray()                                  # then a cancel nobody asked for
    async def go():
        sched, _, _ = night_scheduler(h, [Stage("s", creep, None)])
        await sched.on_event(h.hello(0))
        h.clock.advance(11 * 60)
        for _ in range(2 * RESUMES_MAX):
            await sched.tick(); await sched.wait_idle()
        r = run_get(h.store, h.night)
        assert (r.status, r.resumes) == ("running", 0)
        assert [step_done(h.store, h.night, "s", str(i)) for i in range(7)] == [True] * 6 + [False]
    run(go())


def test_his_logins_never_count_as_stalls(h):
    async def go():
        gate = asyncio.Event()
        async def think(c):
            async def one(u):
                await c.reasoner.think("system", u)
                c.skip("think", u)
            await c.map_units("think", ["101"], one)
        sched, ctxs, admin = night_scheduler(h, [Stage("think", think, "reflection")], gate=gate)
        await sched.on_event(h.hello(0))
        for i in range(RESUMES_MAX + 2):
            h.clock.advance(11 * 60)
            await sched.tick()
            await until(lambda: len(ctxs) == i + 1 and ctxs[i].reasoner.llm.inflight == 1)
            await sched.on_event(h.login())               # he is back, mid-request
            await asyncio.wait_for(sched.handback(), 2)   # at once: the run's cancel path waits on no model
            await sched.on_event(h.logout())
            r = run_get(h.store, h.night)
            assert (r.status, r.resumes, r.attempts) == ("running", 0, 1)
        assert admin.calls[-1] == ("preload", h.LIVE, -1)
        h.clock.advance(11 * 60)
        gate.set()
        await sched.tick(); await sched.wait_idle()
        r = run_get(h.store, h.night)
        assert (r.status, r.attempts) == ("done", 1) and step_done(h.store, h.night, "think", "101")
    run(go())


def test_stopping_the_mind_mid_unit_winds_the_run_down_within_the_stop_budget(h):
    async def go():
        gate = asyncio.Event()                         # never set: the 27B model never answers
        async def think(c):
            async def one(u):
                await c.reasoner.think("system", u)
                c.commit("think", u, lambda s: s.insert_memory(make_mem(owner=int(u), text="Never stored.")))
            await c.map_units("think", ["101", "102"], one)
        sched, ctxs, _ = night_scheduler(h, [Stage("think", think, "reflection")], gate=gate)
        await sched.on_event(h.hello(0))
        h.clock.advance(11 * 60)
        loop = asyncio.create_task(sched.run_forever())
        await until(lambda: ctxs and ctxs[0].reasoner.llm.inflight == 2)   # both requests are out
        began = time.monotonic()
        loop.cancel()                                  # P1b's stop(): cancel, then wait CANCEL_WAIT_S
        done, _ = await asyncio.wait({loop}, timeout=CANCEL_WAIT_S)
        assert loop in done and time.monotonic() - began < CANCEL_WAIT_S
        r = run_get(h.store, h.night)
        assert (r.status, r.resumes) == ("running", 0) and h.count("memory") == 0
        assert not step_done(h.store, h.night, "think", "101")
    run(go())
