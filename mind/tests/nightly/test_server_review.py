from dataclasses import replace
from datetime import datetime, time, timezone
from unittest.mock import AsyncMock

import pytest

from conftest import run
from leeroy_mind.nightly.budget import plan_night
from leeroy_mind.nightly.runner import NightRunner, Stage
from leeroy_mind.nightly.scheduler import tonight
from leeroy_mind.nightly.store import run_begin
from leeroy_mind.nightly.wowlife import _step_state

QUIET = (time(3), time(8))


def at(h, value):
    h.clock.set(datetime.fromisoformat(value).replace(tzinfo=timezone.utc).timestamp())


def test_three_am_run_counts_the_preceding_evening_and_keeps_one_date(h):
    h.settings = replace(h.settings, quiet=QUIET)
    at(h, '2026-11-11 20:00')
    start = h.clock.now()
    at(h, '2026-11-12 01:00')
    h.session(start, h.clock.now())
    at(h, '2026-11-12 03:15')
    night = tonight(h.clock.local(), h.settings)
    ctx = h.ctx(night=night)
    assert _step_state(ctx)
    assert h.store.conn.execute('SELECT hours FROM wow_hours WHERE night=? AND guid=?',
                                (night, h.human)).fetchone()[0] == 5
    at(h, '2026-11-12 07:59')
    assert tonight(h.clock.local(), h.settings) == night == '2026-11-11'


@pytest.mark.parametrize('late', ['2026-11-12 08:00:01', '2026-11-13 03:15:00'])
def test_preflight_cannot_move_the_admitted_budget_end(h, late):
    h.settings = replace(h.settings, quiet=QUIET)
    at(h, '2026-11-12 07:59:59')
    ctx = h.ctx(night='2026-11-11')
    run_begin(h.store, ctx.night, *ctx.window, now=h.clock.now())
    async def preflight():
        at(h, late)
    ctx.reasoner.preflight = preflight
    runner = NightRunner([Stage('test', AsyncMock(), 'reflection')], lambda *_: ctx)
    run(runner.run(ctx.night, ctx.window))
    assert ctx.plan['budget_s'] == ctx.settings.parallel * ctx.settings.budget_floor_hours * 3600


def test_direct_planning_in_overrun_uses_the_previous_end(h):
    h.settings = replace(h.settings, quiet=QUIET)
    at(h, '2026-11-12 08:00:01')
    ctx = h.ctx(night='2026-11-11')
    run_begin(h.store, ctx.night, *ctx.window, now=h.clock.now())
    plan = run(plan_night(ctx))
    assert plan['budget_s'] == ctx.settings.parallel * ctx.settings.budget_floor_hours * 3600
