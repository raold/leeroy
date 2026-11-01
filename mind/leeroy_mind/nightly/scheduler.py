"""The night scheduler (P1c Task 4): it runs the night on the idle GPU once the human has been
offline for the grace period, and stops it the moment he is back.

on_event and on_connection run on P1b's one bridge dispatcher, so they stay short (app.py: "keep
them short, spawn anything long"). His login takes the arbiter's "human" lease and tells the run
to stop, inline and with no await; the GPU hand-back (GpuArbiter.hand_over: the preempt, which
waits for the run to end, then the reflection model's unload and the live model's preload) runs
as a tracked task of its own. His logout, and a dropped bridge, give the lease back inline. The
lease therefore follows the events in their order: a login and a logout in one burst leave
nobody holding it, and the hand-back then finds no lease and touches no model. (A spawned
acquire_live would take its lease only when its task first runs, after such a logout.)

due() names a night when all of these hold:
- nobody holds a live lease (he is offline and no P1d offline raid runs);
- he has been offline, or not known to be online, for grace_minutes;
- the night is the earliest of the last RESUME_NIGHTS (2) nights with an unfinished run
  (running or interrupted), else tonight (night_of the clock's local time). So a run his login
  interrupted resumes first, even when he next goes offline after 06:00 (controller ruling on
  review I1). An unfinished run older than that is marked abandoned at the next tick, logged
  and never resumed: what it finished stays committed, and its window counts as covered, so
  nothing is done twice;
- the night has no done run;
- a failed run of the night has used fewer than max_attempts attempts and finished more than
  retry_minutes ago. Only a night's first begin and a retry after a failure are attempts; a run
  that was only interrupted (his login, P1d's raid, the mind stopping) resumes as it stood, with
  no new attempt, so an interrupted night never costs its reflection (controller rulings on
  Task 4's concern 1);
- a night with no run yet would get a window (from the end of the last done or abandoned run's
  window to now) of min_window_hours or more; a shorter one waits until it is that long, and its
  events stay in that later window (review N8);
- the last done run finished min_gap_hours ago or more, so a run that ended past 06:00 never
  starts the next night at once (controller ruling I11).

tick() begins that night's run with run_begin (window_for's window, which a resumed or retried
run keeps) and runs run_night in a task. How the task ends:
- run_night returns: the run is done, and keeps the unit-failure summary the runner left in
  night_run.error (review I2);
- it is cancelled (his login, P1d's raid, the mind stopping): the run stays running, to resume
  with no back-off. A run_night that swallows its cancel and returns is never done; one whose
  cleanup raises after the cancel is logged and marked interrupted, which resumes the same way;
- it raises anything else: the run failed, and is tried again after retry_minutes.

run_night's cancel path must make no arbiter call: cancel_run is the arbiter's preempt and is
awaited while hand_over holds the arbiter's lock."""
import asyncio
import logging
from datetime import date, timedelta
from typing import Awaitable, Callable

from leeroy_mind.clock import Clock, night_of
from leeroy_mind.logs import log
from leeroy_mind.nightly.gpu import GpuArbiter
from leeroy_mind.nightly.presence import Presence
from leeroy_mind.nightly.settings import NightlySettings
from leeroy_mind.nightly.store import (UNFINISHED, last_finished, last_window_end, run_begin,
                                       run_finish, run_get, unfinished_nights)
from leeroy_mind.storage.store import Store

HUMAN = "human"  # his lease on the GPU arbiter
RESUME_NIGHTS = 2  # an unfinished run resumes within the last two nights, tonight's included

Window = tuple[float, float]


def _nights_back(night: str, n: int) -> str:
    return (date.fromisoformat(night) - timedelta(days=n)).isoformat()


def _cancelling() -> bool:
    """The current task has been told to stop (P1b's app asks the same way)."""
    task = asyncio.current_task()
    return task is not None and task.cancelling() > 0


def window_for(store: Store, night: str, now: float) -> Window:
    """The night's window: its run's when it has one, so a resumed or retried run covers what
    its first attempt did; else from the end of the last done or abandoned run's window (0.0
    when none) to now."""
    run = run_get(store, night)
    if run is not None:
        return run.window_start, run.window_end
    return last_window_end(store) or 0.0, now


