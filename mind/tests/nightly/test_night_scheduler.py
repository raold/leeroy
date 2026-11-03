"""Presence and the night scheduler (P1c Task 4): when the night's run starts, stops and comes
back, and how the GPU changes hands at his login without the bridge's dispatcher waiting on it.
Fakes only: FakeAdmin stands in for Ollama and run_night is test-local, so no model is loaded and
no night stage runs."""
import asyncio
import logging
import sqlite3
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from conftest import run
from leeroy_mind.clock import night_of
from leeroy_mind.llm.client import LLMError
from leeroy_mind.nightly.gpu import GpuArbiter
from leeroy_mind.nightly.llm import ReflectionError
from leeroy_mind.nightly.presence import Presence
from leeroy_mind.nightly.scheduler import NightScheduler, window_for
from leeroy_mind.nightly.store import commit_unit, run_begin, run_finish, run_get, step_done
from leeroy_mind.protocol import Chat, Login, Logout
from nightly.conftest import FakeAdmin


def make_sched(h, *, block=False, fail=None, loaded=None):
    """A scheduler on h's store and clock, with a FakeAdmin (`loaded`: what Ollama holds, the
    embedding model by default) behind a GpuArbiter, and a run_night that records each (night,
    window) in `started`, then waits forever while `sched.block` is set and raises `sched.fail`
    while one is set (both start from the arguments). Returns (sched, started, admin)."""
    admin = FakeAdmin([h.EMB] if loaded is None else loaded)
    started = []

    async def run_night(night, window):
        started.append((night, window))
        if sched.block:
            await asyncio.Event().wait()
        if sched.fail is not None:
            raise sched.fail

    sched = NightScheduler(h.store, h.settings, GpuArbiter(admin, h.settings), run_night,
                           clock=h.clock)
    sched.block, sched.fail = block, fail
    return sched, started, admin


async def noop_night(night, window):
    pass


class GatedAdmin(FakeAdmin):
    """FakeAdmin whose preload waits for `gate` (a 16 GB load from disk)."""

    def __init__(self, loaded):
        super().__init__(loaded)
        self.gate = asyncio.Event()

    async def preload(self, model, keep_alive, *, options=None):
        await self.gate.wait()
        await super().preload(model, keep_alive, options=options)


class DownAdmin(FakeAdmin):
    """FakeAdmin with Ollama down: /api/ps is refused."""

    async def loaded_models(self):
        raise LLMError("/api/ps: connection refused")


# -- the plan's scenarios -----------------------------------------------------------------------

def test_logout_then_grace_starts_tonight(h):
    async def go():
        sched, started, _ = make_sched(h)
        await sched.on_event(h.hello(1)); await sched.on_event(h.logout())
        h.clock.advance(9 * 60); await sched.tick(); assert started == []
        h.clock.advance(2 * 60); await sched.tick(); await sched.wait_idle()
        night = night_of(h.clock.local())
        assert started[0][0] == night and run_get(h.store, night).status == "done"
    run(go())


def test_login_cancels_run_and_hands_gpu_back(h):
    async def go():
        sched, started, admin = make_sched(h, block=True, loaded=[h.REFL, h.EMB])
        await sched.on_event(h.hello(0)); h.clock.advance(11 * 60)
        await sched.tick(); await asyncio.sleep(0)
        await sched.on_event(h.login())
        assert sched.running is None and sched.arbiter.holders == {"human"}   # inline, in the handler
        await sched.handback()                       # the GPU work is a task of its own (PR18)
        assert sched.running is None and admin.calls[-2:] == [("unload", h.REFL), ("preload", h.LIVE, -1)]
        assert run_get(h.store, night_of(h.clock.local())).status == "running" and h.count("night_step") == 0
    run(go())


def test_resume_reuses_window_and_runs_once_per_night(h):
    async def go():
        sched, started, _ = make_sched(h, block=True)
        await sched.on_event(h.hello(0)); h.clock.advance(11 * 60); await sched.tick(); await asyncio.sleep(0)
        await sched.on_event(h.login()); await sched.on_event(h.logout())
        sched.block = False                          # test-local switch read by run_night
        h.clock.advance(11 * 60); await sched.tick(); await sched.wait_idle()
        h.clock.advance(60 * 60); await sched.tick()
        assert len(started) == 2 and started[1][1] == started[0][1]
    run(go())


