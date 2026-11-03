"""Real pipeline orchestration with synthetic records and no network/model work."""
import asyncio
import json
from dataclasses import replace

import pytest

from conftest import run
from nightly.conftest import generic, no_real_name
from leeroy_mind.nightly.pipeline import STAGES, WeeklyEvaluationIncomplete, stage_eval, stage_threads
from leeroy_mind.nightly.runner import NightRunner
from leeroy_mind.nightly.store import run_begin


def test_stage_order_preserves_dependencies_and_skips_breaks():
    assert [s.name for s in STAGES] == ["snapshot", "threads", "confide", "knows", "nudges", "tier0",
        "enrich", "reflect", "life", "wowlife", "mentors", "encounters", "attraction", "romance",
        "jealousy", "human_world", "language", "mood", "notes", "stories", "critic", "forget", "regress", "eval"]
    assert next(s for s in STAGES if s.name == "eval").model is None


@pytest.mark.parametrize("status,complete", [("extracted", True), ("skipped", True),
                                            ("failed", False), ("pending", False)])
def test_thread_receipt_requires_terminal_extraction(h, status, complete):
    tid = h.store.open_thread("guild", h.T)
    h.store.close_thread(tid, h.T, "pending")
    ctx = h.ctx()
    async def extract(ident):
        h.store.set_thread_status(ident, h.T, status)
    ctx.extract_thread = extract
    run(stage_threads(ctx))
    assert ctx.done("threads", str(tid)) is complete
    assert bool(ctx.failures["threads"]) is not complete


def test_cancelled_extraction_stays_pending(h):
    tid = h.store.open_thread("guild", h.T)
    h.store.close_thread(tid, h.T, "pending")
    ctx = h.ctx()
    async def scenario():
        entered = asyncio.Event()
        async def extract(_):
            entered.set()
            await asyncio.Future()
        ctx.extract_thread = extract
        task = asyncio.create_task(stage_threads(ctx))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    run(scenario())
    assert not ctx.done("threads", str(tid)) and h.store.thread_status(tid) == "pending"


@pytest.mark.parametrize("outcome", ["complete", "partial", "raise", "cancel"])
def test_weekly_factory_ownership_and_completion(h, monkeypatch, outcome):
    import leeroy_mind.eval as evaluation
    ctx = h.ctx(night="2026-11-01")
    events = []
    class Asker:
        async def aclose(self):
            events.append("closed")
    async def factory():
        events.append("opened")
        return Asker()
    async def weekly(current, week, *, use_llm):
        assert isinstance(current.asker, Asker) and use_llm
        if outcome == "raise":
            raise ValueError("report failed")
        if outcome == "cancel":
            raise asyncio.CancelledError
        current.store.set_meta(f"nightly:eval-result:{week}", json.dumps(
            {"complete": outcome == "complete", "requested_llm": True}))
    monkeypatch.setattr(evaluation, "run_weekly", weekly)
    ctx.asker = factory
    if outcome in ("raise", "cancel", "partial"):
        expected = {"raise": ValueError, "cancel": asyncio.CancelledError,
                    "partial": WeeklyEvaluationIncomplete}[outcome]
        with pytest.raises(expected):
            run(stage_eval(ctx))
    else:
        run(stage_eval(ctx))
    assert events == ["opened", "closed"] and ctx.asker is factory
    assert ctx.done("eval", "2026-W44") is (outcome == "complete")


def test_weekly_supplied_asker_is_caller_owned_and_weekdays_skip(h, monkeypatch):
    import leeroy_mind.eval as evaluation
    ctx = h.ctx()
    class Asker:
        async def ask(self, *_):
            raise AssertionError
        async def aclose(self):
            raise AssertionError("caller-owned asker closed")
    ctx.asker = Asker()
    seen = []
    async def weekly(current, week, *, use_llm):
        seen.append(week)
        current.store.set_meta(f"nightly:eval-result:{week}", json.dumps(
            {"complete": True, "requested_llm": use_llm}))
    monkeypatch.setattr(evaluation, "run_weekly", weekly)
    run(stage_eval(ctx))
    assert seen == []
    ctx.night = "2026-11-01"
    run(stage_eval(ctx))
    run(stage_eval(ctx))
    assert seen == ["2026-W44"]


def test_full_night_with_fakes_is_idempotent(h):
    for guid in (101, 102, 103):
        h.persona(guid)
        for i in range(3):
            h.mem(guid, f"A quiet guild evening, recollection {i}.", t_created=h.T - 60 + i)
            h.say(h.T + i, guid, f"Quiet evening at the bank {i}.")
    h.session(h.T - 9 * 3600, h.T + 30)
    h.boss(h.T - 100, 533, 15956, "kill", (1, 101, 102, 103))
    h.group(h.T - 300, (1, 101, 102, 103), raid=True)
    h.clock.set(h.T + 600)
    ctx = h.ctx(default=generic, window=(h.T - 12 * 3600, h.T + 600))
    # Exercise real stages at a small integration scale; enrichment still needs owner approval.
    ctx.settings = replace(ctx.settings, encounters_per_night=2, volition_scenes=1,
                           stories_per_night=1, notes_per_night=3, wow_lines_per_night=3)
    run_begin(h.store, ctx.night, *ctx.window, now=h.clock.now())
    runner = NightRunner(STAGES, lambda *_: ctx)
    run(runner.run(ctx.night, ctx.window))
    assert not dict(ctx.failures)
    tables = ("memory", "rel_delta", "emotion", "life_thread", "life_beat", "encounter",
              "attraction_act", "persona_ledger", "fact", "story", "night_step", "critic_score")
    counts = {table: h.count(table) for table in tables}
    state = [tuple(row) for row in h.store.conn.execute("SELECT * FROM persona_state ORDER BY guid")]
    calls = ctx.reasoner.calls
    run(runner.run(ctx.night, ctx.window))
    assert not dict(ctx.failures)
    assert {table: h.count(table) for table in tables} == counts
    assert [tuple(row) for row in h.store.conn.execute("SELECT * FROM persona_state ORDER BY guid")] == state
    assert ctx.reasoner.calls == calls
    assert h.count("night_stage") == len(STAGES)
    assert no_real_name(h.store) == []
