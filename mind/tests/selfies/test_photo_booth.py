"""Only invented personas, fake clocks/admins and the existing no-GPU worker protocol."""
import asyncio
import json
import logging
from dataclasses import replace

import pytest

from conftest import run
from leeroy_mind.clock import night_of
from leeroy_mind.nightly.store import run_begin, run_finish
from leeroy_mind.protocol import Login
from leeroy_mind.selfies.booth import LAST, STAGE
from leeroy_mind.selfies.cli import _status
from leeroy_mind.selfies.files import set_dir
from leeroy_mind.selfies.job import pid_alive
from leeroy_mind.selfies.kind import _record
from leeroy_mind.selfies.stage import make_stage
from leeroy_mind.selfies.store import ensure_first_sets, queue_more, queued_sets, ready_set
from leeroy_mind.selfies.style import LUST
from leeroy_mind.selfies.worker import Rendered, SelfieWorker
from nightly.conftest import FakeAdmin
from .fakes import JPEG, OWNER, fake_command, fake_worker, mind_app, ready, until

ON = "[selfies]\nenabled=true\nbooth=true\n"


class MemoryWorker:
    """The protocol's result without a subprocess; tree shutdown has its own real fake below."""
    def __init__(self, *, flagged=False, on_render=None):
        self.requests, self.stops = [], 0
        self.running, self.flagged, self.on_render = False, flagged, on_render

    async def start(self):
        self.running = True

    async def stop(self):
        self.stops += 1
        self.running = False

    async def render(self, req, *, rid=None):
        self.requests.append(req)
        if self.on_render:
            await self.on_render(req)
        if not self.flagged:
            req.out.write_bytes(JPEG)
            req.thumb.write_bytes(JPEG)
        return Rendered(self.flagged, 0.9 if self.flagged else 0.01, "private prompt sentinel",
                        10, 0.1, 1, req.size, req.size)


def finished_night(app):
    now = app.clock.now()
    night = night_of(app.clock.local())
    run_begin(app.store, night, now - 86400, now, now=now)
    run_finish(app.store, night, now=now, status="done")


@pytest.fixture
def app(tmp_path):
    app = mind_app(tmp_path, toml=ON)
    app.arbiter.admin = FakeAdmin([app.cfg.models.live, app.cfg.models.embedding])
    finished_night(app)
    # Leave only member 101 needing a first set.
    for guid in (102, 103):
        ready(app.store, app.data_dir, guid, 1, now=app.clock.now())
    ensure_first_sets(app.store, [101], now=app.clock.now())
    app.selfie_booth.worker = MemoryWorker
    yield app
    app.store.close()


async def advance(app, seconds):
    await app.clock.advance(seconds, step=seconds)


async def tick(app):
    await app.selfie_booth.tick()
    if app.selfie_booth._task is not None:
        await asyncio.wait({app.selfie_booth._task})
        if not app.selfie_booth._task.cancelled():
            app.selfie_booth._task.result()


def test_more_after_night_renders_after_ten_minutes_with_separate_records(app, caplog):
    async def go():
        ready(app.store, app.data_dir, 101, 1, now=app.clock.now())
        more = queue_more(app.store, 101, now=app.clock.now())
        assert more.set_no == 2
        await advance(app, 599)
        await tick(app)
        assert not ready_set(app.store, 101, 2)
        await advance(app, 1)
        await tick(app)
        assert ready_set(app.store, 101, 2)
        assert not app.store.conn.execute("SELECT 1 FROM night_step WHERE stage='selfies'").fetchone()
        status = _status(app.store, app.clock)
        assert status["booth"]["units"] == 1
        assert status["booth"]["last"]["sets"] == 1
        assert status["booth"]["last"]["outcome"] == "done"
        assert status["booth"]["calls"] == 4
    with caplog.at_level(logging.INFO, logger="leeroy_mind"):
        run(go())
    assert "private prompt sentinel" not in caplog.text
    assert any(getattr(r, "leeroy_event", "") == "selfies_booth_done" for r in caplog.records)


def test_background_loop_picks_up_first_kind_within_hour(app):
    async def go():
        # First kind exists but no queue row yet: the loop timestamps its first observation.
        app.store.conn.execute("DELETE FROM selfie_set WHERE guid=101")
        app.store.conn.commit()
        loop = asyncio.create_task(app.selfie_booth.run_forever())
        try:
            await advance(app, 600)
            await asyncio.sleep(0)
            assert queued_sets(app.store)[0].requested == app.clock.now()
            await advance(app, 600)
            await until(lambda: bool(ready_set(app.store, 101, 1)))
        finally:
            loop.cancel()
            await asyncio.wait({loop})
        assert not app.selfie_booth.running
    run(go())