def test_catch_up_after_days_off(h):
    t0 = datetime(2026, 10, 24, 23, tzinfo=timezone.utc).timestamp()
    run_begin(h.store, "2026-10-24", 0.0, t0, now=t0); run_finish(h.store, "2026-10-24", now=t0, status="done")
    async def go():
        sched, started, _ = make_sched(h)                 # no hello: the bridge has been down all day
        h.clock.advance(11 * 60); await sched.tick(); await sched.wait_idle()
        assert len(started) == 1 and started[0][1] == (t0, h.clock.now())
    run(go())


def test_failure_backs_off_and_gives_up(h):
    async def go():
        sched, started, _ = make_sched(h, fail=ReflectionError("no model", fatal=True))
        await sched.on_event(h.hello(0)); h.clock.advance(11 * 60)
        await sched.tick(); await sched.wait_idle()
        assert run_get(h.store, night_of(h.clock.local())).status == "failed"
        h.clock.advance(30 * 60); await sched.tick(); assert len(started) == 1
        for _ in range(3):
            h.clock.advance(61 * 60); await sched.tick(); await sched.wait_idle()
        assert len(started) == 3
    run(go())


def test_offline_raid_lease_blocks_nightly(h):
    async def go():
        sched, _, _ = make_sched(h)
        await sched.on_event(h.hello(0)); h.clock.advance(11 * 60)
        await sched.arbiter.acquire_live("offline_raid"); assert sched.due() is None
        await sched.arbiter.release_live("offline_raid"); assert sched.due() == night_of(h.clock.local())
    run(go())


def test_a_run_past_the_cutoff_never_starts_the_next_night_at_once(h):
    start = datetime(2026, 10, 30, 1, 30, tzinfo=timezone.utc).timestamp()      # he logged off late on 2026-10-29
    run_begin(h.store, "2026-10-29", 0.0, start, now=start)
    h.clock.set(datetime(2026, 10, 30, 7, 10, tzinfo=timezone.utc).timestamp())  # the run went past 06:00
    run_finish(h.store, "2026-10-29", now=h.clock.now(), status="done")
    async def go():
        sched, started, _ = make_sched(h)
        await sched.on_event(h.hello(0)); h.clock.advance(11 * 60)
        assert sched.due() is None                   # 2026-10-30 would cover only 5.9 hours, and the last run just ended
        h.clock.advance(6 * 3600)
        assert sched.due() == "2026-10-30"           # six hours on: a full window and a rested GPU
    run(go())


# -- presence ------------------------------------------------------------------------------------

def test_presence_reads_the_hello_and_his_own_login_and_logout(h):
    p = Presence(h.clock)
    assert p.online is None and p.offline_seconds() == 0.0          # not known yet: counted from the start
    h.clock.advance(60); assert p.offline_seconds() == 60.0
    assert p.on_event(h.hello(1)) == "login" and p.online is True
    h.clock.advance(60); assert p.offline_seconds() == 0.0          # nothing counts while he is on
    assert p.on_event(Login(ts=0, guid=101, human=False)) is None
    assert p.on_event(Logout(ts=0, guid=101, human=False)) is None and p.online is True    # a member's
    assert p.on_event(h.logout()) == "logout" and p.online is False
    h.clock.advance(300)
    assert p.on_event(h.hello(0)) == "logout" and p.offline_seconds() == 300.0   # said again: the count goes on
    assert p.on_event(h.login()) == "login" and p.offline_seconds() == 0.0
    chat = Chat(ts=0, channel="guild", from_guid=1, from_name="Testmage", to_guid=0, text="hi",
                map=0, zone=0)
    assert p.on_event(chat) is None and p.online is True


def test_presence_counts_from_the_last_change_and_a_drop_is_offline_as_of_then(h):
    p = Presence(h.clock); h.clock.advance(120)
    assert p.on_connection(False) is None and p.online is None and p.offline_seconds() == 120.0
    assert p.on_event(h.hello(0)) == "logout" and p.offline_seconds() == 0.0     # the hello's word starts the count
    p.on_event(h.hello(1)); h.clock.advance(60)
    assert p.on_connection(True) is None and p.online is True               # a connect says nothing; the hello will
    assert p.on_connection(False) == "logout" and p.online is False and p.offline_seconds() == 0.0
    h.clock.advance(30)
    assert p.on_connection(False) is None and p.offline_seconds() == 30.0     # already offline
    h.clock.advance(-600); assert p.offline_seconds() == 0.0                  # a clock set back counts no time


