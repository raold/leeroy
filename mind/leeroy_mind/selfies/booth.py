"""An opt-in, once-a-minute offline profile-photo pass, separate from the night's budget.

The scheduler keeps its own due times. A due night takes priority, cancels a booth already in
flight and waits for its worker tree to end. One lock covers both runs, including cleanup.
Every live lease uses the arbiter's preempt to stop both; cancellation cleanup never calls
the arbiter, whose hand-over owns its lock while awaiting us.
"""
import asyncio
import json
import logging
import time
from contextlib import suppress
from collections import Counter

from leeroy_mind.llm.client import GpuPaused
from leeroy_mind.logs import log
from leeroy_mind.nightly.gpu import LiveLeaseHeld

from .config import worker_command, worker_env
from .render import RenderPolicy, done_sets, queue, render, todo
from .worker import SelfieWorker, to_the_end

STAGE = "selfies_booth"
LAST = "selfies:booth:last"
OFFLINE_SECONDS = 600
TICK_SECONDS = 60
COOLDOWN = "selfies:booth:cooldown_until"
STREAK = "selfies:booth:failures"
COOLDOWN_FIRST = 1200
COOLDOWN_MAX = 7200


class PhotoBooth:
    def __init__(self, app, cfg, *, worker=None):
        self.app, self.cfg = app, cfg
        self.scheduler, self.arbiter = app.night_scheduler, app.arbiter
        self.worker = worker or (lambda: SelfieWorker(worker_command(cfg), env=worker_env(cfg)))
        self._night = self.scheduler.run_night
        self._exclusive = asyncio.Lock()
        self._task = None
        self._stopping = False
        self.scheduler.run_night = self.run_night
        self.arbiter.set_preempt(self.preempt)

    @property
    def running(self):
        return self._task is not None and not self._task.done()

    def stop(self):
        """Ask a running pass to stop, with no await; cancel() then waits for its cleanup."""
        if self.running and not self._stopping:
            self._stopping = True
            self._task.cancel()

    async def cancel(self):
        if self.running:
            self.stop()
            # A cancelled hand-over must not cancel or abandon the worker's cleanup.
            await asyncio.wait({self._task})

    async def preempt(self):
        await self.scheduler.cancel_run()
        await self.cancel()

    async def run_night(self, night, window):
        await self.cancel()
        async with self._exclusive:
            await self._night(night, window)

    def _eligible(self):
        return (self.cfg.enabled and self.cfg.booth and not self.arbiter.holders
                and not getattr(self.arbiter, 'paused', False)
                and self.scheduler.presence.offline_seconds() >= OFFLINE_SECONDS
                and self.scheduler.running is None and not self._exclusive.locked()
                and self.scheduler.due() is None and not self._cooling())

    def problem(self):
        if getattr(self.arbiter, 'paused', False):
            return 'The GPU is paused. Resume it from the top bar first.'
        if not self.cfg.enabled or not self.cfg.booth:
            return "The photo booth is off."
        if self.running or self._exclusive.locked():
            return "A photo booth or night pass is already running."
        if self.arbiter.holders:
            return "A live lease holds the GPU."
        if self.scheduler.presence.offline_seconds() < OFFLINE_SECONDS:
            return "The owner must have been offline for at least ten minutes."
        if self.scheduler.running is not None or self.scheduler.due() is not None:
            return "The night is running or due and takes priority."
        if self._cooling():
            return "The photo booth is cooling down after a failure."
        return None

    def _cooling(self):
        until = self.app.store.get_meta(COOLDOWN)
        try:
            return until is not None and self.app.clock.now() < float(until)
        except ValueError:
            return False

    def _cool(self, ctx, code):
        store = ctx.store
        try:
            streak = int(store.get_meta(STREAK) or 0) + 1
        except ValueError:
            streak = 1
        seconds = min(COOLDOWN_FIRST * 2 ** (streak - 1), COOLDOWN_MAX)
        with store.transaction():
            store.set_meta(STREAK, str(streak))
            store.set_meta(COOLDOWN, str(ctx.clock.now() + seconds))
        log("selfies_booth_cooldown", level=logging.WARNING, code=code, minutes=seconds // 60,
            failures=streak)

    def _reset_cooldown(self, ctx):
        if ctx.store.get_meta(STREAK) or ctx.store.get_meta(COOLDOWN):
            with ctx.store.transaction():
                ctx.store.set_meta(STREAK, "0")
                ctx.store.set_meta(COOLDOWN, "0")

    def _context(self):
        now = self.app.clock.now()
        day = self.app.clock.local().date().isoformat()
        # commit_unit resets the matching night_run's stall counter. A distinct key also
        # keeps these records out of nightly CLI/audit reports, which join by night date.
        ctx = self.app.night_runner.make_ctx(f"booth:{day}", (now, now))
        policy = RenderPolicy(stage=STAGE, cap=self.cfg.booth_sets_per_day,
                              requested_before=now - self.cfg.booth_wait_minutes * 60,
                              day=day, record_failures=True)
        return ctx, policy

    async def tick(self):
        if self.running or not self._eligible():
            return
        await self.request(quiet=True)

    async def request(self, *, quiet=False):
        """Return this pass's outcome task, using the timer's wait, cap, lease and priority rules."""
        if problem := self.problem():
            raise ValueError(problem)
        ctx, policy = self._context()
        queue(ctx, policy)  # timestamps first sets without touching the night's receipts
        if not todo(ctx, policy):
            if quiet:
                return None
            raise ValueError("No eligible photo sets are due within today's allowance.")
        self._stopping = False
        self._task = asyncio.create_task(self._run(ctx, policy), name="selfies-booth")
        self._task.add_done_callback(self._finished)
        return self._task

    @staticmethod
    def _finished(task):
        if not task.cancelled() and (error := task.exception()) is not None:
            log("selfies_booth_failed", level=logging.WARNING, error=type(error).__name__)

    async def _run(self, ctx, policy):
        # A night or a lease may have arrived between tick and this task's first instruction.
        if not self._eligible():
            return "preempted"
        async with self._exclusive:
            started, began = ctx.clock.now(), time.monotonic()
            before = done_sets(ctx, policy)
            outcome, calls, code = "failed", 0, None
            tally = Counter()
            w = self.worker()
            original_render = w.render

            async def counted(*args, **kwargs):
                nonlocal calls
                calls += 1
                return await original_render(*args, **kwargs)

            w.render = counted
            original_start = w.start

            async def noting_start(*args, **kwargs):
                nonlocal code
                try:
                    return await original_start(*args, **kwargs)
                except Exception as e:
                    code = getattr(e, "reason", None) or type(e).__name__
                    raise

            w.start = noting_start
            try:
                try:
                    await self.arbiter.make_room()
                    await render(ctx, self.cfg, w, policy, tally=tally)
                    outcome = "partial" if ctx.failures else "done"
                finally:
                    await to_the_end(w.stop())
                # Normal completion only. A live lease now owns the next cold preload.
                with suppress(LiveLeaseHeld):
                    await self.arbiter.make_room()
            except asyncio.CancelledError:
                outcome = "preempted"
                raise
            except (LiveLeaseHeld, GpuPaused):  # a lease, or the owner's GPU pause
                outcome = "preempted"
            except Exception as e:
                code = code or type(e).__name__
                log("selfies_booth_failed", level=logging.WARNING, error=type(e).__name__)
            finally:
                seconds = round(time.monotonic() - began, 3)
                attempts = done_sets(ctx, policy) - before
                # A cancelled pass may have already published some sets before its return.
                sets = tally["ready"]
                if outcome != "preempted":
                    if sets:
                        self._reset_cooldown(ctx)
                    elif outcome == "failed" or (ctx.failures and not tally["refused"]):
                        self._cool(ctx, code or "no_set")
                with ctx.store.transaction():
                    # Unlike a resumed night, every booth pass (even one stopped before its
                    # first request) advances its record. Calls are actual render attempts.
                    ctx.store.conn.execute("INSERT INTO night_stage(night,stage,started,finished,"
                        "calls,call_s) VALUES(?,?,?,?,?,?) ON CONFLICT(night,stage) DO UPDATE SET "
                        "started=min(started,excluded.started),finished=max(finished,excluded.finished),"
                        "calls=calls+excluded.calls,call_s=call_s+excluded.call_s",
                        (ctx.night, STAGE, started, ctx.clock.now(), calls, seconds))
                    ctx.store.set_meta(LAST, json.dumps(dict(day=policy.day, started=started,
                        finished=ctx.clock.now(), sets=sets, attempts=attempts, seconds=seconds,
                        outcome=outcome)))
                log("selfies_booth_done", sets=sets, seconds=seconds, outcome=outcome)
            return outcome

    async def run_forever(self):
        try:
            while True:
                try:
                    await self.tick()
                except Exception as e:
                    log("selfies_booth_tick_failed", level=logging.WARNING,
                        error=type(e).__name__)
                await self.app.clock.sleep(TICK_SECONDS)
        finally:
            await self.cancel()
