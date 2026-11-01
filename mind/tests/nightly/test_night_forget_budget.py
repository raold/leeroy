"""Budget the actual weekly consolidation work, including partial cross-night retries."""
from dataclasses import replace

from conftest import run
from leeroy_mind.nightly import budget
from leeroy_mind.nightly.forget import DAY_S, stage_forget


def old(h, owner=101, **kw):
    event = h.boss(h.T - 8 * DAY_S, 631, 11, "wiped", (owner,))
    return h.mem(owner, "We gathered herbs together.", t=h.T - 8 * DAY_S,
                 importance=3, evidence=(f"event:{event}",), **kw)


def test_forget_budget_counts_exact_scope_chunks_without_writing(h):
    for _ in range(41):
        old(h)
    old(h, scope="private", scope_with=102)
    old(h, scope="private", scope_with=103)
    old(h, owner=102)
    old(h, key="diary:old")
    old(h, access_n=1)
    h.mem(101, "We gathered herbs today.", importance=3)
    before = h.store.conn.total_changes
    count = budget.COUNTERS["forget"](h.ctx())
    assert count == 5  # two public chunks, two distinct private partners, another owner
    assert h.store.conn.total_changes == before
    assert budget.estimate({"forget": count}, measured={}) == {"forget": 100.0}
    assert budget.estimate({"forget": count}, measured={"forget": 7.5}) == {"forget": 37.5}


def test_forget_budget_matches_model_calls_and_completed_week(h):
    for _ in range(41):
        old(h)
    calls = []
    def reply(prompt):
        calls.append(prompt)
        return {"text": "We gathered herbs together."}
    ctx = h.ctx(default=reply)
    assert budget.COUNTERS["forget"](ctx) == 2
    run(stage_forget(ctx))
    assert len(calls) == 2 and not ctx.failures
    assert len([m for m in h.rows(101) if (m.key or "").startswith("summary:")]) == 2
    assert sum(m.status == "archived" for m in h.rows(101)) == 41
    old(h)  # newly eligible data waits for next week's run
    assert budget.COUNTERS["forget"](h.ctx(night="2026-10-30")) == 0


def test_forget_budget_counts_only_frozen_failed_chunks_on_next_night(h):
    for _ in range(41):
        old(h)
    calls = []
    def reply(prompt):
        calls.append(prompt)
        if len(calls) == 1:
            raise RuntimeError("transient generation failure")
        return {"text": "We gathered herbs together."}
    ctx = h.ctx(default=reply)
    ctx.settings = replace(ctx.settings, parallel=1)
    run(stage_forget(ctx))
    assert ctx.failures["forget"] == 1 and len(calls) == 2
    assert sum(m.status == "archived" for m in h.rows(101)) == 1
    old(h, owner=102)  # not in the durable plan for this week
    retry = h.ctx(night="2026-10-30", default=reply)
    assert budget.COUNTERS["forget"](retry) == 1
    run(stage_forget(retry))
    assert len(calls) == 3 and not retry.failures
    assert sum(m.status == "archived" for m in h.rows(101)) == 41
    assert budget.COUNTERS["forget"](retry) == 0


def test_forget_remains_budgeted_after_optional_work_is_cut(h, monkeypatch):
    old(h)
    monkeypatch.setattr(budget, "COUNTERS", {"forget": budget.COUNTERS["forget"]})
    ctx = h.ctx()
    ctx.settings = replace(ctx.settings, budget_floor_hours=0, parallel=1)
    h.clock.set(ctx.window[1])
    plan = run(budget.plan_night(ctx))
    assert plan["budget_s"] == 0
    assert plan["units"] == {"forget": 1} and plan["estimate_s"] == 20