# -- his login and logout on the bridge's one dispatcher (PR18) ----------------------------------

def test_a_login_and_a_logout_in_one_burst_leave_nobody_holding_and_the_gpu_alone(h):
    async def go():
        sched, started, admin = make_sched(h, block=True, loaded=[h.REFL, h.EMB])
        await sched.on_event(h.hello(0)); h.clock.advance(11 * 60)
        await sched.tick(); await asyncio.sleep(0)
        await sched.on_event(h.login()); await sched.on_event(h.logout())    # no yield between the two
        assert sched.arbiter.holders == frozenset() and sched.running is None   # the login stopped the run inline
        await asyncio.wait_for(sched.handback(), 5); await asyncio.wait_for(sched.wait_idle(), 5)
        assert sched.arbiter.holders == frozenset() and sched.arbiter.reflection_allowed()
        assert admin.calls == [] and admin.loaded == [h.REFL, h.EMB]        # no live model loaded for nobody
        assert run_get(h.store, started[0][0]).status == "running"          # paused, to resume
    run(go())


def test_the_login_handler_returns_before_the_gpu_work(h):
    async def go():
        admin = GatedAdmin([h.REFL, h.EMB])
        sched = NightScheduler(h.store, h.settings, GpuArbiter(admin, h.settings), noop_night,
                               clock=h.clock)
        await asyncio.wait_for(sched.on_event(h.login()), 1)    # the dispatcher never waits on a preload
        for _ in range(3):
            await asyncio.sleep(0)
        assert admin.calls == [("unload", h.REFL)] and admin.loaded == [h.EMB]    # held at the preload
        idle = asyncio.create_task(sched.wait_idle())
        for _ in range(3):
            await asyncio.sleep(0)
        assert not idle.done()                                   # idle: no hand-back in flight either
        admin.gate.set(); await asyncio.wait_for(idle, 5)
        assert admin.calls == [("unload", h.REFL), ("preload", h.LIVE, -1)]
    run(go())


def test_the_run_has_wound_down_before_the_gpu_changes_hands(h):
    async def go():
        admin = FakeAdmin([h.REFL, h.EMB]); ended = []
        async def run_night(night, window):
            try:
                await asyncio.Event().wait()
            finally:
                for _ in range(3):
                    await asyncio.sleep(0)               # a cleanup that takes a few turns of the loop
                ended.append(list(admin.calls))
        sched = NightScheduler(h.store, h.settings, GpuArbiter(admin, h.settings), run_night,
                               clock=h.clock)
        await sched.on_event(h.hello(0)); h.clock.advance(11 * 60)
        await sched.tick(); await asyncio.sleep(0)
        await sched.on_event(h.login()); await sched.handback()
        assert ended == [[]]                              # over before the unload, not during it
        assert admin.calls == [("unload", h.REFL), ("preload", h.LIVE, -1)]
    run(go())


def test_his_login_during_an_offline_raid_hands_nothing_back_twice(h):
    async def go():
        sched, _, admin = make_sched(h, loaded=[h.REFL, h.EMB])
        await sched.arbiter.acquire_live("offline_raid")        # P1d's raid already has the live model
        calls = list(admin.calls)
        await sched.on_event(h.login()); await sched.handback()
        assert admin.calls == calls and sched.arbiter.holders == {"offline_raid", "human"}
        await sched.arbiter.release_live("offline_raid"); h.clock.advance(11 * 60)
        assert sched.due() is None                               # he is still on
        await sched.on_event(h.logout()); assert sched.arbiter.reflection_allowed()
    run(go())


def test_a_failed_hand_back_is_logged_and_his_logout_still_ends_the_lease(h, caplog):
    async def go():
        arb = GpuArbiter(DownAdmin([h.EMB]), h.settings)
        sched = NightScheduler(h.store, h.settings, arb, noop_night, clock=h.clock)
        await sched.on_event(h.login()); await sched.handback()
        assert arb.holders == {"human"}                          # he is online all the same
        await sched.on_event(h.logout()); assert arb.reflection_allowed()
    with caplog.at_level(logging.WARNING, logger="leeroy_mind"):
        run(go())
    assert "nightly_handback_failed" in caplog.text and "connection refused" in caplog.text


