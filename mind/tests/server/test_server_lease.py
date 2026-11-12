import asyncio
from dataclasses import replace
from datetime import datetime, time, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from conftest import run
from leeroy_mind.server.attach import attach_server
from leeroy_mind.server.config import ServerConfig
from leeroy_mind.server.lease import ServerLease
from nightly.conftest import FakeAdmin, StepClock, h
from nightly.test_night_scheduler import make_sched
from leeroy_mind.nightly.store import run_get


class Arbiter:
    def __init__(self):
        self.holders = set()
        self.hold_calls = []
        self.hand_over = AsyncMock()
        self.settled = AsyncMock()

    def hold(self, holder):
        self.hold_calls.append(holder)
        first = not self.holders
        self.holders.add(holder)
        return first

    async def release_live(self, holder):
        self.holders.discard(holder)


def fixture(value='2026-11-12 08:00', *, running=None, config=None):
    clock = StepClock(datetime.fromisoformat(value).replace(tzinfo=timezone.utc))
    app = SimpleNamespace(clock=clock, arbiter=Arbiter(),
                          night_scheduler=SimpleNamespace(running_night=lambda: running))
    return app, ServerLease(app, config or ServerConfig(enabled=True))


def set_time(app, value):
    app.clock.set(datetime.fromisoformat(value).replace(tzinfo=timezone.utc).timestamp())


def test_take_once_release_in_window_and_retake_after_restart():
    async def go():
        app, lease = fixture()
        await lease.tick(app.clock.now())
        assert lease.held and not lease.ready()
        await lease.handback()
        assert lease.ready()
        await lease.tick(app.clock.now())
        assert app.arbiter.hold_calls == ['server']
        set_time(app, '2026-11-13 03:00')
        await lease.tick(app.clock.now())
        assert not lease.held and not lease.ready() and not app.arbiter.holders
        set_time(app, '2026-11-13 08:00')
        await lease.tick(app.clock.now())
        await lease.close()
        assert not lease.held and not app.arbiter.holders
        restarted = ServerLease(app, ServerConfig(enabled=True))
        await restarted.tick(app.clock.now())
        await restarted.handback()
        assert restarted.ready()
        await restarted.close()
    run(go())


@pytest.mark.parametrize('window,end', [((time(3), time(8)), 8), ((time(23), time(2)), 2)])
def test_overrun_is_one_hour_from_previous_window_end(window, end):
    async def go():
        app, lease = fixture(f'2026-11-12 {end:02}:00', running='2026-11-12',
                              config=ServerConfig(enabled=True, quiet_window=window))
        for minute in (0, 30, 59):
            set_time(app, f'2026-11-12 {end:02}:{minute:02}')
            await lease.tick(app.clock.now())
            assert not lease.held
        set_time(app, f'2026-11-12 {end+1:02}:00')
        await lease.tick(app.clock.now())
        assert lease.held
        await lease.close()
    run(go())


@pytest.mark.parametrize('day', ['2026-03-08', '2026-11-01'])
def test_overrun_deadline_across_dst(day):
    async def go():
        app, lease = fixture(running='unfinished')
        zone = ZoneInfo('America/New_York')
        app.clock.local = lambda: datetime.fromtimestamp(app.clock.now(), zone)
        for hour, held in ((8, False), (9, True)):
            app.clock.set(datetime.fromisoformat(f'{day} {hour:02}:00').replace(tzinfo=zone).timestamp())
            await lease.tick(app.clock.now())
            assert lease.held is held
        await lease.close()
    run(go())


def test_finished_night_does_not_delay_lease():
    async def go():
        app, lease = fixture('2026-11-12 06:10')
        await lease.tick(app.clock.now())
        assert not lease.held
        set_time(app, '2026-11-12 08:00')
        await lease.tick(app.clock.now())
        assert lease.held
        await lease.close()
    run(go())


def test_close_joins_inflight_handover_and_preserves_other_holders():
    async def go():
        app, lease = fixture()
        entered = asyncio.Event()
        async def handover():
            entered.set()
            await asyncio.Event().wait()
        app.arbiter.hand_over.side_effect = handover
        await lease.tick(app.clock.now())
        await entered.wait()
        app.arbiter.hold('human')
        await lease.close()
        await lease.handback()
        assert app.arbiter.holders == {'human'} and not lease.ready()
        await lease.tick(app.clock.now())  # a closed owner cannot reacquire
        assert not lease.held
    run(go())


def test_another_holder_does_not_duplicate_handover():
    async def go():
        app, lease = fixture()
        app.arbiter.hold('human')
        await lease.tick(app.clock.now())
        await lease.handback()
        app.arbiter.hand_over.assert_not_called()
        await lease.close()
        assert app.arbiter.holders == {'human'}
    run(go())


def test_failed_handover_keeps_lease_and_logs_only_error_code(caplog):
    async def go():
        app, lease = fixture()
        app.arbiter.hand_over.side_effect = RuntimeError('private sentinel')
        await lease.tick(app.clock.now())
        await lease.handback()
        assert lease.held  # live request may load the model, night stays excluded
        assert 'private sentinel' not in caplog.text
        assert 'server_handback_failed' in caplog.text
        await lease.close()
    run(go())


