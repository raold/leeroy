import asyncio
from dataclasses import replace
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

import pytest

from conftest import run
from leeroy_mind.config import load_config
from leeroy_mind.nightly.budget import plan_night
from leeroy_mind.nightly.scheduler import tonight, window_for
from leeroy_mind.nightly.settings import load_settings
from leeroy_mind.nightly.store import run_begin, run_finish, run_get
from nightly.test_night_scheduler import make_sched

QUIET = (time(3), time(8))


def at(h, value):
    h.clock.set(datetime.fromisoformat(value).replace(tzinfo=timezone.utc).timestamp())


def scheduler(h, value, *, quiet=QUIET, block=False):
    at(h, value)
    h.settings = replace(h.settings, quiet=quiet)
    sched, started, admin = make_sched(h, block=block)
    h.clock.advance(15 * 60)
    return sched, started, admin


@pytest.mark.parametrize('enabled', [True, False])
def test_load_settings_only_uses_enabled_server(h, tmp_path, enabled):
    path = tmp_path / 'mind.toml'
    path.write_text('[server]\nenabled=' + str(enabled).lower())
    assert load_settings(load_config(path), tmp_path).quiet == (QUIET if enabled else None)


@pytest.mark.parametrize('value,quiet,expected', [
    ('2026-11-12 06:00', None, '2026-11-12'),
    ('2026-11-12 14:00', None, '2026-11-12'),
    ('2026-11-12 09:00', QUIET, None),
    ('2026-11-12 03:05', QUIET, '2026-11-12'),
])
def test_due(h, value, quiet, expected):
    sched, _, _ = scheduler(h, value, quiet=quiet)
    assert sched.due() == expected


def test_login_pauses_logout_resumes_and_restart_does_not_repeat(h):
    async def go():
        sched, started, _ = scheduler(h, '2026-11-12 03:05', block=True)
        await sched.tick()
        await asyncio.sleep(0)
        assert sched.running_night() == '2026-11-12'
        await sched.on_event(h.login())
        await sched.handback()
        assert sched.running_night() is None
        await sched.on_event(h.logout())
        h.clock.advance(11 * 60)
        sched.block = False
        await sched.tick()
        await sched.wait_idle()
        assert len(started) == 2 and started[0] == started[1]
        assert run_get(h.store, '2026-11-12').attempts == 1
        restarted, _, _ = make_sched(h)
        h.clock.advance(11 * 60)
        assert restarted.due() is None
        await sched.arbiter.settled()
    run(go())


def test_missed_window_covers_all_events_next_time(h):
    async def go():
        at(h, '2026-11-11 03:10')
        end = h.clock.now()
        run_begin(h.store, '2026-11-11', 0, end, now=end)
        run_finish(h.store, '2026-11-11', now=end, status='done')
        sched, started, _ = scheduler(h, '2026-11-12 02:00')
        await sched.on_event(h.login())
        at(h, '2026-11-12 03:10')
        assert sched.due() is None
        at(h, '2026-11-12 08:30')
        await sched.on_event(h.logout())
        await sched.handback()
        h.clock.advance(11 * 60)
        assert sched.due() is None
        at(h, '2026-11-13 03:10')
        assert sched.due() == '2026-11-13'
        assert window_for(h.store, sched.due(), h.clock.now()) == (end, h.clock.now())
        await sched.tick()
        await sched.wait_idle()
        assert len(started) == 1
        await sched.arbiter.settled()
    run(go())


@pytest.mark.parametrize('day,prior', [('2026-11-01', '2026-10-31'), ('2026-03-08', '2026-03-07')])
def test_night_key_across_dst(h, day, prior):
    settings = replace(h.settings, quiet=QUIET)
    local = datetime.fromisoformat(day).replace(tzinfo=ZoneInfo('America/New_York'))
    assert tonight(local.replace(hour=2, minute=59), settings) == prior
    assert tonight(local.replace(hour=3), settings) == day


@pytest.mark.parametrize('value,hours', [('03:30', 4.5), ('07:30', 2)])
def test_budget_cutoff_and_floor(h, value, hours):
    at(h, '2026-11-12 ' + value)
    h.settings = replace(h.settings, quiet=QUIET)
    ctx = h.ctx(night='2026-11-12')
    run_begin(h.store, ctx.night, *ctx.window, now=h.clock.now())
    plan = run(plan_night(ctx))
    assert plan['budget_s'] == ctx.settings.parallel * hours * 3600
    assert run_get(h.store, ctx.night).plan['budget_s'] == plan['budget_s']