def test_a_dropped_bridge_ends_his_lease_and_he_counts_as_offline_from_the_drop(h):
    async def go():
        sched, _, _ = make_sched(h)
        await sched.on_event(h.hello(1)); await sched.handback()
        h.clock.advance(3600)
        await sched.on_connection(True); assert sched.arbiter.holders == {"human"}
        await sched.on_connection(False)
        assert sched.arbiter.holders == frozenset() and sched.presence.online is False
        h.clock.advance(9 * 60); assert sched.due() is None       # counted from the drop, not the start
        h.clock.advance(2 * 60); assert sched.due() == night_of(h.clock.local())
    run(go())


# -- cancel_run, the arbiter's preempt -----------------------------------------------------------

def test_cancel_run_absorbs_only_the_runs_own_cancel(h):
    async def go():
        gate = asyncio.Event()
        async def run_night(night, window):
            try:
                await asyncio.Event().wait()
            finally:
                await gate.wait()                         # a slow wind-down
        sched = NightScheduler(h.store, h.settings, GpuArbiter(FakeAdmin([h.EMB]), h.settings),
                               run_night, clock=h.clock)
        await sched.on_event(h.hello(0)); h.clock.advance(11 * 60)
        await sched.tick(); await asyncio.sleep(0)
        waiter = asyncio.create_task(sched.cancel_run())
        for _ in range(3):
            await asyncio.sleep(0)
        assert not waiter.done() and sched.running is None     # told to stop, still winding down
        waiter.cancel()                                        # the preempt's deadline, say
        with pytest.raises(asyncio.CancelledError):
            await waiter                                       # its own cancel reaches its caller
        idle = asyncio.create_task(sched.wait_idle())
        for _ in range(3):
            await asyncio.sleep(0)
        assert not idle.done()                                 # and the run winds down undisturbed
        gate.set(); await idle
        assert run_get(h.store, night_of(h.clock.local())).status == "running"
    run(go())


# -- the loop ------------------------------------------------------------------------------------

def test_run_forever_ticks_on_the_mind_clock_and_leaves_the_run_resumable_when_stopped(h):
    async def go():
        sched, started, _ = make_sched(h, block=True)
        sched.tick_seconds = 60
        sleeps = []
        async def sleep(s):
            sleeps.append(s); h.clock.advance(s); await asyncio.sleep(0)
        h.clock.sleep = sleep
        await sched.on_event(h.hello(0))
        loop = asyncio.create_task(sched.run_forever())
        for _ in range(500):
            if started:
                break
            await asyncio.sleep(0)
        assert set(sleeps) == {60} and started == [(h.night, (0.0, h.T + 10 * 60))]   # the grace, tick by tick
        loop.cancel()
        with pytest.raises(asyncio.CancelledError):
            await loop
        assert sched.running is None and run_get(h.store, h.night).status == "running"
        await asyncio.wait_for(sched.wait_idle(), 1)            # the mind stopping stopped the run
    run(go())


def test_a_tick_that_fails_is_logged_and_the_loop_goes_on(h, caplog):
    async def go():
        sched, _, _ = make_sched(h)
        dues = []
        def due():
            dues.append(1)
            if len(dues) == 1:
                raise sqlite3.OperationalError("database is locked")
            return None
        sched.due = due
        async def sleep(s):
            await asyncio.sleep(0)
        h.clock.sleep = sleep
        loop = asyncio.create_task(sched.run_forever())
        for _ in range(100):
            if len(dues) >= 3:
                break
            await asyncio.sleep(0)
        loop.cancel()
        with pytest.raises(asyncio.CancelledError):
            await loop
        assert len(dues) >= 3
    with caplog.at_level(logging.WARNING, logger="leeroy_mind"):
        run(go())
    assert "nightly_tick_failed" in caplog.text and "database is locked" in caplog.text


# -- windows and attempts ------------------------------------------------------------------------

def test_a_run_a_restart_left_running_resumes_with_its_window(h):
    run_begin(h.store, h.night, 100.0, h.T - 3600, now=h.T - 3600)      # the mind stopped mid-night
    async def go():
        sched, started, _ = make_sched(h)
        h.clock.advance(11 * 60); await sched.tick(); await sched.wait_idle()
        assert started == [(h.night, (100.0, h.T - 3600))]
        r = run_get(h.store, h.night)
        assert r.status == "done" and r.attempts == 1         # a resume is no new attempt
    run(go())