def test_overrun_preempts_real_scheduler_and_resumes_same_run_next_window(h):
    async def go():
        h.clock.set(datetime(2026, 11, 12, 3, tzinfo=timezone.utc).timestamp())
        h.settings = replace(h.settings, quiet=(time(3), time(8)))
        sched, started, _ = make_sched(h, block=True)
        app = SimpleNamespace(clock=h.clock, arbiter=sched.arbiter, night_scheduler=sched)
        lease = ServerLease(app, ServerConfig(enabled=True))
        h.clock.advance(11 * 60)
        await sched.tick()
        await asyncio.sleep(0)
        for value in ('2026-11-12 08:00', '2026-11-12 08:30'):
            set_time(app, value)
            await lease.tick(h.clock.now())
            assert sched.running_night() == '2026-11-12'
        set_time(app, '2026-11-12 09:00')
        await lease.tick(h.clock.now())
        await lease.handback()
        assert sched.running_night() is None
        assert run_get(h.store, '2026-11-12').status == 'running'
        set_time(app, '2026-11-13 03:00')
        await lease.tick(h.clock.now())
        sched.block = False
        await sched.tick()
        await sched.wait_idle()
        assert len(started) == 2 and started[0] == started[1]
        assert run_get(h.store, '2026-11-12').attempts == 1
        await lease.close()
    run(go())


@pytest.mark.parametrize('text,enabled', [('', False), ('[server]\nenabled=true', True),
                                        ('[server]\nenabled=true\nreactions=1', False)])
def test_app_attach_is_last_and_starts_nothing(tmp_path, text, enabled):
    from selfies.fakes import mind_app
    app = mind_app(tmp_path, toml=text)
    try:
        assert (app.server is not None) is enabled
        assert not app._bg_tasks
        if enabled:
            assert app._background[-1] == app.server.lease.run
            assert app._shutdown_drains[-1] == app.server.lease.close
        else:
            assert attach_server(app) is None
    finally:
        run(app.stop())


def test_live_admission_waits_for_server_and_reload_keeps_window(tmp_path):
    from selfies.fakes import mind_app
    async def go():
        app = mind_app(tmp_path, toml='[server]\nenabled=true')
        entered, release = asyncio.Event(), asyncio.Event()
        async def handover():
            entered.set()
            await release.wait()
        app.arbiter.hand_over = handover
        try:
            await app.server.lease.tick(app.clock.now())
            await entered.wait()
            admission = asyncio.create_task(app.engine.live_admission())
            await asyncio.sleep(0)
            assert not admission.done()
            release.set()
            await admission
            (tmp_path / 'mind.toml').write_text('[server]\nenabled=false')
            ctx = app.night_runner.make_ctx('2026-11-12', (1, 2))
            assert ctx.settings.quiet == app.server.config.quiet_window
        finally:
            await app.stop()
    run(go())


def test_login_inside_window_only_takes_human_lease(h):
    async def go():
        h.clock.set(datetime(2026, 11, 12, 3, tzinfo=timezone.utc).timestamp())
        h.settings = replace(h.settings, quiet=(time(3), time(8)))
        sched, _, _ = make_sched(h, block=True)
        app = SimpleNamespace(clock=h.clock, arbiter=sched.arbiter, night_scheduler=sched)
        lease = ServerLease(app, ServerConfig(enabled=True))
        h.clock.advance(11 * 60)
        await lease.tick(h.clock.now())
        await sched.tick()
        await asyncio.sleep(0)
        await sched.on_event(h.login())
        await sched.handback()
        await lease.tick(h.clock.now())
        assert app.arbiter.holders == {'human'} and not lease.held
        assert sched.running_night() is None
        await sched.on_event(h.logout())
        await lease.close()
    run(go())


def test_booth_renders_only_in_quiet_window(tmp_path):
    from selfies.fakes import mind_app, ready
    from selfies.test_photo_booth import MemoryWorker, tick
    from leeroy_mind.nightly.scheduler import tonight
    from leeroy_mind.nightly.store import run_begin, run_finish
    from leeroy_mind.selfies.store import ensure_first_sets, ready_set
    async def go():
        app = mind_app(tmp_path, toml='[server]\nenabled=true\n[selfies]\nenabled=true\nbooth=true')
        app.arbiter.admin = FakeAdmin([])
        worker = MemoryWorker()
        app.selfie_booth.worker = lambda: worker
        try:
            for guid in (102, 103):
                ready(app.store, tmp_path, guid, 1, now=app.clock.now())
            ensure_first_sets(app.store, [101], now=app.clock.now())
            await app.clock.advance(3600, step=3600)
            await app.server.lease.tick(app.clock.now())
            await app.server.lease.handback()
            await tick(app)
            assert not worker.requests and not ready_set(app.store, 101, 1)
            await app.clock.advance(8 * 3600, step=8 * 3600)  # 05:00 next day
            assert app.clock.local().hour == 5
            await app.server.lease.tick(app.clock.now())
            now = app.clock.now()
            night = tonight(app.clock.local(), app.night_scheduler.settings)
            run_begin(app.store, night, now-86400, now, now=now)
            run_finish(app.store, night, now=now, status='done')
            await tick(app)
            assert worker.requests and ready_set(app.store, 101, 1)
        finally:
            await app.stop()
    run(go())
