"""Planning includes pending human-world calls and respects degraded settings."""
from dataclasses import replace

from conftest import run
from nightly.test_night_human_world import fact, reaction, live_night
from nightly.conftest import yes
from leeroy_mind.nightly import budget, human_world as hw


def test_follow_counter_is_read_only_and_respects_completed_units(h):
    line, _ = fact(h)
    h.rel(101, h.human, affinity=70, familiarity=70)
    ctx = h.ctx()
    before = list(h.store.conn.execute("SELECT * FROM meta"))
    assert budget.COUNTERS["follow"](ctx) == 1
    assert list(h.store.conn.execute("SELECT * FROM meta")) == before
    assert not ctx.done(hw.STAGE, "inputs")
    ctx.skip(hw.STAGE, f"follow:chat:{line}")
    assert budget.COUNTERS["follow"](ctx) == 0


def test_profile_budget_and_degradation_disable_model_work(h):
    live_night(h)
    for n in range(10):
        reaction(h, n)
    ctx = h.ctx(default=yes)
    assert budget.COUNTERS["profile"](ctx) == 1
    assert not ctx.done(hw.STAGE, "inputs")
    ctx.settings = replace(ctx.settings, profile_per_night=0)
    assert budget.COUNTERS["profile"](ctx) == 0
    run(hw.stage_human_world(ctx))
    assert ctx.done(hw.STAGE, "profile") and not ctx.failures
    assert h.store.conn.execute("SELECT count(*) FROM lands_profile").fetchone()[0] == 0
    assert ctx.reasoner.llm.calls == []