def test_an_interrupted_last_retry_still_resumes(h):
    for _ in range(h.settings.max_attempts - 1):             # two failures ...
        run_begin(h.store, h.night, 0.0, h.T, now=h.T)
        run_finish(h.store, h.night, now=h.T, status="failed", error="ReflectionError('x')")
    run_begin(h.store, h.night, 0.0, h.T, now=h.T)           # ... then the last retry, which his login interrupted
    async def go():
        sched, started, _ = make_sched(h)
        h.clock.advance(11 * 60); await sched.tick(); await sched.wait_idle()
        r = run_get(h.store, h.night)
        assert started == [(h.night, (0.0, h.T))] and r.status == "done"
        assert r.attempts == h.settings.max_attempts         # and its resume took no attempt of its own
    run(go())


def test_two_interruptions_then_ollama_down_at_the_next_begin_and_the_night_is_still_retried(h):
    async def go():
        sched, started, _ = make_sched(h, block=True)
        await sched.on_event(h.hello(0))
        for _ in range(2):                                   # he logs in on the night twice
            h.clock.advance(11 * 60); await sched.tick(); await asyncio.sleep(0)
            await sched.on_event(h.login()); await sched.handback(); await sched.on_event(h.logout())
        sched.block = False
        sched.fail = ReflectionError("cannot list Ollama's models: connection refused", fatal=True)
        h.clock.advance(11 * 60); await sched.tick(); await sched.wait_idle()
        assert run_get(h.store, h.night).status == "failed"  # Ollama was briefly down
        sched.fail = None
        h.clock.advance(61 * 60); await sched.tick(); await sched.wait_idle()
        r = run_get(h.store, h.night)
        assert len(started) == 4 and r.status == "done" and r.attempts == 2   # one failure, one retry
    run(go())


def test_three_interruptions_then_a_resume_the_same_night(h):
    async def go():
        sched, started, _ = make_sched(h, block=True)
        await sched.on_event(h.hello(0))
        for _ in range(3):                                   # he plays in the night's window three times
            h.clock.advance(11 * 60); await sched.tick(); await asyncio.sleep(0)
            assert sched.running == h.night
            await sched.on_event(h.login()); await sched.handback()
            await sched.on_event(h.logout())
        sched.block = False
        h.clock.advance(11 * 60); await sched.tick(); await sched.wait_idle()
        assert [n for n, _ in started] == [h.night] * 4 and {w for _, w in started} == {(0.0, h.T + 11 * 60)}
        r = run_get(h.store, h.night)
        assert r.status == "done" and r.attempts == 1        # the interruptions cost the night nothing
    run(go())


def test_a_failed_run_is_retried_only_more_than_retry_minutes_after_it_failed(h):
    run_begin(h.store, h.night, 0.0, h.T, now=h.T)
    run_finish(h.store, h.night, now=h.T, status="failed", error="ReflectionError('no model')")
    async def go():
        sched, _, _ = make_sched(h)
        h.clock.advance(h.settings.retry_minutes * 60); assert sched.due() is None   # exactly 60 minutes: not yet
        h.clock.advance(1); assert sched.due() == h.night
    run(go())


def test_a_long_run_that_ended_past_six_holds_the_next_night_back_whatever_its_window(h):
    end = datetime(2026, 10, 28, 23, tzinfo=timezone.utc).timestamp()           # he logged off at 23:00
    run_begin(h.store, "2026-10-28", 0.0, end, now=end)
    h.clock.set(datetime(2026, 10, 29, 8, tzinfo=timezone.utc).timestamp())     # a long catch-up, done at 08:00
    run_finish(h.store, "2026-10-28", now=h.clock.now(), status="done")
    async def go():
        sched, _, _ = make_sched(h)
        await sched.on_event(h.hello(0)); h.clock.advance(11 * 60)
        assert sched.due() is None                   # a 9-hour window, but the GPU has just been busy
        h.clock.advance(6 * 3600 - 11 * 60 - 1); assert sched.due() is None
        h.clock.advance(1); assert sched.due() == "2026-10-29"          # six hours after it finished
    run(go())


def test_a_night_whose_window_would_be_short_waits_until_it_is_long_enough(h):
    end = h.T - 3 * 3600
    run_begin(h.store, "2026-10-28", 0.0, end, now=end); run_finish(h.store, "2026-10-28", now=end, status="done")
    async def go():
        sched, started, _ = make_sched(h)
        sched.settings = replace(h.settings, min_gap_hours=0.0)        # the window alone decides
        h.clock.advance(3 * 3600 - 1); await sched.tick(); assert started == []     # six hours less a second
        h.clock.advance(1); await sched.tick(); await sched.wait_idle()
        assert started == [(h.night, (end, h.clock.now()))]     # from where the last window ended: nothing skipped (N8)
    run(go())