@pytest.mark.parametrize("holder", ["human", "offline_raid", "voice"])
def test_any_live_lease_defers_every_gpu_call(app, holder):
    async def go():
        app.arbiter.hold(holder)
        await advance(app, 3600)
        await tick(app)
        assert not app.arbiter.admin.calls
        assert not ready_set(app.store, 101, 1)
    run(go())


def test_online_and_fresh_logout_wait_the_same_presence_grace(app):
    async def go():
        app.night_scheduler.presence._say(True)
        await advance(app, 3600)
        await tick(app)
        assert not ready_set(app.store, 101, 1)
        app.night_scheduler.presence._say(False)
        await advance(app, 599)
        await tick(app)
        assert not ready_set(app.store, 101, 1)
        await advance(app, 1)
        await tick(app)
        assert ready_set(app.store, 101, 1)
    run(go())


def test_request_wait_is_independent_of_how_long_owner_has_been_offline(app):
    async def go():
        await advance(app, 3600)
        app.store.conn.execute("UPDATE selfie_set SET requested=? WHERE guid=101", (app.clock.now(),))
        app.store.conn.commit()
        await tick(app)
        assert not ready_set(app.store, 101, 1)
        await advance(app, 600)
        await tick(app)
        assert ready_set(app.store, 101, 1)
    run(go())


def test_daily_limit_persists_between_passes_and_night_renders_leftovers(app):
    async def go():
        app.selfie_booth.cfg = replace(app.selfie_booth.cfg, booth_sets_per_day=1)
        queue_more(app.store, 102, now=app.clock.now())
        await advance(app, 600)
        await tick(app)
        assert len(queued_sets(app.store)) == 1
        await tick(app)
        assert len(queued_sets(app.store)) == 1
        ctx = app.night_runner.make_ctx(night_of(app.clock.local()), (0, app.clock.now()))
        ctx.settings = replace(ctx.settings, selfie_per_night=1)
        await make_stage(app.selfie_booth.cfg, worker=MemoryWorker)(ctx)
        assert not queued_sets(app.store)
        assert _status(app.store, app.clock)["booth"]["units"] == 1
        assert app.store.conn.execute("SELECT count(*) FROM night_step WHERE stage='selfies' "
                                      "AND unit<>'queue'").fetchone()[0] == 1
    run(go())


def test_new_local_day_has_its_own_allowance(app):
    async def go():
        app.selfie_booth.cfg = replace(app.selfie_booth.cfg, booth_sets_per_day=1)
        await advance(app, 600)
        await tick(app)
        queue_more(app.store, 101, now=app.clock.now())
        await advance(app, 86400)
        finished_night(app)
        await tick(app)
        assert ready_set(app.store, 101, 2)
        assert app.store.conn.execute("SELECT count(*) FROM night_step WHERE stage=? AND "
                                      "unit<>'queue'", (STAGE,)).fetchone()[0] == 2
    run(go())


def test_held_lust_set_stays_queued_without_gpu_or_daily_receipt(app):
    from leeroy_mind.personas.io import load_persona
    p = load_persona(app.store, 101)
    _record(app.store, 101, LUST, p.photo)  # recorded lust kind, wearer is no longer lust-styled
    async def go():
        await advance(app, 600)
        await tick(app)
        assert not ready_set(app.store, 101, 1)
        assert not app.arbiter.admin.calls
        assert not app.store.conn.execute("SELECT 1 FROM night_step WHERE stage=? AND "
                                         "unit='101:1'", (STAGE,)).fetchone()
    run(go())


def test_nsfw_refusal_uses_three_seeds_and_is_not_retried_all_day(app):
    w = MemoryWorker(flagged=True)
    app.selfie_booth.worker = lambda: w
    async def go():
        await advance(app, 600)
        await tick(app)
        assert len(w.requests) == 3
        assert len({r.seed for r in w.requests}) == 3
        assert all(r.nsfw_max == app.selfie_booth.cfg.nsfw_max for r in w.requests)
        assert not ready_set(app.store, 101, 1) and not set_dir(app.data_dir, 101, 1).exists()
        await tick(app)
        assert len(w.requests) == 3
        assert json.loads(app.store.get_meta(LAST))["outcome"] == "partial"
        # Failure is booth-only; the night's renderer still gets its normal opportunity.
        ctx = app.night_runner.make_ctx(night_of(app.clock.local()), (0, app.clock.now()))
        await make_stage(app.selfie_booth.cfg, worker=MemoryWorker)(ctx)
        assert ready_set(app.store, 101, 1)
    run(go())