class NightScheduler:
    """See the module docstring. `presence` is the human's Presence. `running` is the night
    whose run is under way, and None from the moment that run is told to stop."""

    def __init__(self, store: Store, settings: NightlySettings, arbiter: GpuArbiter,
                 run_night: Callable[[str, Window], Awaitable[None]], *, clock: Clock,
                 tick_seconds: float = 30):
        self.store = store
        self.settings = settings
        self.arbiter = arbiter
        self.run_night = run_night
        self.clock = clock
        self.tick_seconds = tick_seconds
        self.presence = Presence(clock)
        self._task: asyncio.Task | None = None  # the night's run, kept after it ends
        self._night: str | None = None
        self._stopping = False  # the run in _task has been told to stop
        self._handbacks: set[asyncio.Task] = set()
        arbiter.set_preempt(self.cancel_run)

    @property
    def running(self) -> str | None:
        if self._task is None or self._task.done() or self._stopping:
            return None
        return self._night

    # -- the bridge: inline, with no wait on the GPU ---------------------------------------------

    async def on_event(self, ev) -> None:
        """A hello, login or logout. His login takes the lease and stops the run at once, and
        the GPU hand-back runs as a task (handback() waits for it); his logout ends the lease."""
        change = self.presence.on_event(ev)
        if change == "login":
            first = self.arbiter.hold(HUMAN)
            self._stop_run()
            if first:
                self._spawn_handback()
        elif change == "logout":
            await self.arbiter.release_live(HUMAN)

    async def on_connection(self, up: bool) -> None:
        """A dropped bridge counts him offline as of now (Presence), so his lease ends."""
        if self.presence.on_connection(up) == "logout":
            await self.arbiter.release_live(HUMAN)

    def _spawn_handback(self) -> None:
        task = asyncio.get_running_loop().create_task(self.arbiter.hand_over(),
                                                      name="nightly-handback")
        self._handbacks.add(task)
        task.add_done_callback(self._handback_done)

    def _handback_done(self, task: asyncio.Task) -> None:
        """A failed hand-back keeps his lease (the arbiter's rule), so no night starts; live
        talk never asks the arbiter, and its first request loads the live model. Logged here."""
        self._handbacks.discard(task)
        if not task.cancelled() and (e := task.exception()) is not None:
            log("nightly_handback_failed", level=logging.WARNING, error=repr(e))

    async def handback(self) -> None:
        """Wait for the hand-backs his logins started."""
        while self._handbacks:
            await asyncio.wait(set(self._handbacks))

    # -- the night's run ---------------------------------------------------------------------------

    def due(self) -> str | None:
        """The night to run when the module docstring's conditions all hold, else None. It does
        not ask whether a run is under way; tick() does."""
        s = self.settings
        if not self.arbiter.reflection_allowed():
            return None
        if self.presence.offline_seconds() < s.grace_minutes * 60:
            return None
        now = self.clock.now()
        tonight = night_of(self.clock.local())
        oldest = _nights_back(tonight, RESUME_NIGHTS - 1)
        night = next((n for n in unfinished_nights(self.store) if oldest <= n <= tonight), tonight)
        run = run_get(self.store, night)
        if run is None:
            if now - (last_window_end(self.store) or 0.0) < s.min_window_hours * 3600:
                return None
        elif run.status == "done" or (run.status == "failed" and run.attempts >= s.max_attempts):
            return None
        elif run.status == "failed" and (run.finished is None
                                         or now - run.finished <= s.retry_minutes * 60):
            return None
        finished = last_finished(self.store)
        if finished is not None and now - finished < s.min_gap_hours * 3600:
            return None
        return night

    async def tick(self) -> None:
        """Begin the due night's run. A run under way is left alone. One told to stop is given
        up to tick_seconds to wind down first, so a login and logout between two ticks cost the
        night no more than the grace period."""
        task = self._task
        if task is not None and not task.done():
            if not self._stopping:
                return
            await asyncio.wait({task}, timeout=self.tick_seconds)
            if self._task is not None and not self._task.done():
                return
        self._abandon_stale()
        night = self.due()
        if night is not None:
            self._start(night)

    def _abandon_stale(self) -> None:
        """Every unfinished run older than the last RESUME_NIGHTS nights is abandoned."""
        oldest = _nights_back(night_of(self.clock.local()), RESUME_NIGHTS - 1)
        for night in unfinished_nights(self.store):
            if night < oldest:
                self._mark(night, "abandoned")
                log("nightly_abandoned", level=logging.WARNING, night=night)

    def _mark(self, night: str, status: str) -> None:
        """Set the run's status and finished time, and keep night_run.error: the unit-failure
        summary the runner writes there (Task 5), which Task 23's night_health reads."""
        with self.store.transaction():
            kept = run_get(self.store, night)
            run_finish(self.store, night, now=self.clock.now(), status=status,
                       error=None if kept is None else kept.error)

    def _start(self, night: str) -> None:
        now = self.clock.now()
        prior = run_get(self.store, night)
        resumed = prior is not None and prior.status in UNFINISHED
        # Its first begin, or a retry after a failure, is one more attempt; a resume is none.
        run = run_begin(self.store, night, *window_for(self.store, night, now), now=now,
                        count_attempt=not resumed)
        window = (run.window_start, run.window_end)
        log("nightly_started", night=night, attempt=run.attempts, resumed=resumed,
            window_start=window[0], window_end=window[1])
        self._night, self._stopping = night, False
        self._task = asyncio.get_running_loop().create_task(
            self._run(night, window, run.attempts), name=f"nightly-{night}")
        self._task.add_done_callback(self._run_done)

    async def _run(self, night: str, window: Window, attempt: int) -> None:
        try:
            await self.run_night(night, window)
        except asyncio.CancelledError:
            log("nightly_paused", night=night, attempt=attempt)  # still running: resumable
            raise
        except Exception as e:
            if _cancelling():  # its cleanup raised on the way out of a cancel: no failure
                log("nightly_cleanup_failed", level=logging.WARNING, night=night, error=repr(e))
                self._mark(night, "interrupted")
                raise asyncio.CancelledError from e
            run_finish(self.store, night, now=self.clock.now(), status="failed", error=repr(e))
            log("nightly_failed", level=logging.WARNING, night=night, attempt=attempt,
                error=repr(e))
            return
        if _cancelling():  # it swallowed its cancel, so units may be left: not done
            log("nightly_cancel_swallowed", level=logging.WARNING, night=night, attempt=attempt)
            raise asyncio.CancelledError
        self._mark(night, "done")
        log("nightly_done", night=night, attempt=attempt)

    @staticmethod
    def _run_done(task: asyncio.Task) -> None:
        """What _run itself could not record (run_finish on a store that is gone) is logged."""
        if not task.cancelled() and (e := task.exception()) is not None:
            log("nightly_run_crashed", level=logging.ERROR, task=task.get_name(), error=repr(e))

    def _stop_run(self) -> asyncio.Task | None:
        """Tell the run to stop, with no await: the task while it winds down, else None."""
        task = self._task
        if task is None or task.done():
            return None
        if not self._stopping:
            self._stopping = True
            task.cancel()
        return task

    async def cancel_run(self) -> None:
        """Stop the run and wait for it to end (the arbiter's preempt). The run stays running,
        to resume later. Only the run's own cancel is absorbed: a cancel of this call (the
        preempt's deadline, a hand-back cancelled at shutdown) reaches its caller and leaves the
        run to finish winding down by itself."""
        task = self._stop_run()
        if task is not None:
            await asyncio.wait({task})

    async def wait_idle(self) -> None:
        """Wait until neither a run nor a hand-back is in flight."""
        while busy := [t for t in (self._task, *self._handbacks)
                       if t is not None and not t.done()]:
            await asyncio.wait(busy)

    async def run_forever(self) -> None:
        """tick() every tick_seconds on the mind clock (P1b's register_background). A tick that
        raises (a locked database) is logged and the next one tries again. When the mind stops,
        the run is stopped, left running to resume at the next start, and any hand-back still
        in flight is cancelled."""
        try:
            while True:
                try:
                    await self.tick()
                except Exception as e:
                    log("nightly_tick_failed", level=logging.WARNING, error=repr(e))
                await self.clock.sleep(self.tick_seconds)
        finally:
            handbacks = list(self._handbacks)
            for t in handbacks:
                t.cancel()
            pending = [t for t in (self._stop_run(), *handbacks) if t is not None]
            if pending:
                await asyncio.wait(pending)