def test_window_for_reuses_a_runs_window_and_otherwise_starts_at_the_last_done_end(h):
    assert window_for(h.store, h.night, 500.0) == (0.0, 500.0)              # nothing done yet: from the start
    run_begin(h.store, "2026-10-27", 0.0, 300.0, now=300.0)
    run_finish(h.store, "2026-10-27", now=310.0, status="done")
    run_begin(h.store, "2026-10-28", 300.0, 400.0, now=400.0)
    run_finish(h.store, "2026-10-28", now=410.0, status="failed", error="x")
    assert window_for(h.store, h.night, 500.0) == (300.0, 500.0)            # a failed run's window is not done
    run_begin(h.store, h.night, 300.0, 450.0, now=450.0)
    assert window_for(h.store, h.night, 900.0) == (300.0, 450.0)            # the night's own window, again


def test_run_begin_can_resume_a_run_without_an_attempt(h):
    run_begin(h.store, h.night, 0.0, 100.0, now=100.0)
    run_finish(h.store, h.night, now=150.0, status="interrupted")
    r = run_begin(h.store, h.night, 0.0, 999.0, now=200.0, count_attempt=False)
    assert (r.status, r.attempts, r.window_end) == ("running", 1, 100.0)


# -- fix round 1: a run interrupted across 06:00, the runner's summary, a runner that misbehaves --

def test_an_interrupted_night_resumes_first_when_his_next_offline_time_is_past_six(h):
    async def go():
        sched, started, _ = make_sched(h, block=True)
        await sched.on_event(h.hello(0)); h.clock.advance(11 * 60)            # 21:11 on 2026-10-29
        await sched.tick(); await asyncio.sleep(0)
        await sched.on_event(h.login()); await sched.handback()               # he plays ...
        h.clock.set(datetime(2026, 10, 30, 7, 0, tzinfo=timezone.utc).timestamp())    # ... past 06:00
        await sched.on_event(h.logout())
        sched.block = False
        h.clock.advance(11 * 60); await sched.tick(); await sched.wait_idle()
        first = (0.0, h.T + 11 * 60)
        assert started == [("2026-10-29", first), ("2026-10-29", first)]      # last night first, as it stood
        old = run_get(h.store, "2026-10-29")
        assert old.status == "done" and old.attempts == 1 and run_get(h.store, "2026-10-30") is None
        h.clock.advance(6 * 3600 - 1); await sched.tick(); assert len(started) == 2      # then the gap
        h.clock.advance(1); await sched.tick(); await sched.wait_idle()
        assert started[2] == ("2026-10-30", (first[1], h.clock.now()))       # from where it ended: nothing twice
    run(go())


def test_an_unfinished_run_older_than_the_last_two_nights_is_abandoned_and_its_units_stay(h, caplog):
    older_end, stale_end = h.T - 3 * 86400, h.T - 2 * 86400                  # 2026-10-26 and -27, 21:00
    run_begin(h.store, "2026-10-26", 0.0, older_end, now=older_end)
    run_finish(h.store, "2026-10-26", now=older_end, status="interrupted")
    run_begin(h.store, "2026-10-27", older_end, stale_end, now=stale_end)    # interrupted, then the PC was off
    commit_unit(h.store, "2026-10-27", "reflect", "101", lambda store: None, now=stale_end)
    with h.store.transaction():
        h.store.conn.execute("UPDATE night_run SET error = ? WHERE night = ?", ('{"reflect": 1}', "2026-10-27"))
    async def go():
        sched, started, _ = make_sched(h)
        h.clock.advance(11 * 60)
        assert sched.due() == h.night                            # never a night older than the last two
        await sched.tick(); await sched.wait_idle()
        return started
    with caplog.at_level(logging.WARNING, logger="leeroy_mind"):
        started = run(go())
    stale = run_get(h.store, "2026-10-27")
    assert stale.status == "abandoned" and stale.error == '{"reflect": 1}'
    assert run_get(h.store, "2026-10-26").status == "abandoned"         # an interrupted one too
    assert "nightly_abandoned: night=2026-10-26" in caplog.text and "nightly_abandoned: night=2026-10-27" in caplog.text
    assert started == [(h.night, (stale_end, h.T + 11 * 60))]   # never resumed; tonight starts where it ended
    assert step_done(h.store, "2026-10-27", "reflect", "101")       # and what it finished stays done