def test_worker_exit_precedes_second_make_room(app):
    w = MemoryWorker()
    app.selfie_booth.worker = lambda: w
    observed = []
    original = app.arbiter.make_room
    async def room():
        observed.append((w.running, w.stops))
        await original()
    app.arbiter.make_room = room
    async def go():
        await advance(app, 600)
        await tick(app)
    run(go())
    assert observed == [(False, 0), (False, 1)]


def test_login_kills_fake_worker_and_child_before_live_preload(app, tmp_path):
    pids, requests = tmp_path / "pids.json", tmp_path / "requests.log"
    command, env = fake_command(tmp_path, FAKE_SELFIE_HANG="1", FAKE_SELFIE_CHILD="1",
                                FAKE_SELFIE_PIDS=str(pids), FAKE_SELFIE_LOG=str(requests))
    app.selfie_booth.worker = lambda: SelfieWorker(command, env=env)
    seen = []
    class Admin(FakeAdmin):
        async def preload(self, *args, **kwargs):
            row = json.loads(pids.read_text())
            seen.append((pid_alive(row["pid"]), pid_alive(row["child"])))
            await super().preload(*args, **kwargs)
    app.arbiter.admin = Admin([app.cfg.models.embedding])
    async def go():
        await advance(app, 600)
        await app.selfie_booth.tick()
        await until(requests.exists)
        await app.night_scheduler.on_event(Login(ts=app.clock.now(), guid=OWNER, human=True))
        await app.night_scheduler.handback()
        assert app.selfie_booth._task.cancelled()
        assert seen == [(False, False)]
        assert not ready_set(app.store, 101, 1)
        assert json.loads(app.store.get_meta(LAST))["outcome"] == "preempted"
    run(go())


def test_night_preempts_booth_and_waits_for_its_tree(app, tmp_path):
    pids, requests = tmp_path / "pids.json", tmp_path / "requests.log"
    app.selfie_booth.worker = fake_worker(tmp_path, FAKE_SELFIE_HANG="1", FAKE_SELFIE_CHILD="1",
        FAKE_SELFIE_PIDS=str(pids), FAKE_SELFIE_LOG=str(requests))
    seen = []
    async def night(n, window):
        row = json.loads(pids.read_text())
        seen.append((pid_alive(row["pid"]), pid_alive(row["child"])))
        await app.selfie_booth.tick()
        assert app.selfie_booth._task.cancelled()
    app.selfie_booth._night = night
    async def go():
        await advance(app, 600)
        await app.selfie_booth.tick()
        await until(requests.exists)
        app.store.conn.execute("DELETE FROM night_run")
        app.store.conn.commit()
        await app.night_scheduler.tick()
        await app.night_scheduler.wait_idle()
        assert seen == [(False, False)]
    run(go())


def test_due_night_and_night_admitted_after_tick_take_priority(app):
    async def go():
        await advance(app, 600)
        app.store.conn.execute("DELETE FROM night_run")
        app.store.conn.commit()
        assert app.night_scheduler.due() is not None
        await tick(app)
        assert app.selfie_booth._task is None
        finished_night(app)
        await app.selfie_booth.tick()
        app.store.conn.execute("DELETE FROM night_run")
        app.store.conn.commit()
        await asyncio.wait({app.selfie_booth._task})
        assert not app.arbiter.admin.calls
    run(go())


def test_lease_arriving_after_tick_prevents_worker_start(app):
    async def go():
        await advance(app, 600)
        await app.selfie_booth.tick()
        app.arbiter.hold("voice")
        await asyncio.wait({app.selfie_booth._task})
        assert not app.arbiter.admin.calls and not ready_set(app.store, 101, 1)
    run(go())


def test_booth_does_not_start_during_night_cleanup(app):
    async def go():
        entered, cleanup, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def night(n, window):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleanup.set()
                await release.wait()
        app.selfie_booth._night = night
        await advance(app, 600)
        task = asyncio.create_task(app.selfie_booth.run_night("test", (0, app.clock.now())))
        await entered.wait()
        task.cancel()
        await cleanup.wait()
        await tick(app)
        assert app.selfie_booth._task is None and not app.arbiter.admin.calls
        release.set()
        await asyncio.wait({task})
        await tick(app)
        assert ready_set(app.store, 101, 1)
    run(go())