def test_a_done_run_keeps_the_unit_failure_summary_the_runner_left(h):
    async def run_night(night, window):                         # Task 5 leaves its unit failures in error
        with h.store.transaction():
            h.store.conn.execute("UPDATE night_run SET error = ? WHERE night = ?", ('{"reflect": 2}', night))
    async def go():
        sched = NightScheduler(h.store, h.settings, GpuArbiter(FakeAdmin([h.EMB]), h.settings),
                               run_night, clock=h.clock)
        await sched.on_event(h.hello(0)); h.clock.advance(11 * 60)
        await sched.tick(); await sched.wait_idle()
    run(go())
    r = run_get(h.store, h.night)
    assert r.status == "done" and r.error == '{"reflect": 2}'    # what Task 23's night_health reads


def test_a_run_that_swallows_its_cancel_is_not_marked_done_and_resumes(h):
    calls = []
    async def run_night(night, window):
        calls.append(window)
        if len(calls) == 1:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return                                          # a runner that swallows its cancel
    async def go():
        sched = NightScheduler(h.store, h.settings, GpuArbiter(FakeAdmin([h.REFL, h.EMB]), h.settings),
                               run_night, clock=h.clock)
        await sched.on_event(h.hello(0)); h.clock.advance(11 * 60)
        await sched.tick(); await asyncio.sleep(0)
        await sched.on_event(h.login()); await sched.handback(); await sched.wait_idle()
        assert run_get(h.store, h.night).status == "running"     # not done: units may be unfinished
        await sched.on_event(h.logout()); h.clock.advance(11 * 60)
        await sched.tick(); await sched.wait_idle()
        r = run_get(h.store, h.night)
        assert len(calls) == 2 and calls[1] == calls[0] and r.status == "done" and r.attempts == 1
    run(go())


def test_a_cleanup_that_raises_after_a_cancel_marks_the_run_interrupted_not_failed(h, caplog):
    calls, seen = [], []
    async def run_night(night, window):
        calls.append(window)
        if len(calls) == 1:
            try:
                await asyncio.Event().wait()
            finally:
                raise sqlite3.OperationalError("database is locked")    # a cleanup raising on its way out
        seen.append(run_get(h.store, night).status)
    async def go():
        sched = NightScheduler(h.store, h.settings, GpuArbiter(FakeAdmin([h.REFL, h.EMB]), h.settings),
                               run_night, clock=h.clock)
        await sched.on_event(h.hello(0)); h.clock.advance(11 * 60)
        await sched.tick(); await asyncio.sleep(0)
        await sched.on_event(h.login()); await sched.handback(); await sched.wait_idle()
        r = run_get(h.store, h.night)
        assert (r.status, r.attempts, r.error) == ("interrupted", 1, None)    # not failed: no attempt lost
        await sched.on_event(h.logout()); h.clock.advance(11 * 60)        # the grace, and no back-off
        await sched.tick(); await sched.wait_idle()
        r = run_get(h.store, h.night)
        assert seen == ["running"] and calls == [calls[0]] * 2 and (r.status, r.attempts) == ("done", 1)
    with caplog.at_level(logging.WARNING, logger="leeroy_mind"):
        run(go())
    assert "nightly_cleanup_failed" in caplog.text and "database is locked" in caplog.text
    assert "nightly_failed" not in caplog.text


def test_an_offline_raid_preempts_a_running_night_and_it_resumes_after_without_grace(h):
    async def go():
        sched, started, admin = make_sched(h, block=True, loaded=[h.REFL, h.EMB])
        await sched.on_event(h.hello(0)); h.clock.advance(11 * 60)
        await sched.tick(); await asyncio.sleep(0)
        assert sched.running == h.night
        async with sched.arbiter.live("offline_raid"):          # P1d's raid: its hand-over pre-empts the night
            assert sched.running is None and run_get(h.store, h.night).status == "running"
            assert admin.calls == [("unload", h.REFL), ("preload", h.LIVE, -1)]
            await sched.tick(); assert len(started) == 1        # held while the raid runs
        sched.block = False
        await sched.tick(); await sched.wait_idle()             # no grace: he never came back
        assert started == [started[0]] * 2 and run_get(h.store, h.night).status == "done"
    run(go())