def test_crossing_midnight_never_publishes_against_previous_day_allowance(app):
    async def go():
        await advance(app, 600)
        moved = False
        async def render(req):
            nonlocal moved
            if not moved:
                moved = True
                await advance(app, 86400)
        app.selfie_booth.worker = lambda: MemoryWorker(on_render=render)
        await tick(app)
        assert not ready_set(app.store, 101, 1)
        assert not set_dir(app.data_dir, 101, 1).exists()
        assert json.loads(app.store.get_meta(LAST))["sets"] == 0
        finished_night(app)
        app.selfie_booth.worker = MemoryWorker
        await tick(app)
        assert ready_set(app.store, 101, 1)
    run(go())


def test_partial_cancel_reports_already_committed_sets(app):
    async def go():
        queue_more(app.store, 102, now=app.clock.now())
        ready_for_cancel = asyncio.Event()
        async def render(req):
            # More is prioritized: member 102 commits, then 101 blocks.
            if "101" in req.out.parts:
                ready_for_cancel.set()
                await asyncio.Event().wait()
        w = MemoryWorker(on_render=render)
        app.selfie_booth.worker = lambda: w
        await advance(app, 600)
        await app.selfie_booth.tick()
        await ready_for_cancel.wait()
        await app.selfie_booth.cancel()
        row = json.loads(app.store.get_meta(LAST))
        assert row["sets"] == 1 and row["outcome"] == "preempted"
        assert ready_set(app.store, 102, 2) and not ready_set(app.store, 101, 1)
        assert w.stops == 1
        assert _status(app.store, app.clock)["booth"]["calls"] == 5
    run(go())


def test_stopping_background_loop_ends_worker_tree(app, tmp_path):
    pids, requests = tmp_path / "pids.json", tmp_path / "requests.log"
    app.selfie_booth.worker = fake_worker(tmp_path, FAKE_SELFIE_HANG="1", FAKE_SELFIE_CHILD="1",
        FAKE_SELFIE_PIDS=str(pids), FAKE_SELFIE_LOG=str(requests))
    async def go():
        await advance(app, 600)
        loop = asyncio.create_task(app.selfie_booth.run_forever())
        await until(requests.exists)
        loop.cancel()
        await asyncio.wait({loop})
        row = json.loads(pids.read_text())
        assert not pid_alive(row["pid"]) and not pid_alive(row["child"])
        assert not app.selfie_booth.running and not ready_set(app.store, 101, 1)
    run(go())


def test_restart_preserves_daily_allowance(app):
    from leeroy_mind.app import MindApp
    from leeroy_mind.config import load_config
    from leeroy_mind.testing.fake_llm import FakeLLM
    async def go():
        app.selfie_booth.cfg = replace(app.selfie_booth.cfg, booth_sets_per_day=1)
        await advance(app, 600)
        await tick(app)
        queue_more(app.store, 101, now=app.clock.now())
        (app.data_dir / "mind.toml").write_text(ON + "booth_sets_per_day=1\n")
        restarted = MindApp(load_config(app.data_dir / "mind.toml"), app.data_dir,
                            llm=FakeLLM(), clock=app.clock)
        try:
            restarted.arbiter.admin = FakeAdmin([])
            restarted.selfie_booth.worker = MemoryWorker
            await advance(restarted, 600)
            await tick(restarted)
            assert not ready_set(restarted.store, 101, 2)
            assert not restarted.arbiter.admin.calls
        finally:
            restarted.store.close()
    run(go())


def test_failed_admission_stops_worker_and_records_zero_render_calls(app, caplog):
    w = MemoryWorker()
    app.selfie_booth.worker = lambda: w
    async def broken_room():
        raise RuntimeError("private exception sentinel")
    app.arbiter.make_room = broken_room
    async def go():
        await advance(app, 600)
        await tick(app)
        assert w.stops == 1 and not w.requests
        status = _status(app.store, app.clock)["booth"]
        assert status["calls"] == 0 and status["last"]["outcome"] == "failed"
        assert not ready_set(app.store, 101, 1)
    with caplog.at_level(logging.INFO, logger="leeroy_mind"):
        run(go())
    assert "private exception sentinel" not in caplog.text


@pytest.mark.parametrize("toml", ["", "[selfies]\nbooth=true\n", "[selfies]\nenabled=true\n",
    "[selfies]\nenabled=true\nbooth=false\n", "[selfies]\nenabled=true\nbooth='yes'\n",
    ON + "booth_wait_minutes=0\n", ON + "booth_sets_per_day=false\n"])
def test_off_or_invalid_registers_no_booth_and_keeps_normal_scheduler(tmp_path, toml):
    app = mind_app(tmp_path, toml=toml)
    try:
        assert not hasattr(app, "selfie_booth")
        assert app.night_scheduler.run_night.__self__ is app.night_runner
        assert app.arbiter._preempt.__self__ is app.night_scheduler
    finally:
        app.store.close()
