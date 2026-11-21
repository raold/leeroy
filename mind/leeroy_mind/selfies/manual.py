"""His Render selfies button (the console's top bar, console/RENDER.md): the render backlog now.

What it renders, in this order: every queued profile set that can render (first sets, then
"4 more"), then, with [selfies] snaps, every planned situational and guild photo that can render
now. It runs the night's own renderers (render.render for the sets, snapstage._render for the
photos), so prompts, held-set rules, the NSFW gate, seeds, retries and the publish transactions
are theirs, unchanged. Photo requests are not part of it: they render on his ask in their own
bounded borrow (photos/requests.py) or send a ready-made photo.

What it bypasses: only the booth's scheduling limits (booth_wait_minutes, booth_sets_per_day,
the failure cooldown, the ten-minute offline wait) and the night's per-night caps. It needs
[selfies] enabled, never booth. Held sets stay held and queued.

Receipts: units go under night "manual:<local date>" and the stages STAGE and SNAPS_STAGE, with a
night_stage row of its own and the meta LAST, so it never resets a night's stall counter, never
spends the booth's allowance and never reads as nightly work. A set the photo checks refused in a
run is skipped by later runs that day (the same seeds would be refused again; the night, under
its own key, tries new ones); one a worker error ended (a timeout, contention, an OOM) is not.

The GPU: it refuses while he is in WoW or his presence is unknown, an offline raid, the voice
service or another job holds a lease, Free GPU is paused, a night or a booth pass runs or winds
down, a photo request renders, answers to him on Discord are being written or fall due within the
run, a run would eat into the night's quiet window, or another run is on. Admission is one step
on the loop: the checks, the release of the server and Discord presence leases (Free GPU lets
them go too) and the task's creation. From then on any lease holder at all is a preemption: the
run's first step finds it (his post, an owed answer's rescue, a carried whisper or a group DM
tick taken between the click and that step), and later every live lease preempts it as it
preempts the booth, through the arbiter's preempt, chained after the night's and the booth's;
the worker's whole process tree ends before the hand-over preloads, and that hand-over reloads
the live model whole (make_room marked the GPU shared). Free GPU stops it before unloading. While
it runs no night is due (NightScheduler.image_work), the booth does not start, a photo request's
borrow is busy and the server lease stays released (its end ticks that lease again). A cancel
path never calls the arbiter: hand_over holds its lock while it awaits the preempt.

Resuming (2026-11-21): a run a lease preempted (his login, the Discord desk, an offline raid's
start, the voice service, another job) resumes by itself, the same day, through request() and
its admission, once RESUME_AFTER_S have passed since it stopped and nothing holds the GPU;
run_forever checks every RESUME_TICK_S. Sets already ready stay ready: a resumed run renders
what is still queued. His Stop, Free GPU and a shutdown never resume, nor does a pause or an
empty backlog met while waiting; at most MAX_RESUMES a local day.

Logs: selfies_manual_render_started total= kinds=, selfies_manual_render_done done= failed= held=
seconds= outcome= by=, selfies_manual_render_resumed tries=, selfies_manual_resume_dropped
reason=; codes, ids and counts only, never a prompt, a scene or a name."""
import asyncio
import json
import logging
import math
import statistics
import time
from collections import Counter
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import datetime

from leeroy_mind.llm.client import GpuPaused
from leeroy_mind.logs import log
from leeroy_mind.nightly.gpu import LiveLeaseHeld

from . import snapstage
from .config import worker_command, worker_env
from .render import (SET_SIZE, RenderPolicy, _held, _set_held, _stored, allows, queue, render,
                     settle_sets)
from .store import needing_first, queued_sets
from .worker import SelfieWorker, to_the_end

STAGE = "selfies_manual"
SNAPS_STAGE = "snaps_manual"
LAST = "selfies:manual:last"
ALL = 1 << 30  # no per-run cap: the whole backlog
POLICY = RenderPolicy(stage=STAGE, cap=ALL, requested_before=math.inf, day=None,
                      first_first=True, record_refusals=True)
STOP_WAIT_S = 60.0  # render_stop's wait for the worker tree, before it answers anyway
COUNT_S = 30.0  # the idle backlog count is read again after this long
MEASURED = 10  # the newest records an estimate reads
MIN_IMAGE_S = 1.0  # no SDXL image comes back faster: a pass measuring less only failed fast
ANSWER_HORIZON_S = 1800.0  # owed answers due this soon hold a start whose length is unknown
UNIT_KEYS = ("ready", "refused", "failed", "skipped", "expired", "held", "measured")
RESUME_TICK_S = 60.0  # run_forever's check for a run to resume
RESUME_AFTER_S = 120.0  # a resume waits this long after its preemption: an offline raid's start
                        # refused as driver_pending tries again after 60 s
MAX_RESUMES = 6  # automatic resumes a local day; his Render click is never counted
NO_RESUME = frozenset({"owner", "gpu_pause", "shutdown"})  # stops he asked for, or the app's

OFF = "Selfies are off ([selfies] enabled in mind.toml)."
IN_GAME = "Log out of WoW first: photos render only while you are out of the game."
UNKNOWN = "Can't tell whether you're in WoW yet; try again in a moment."
NOTHING = "Nothing is queued to render."


def night_key(day: str) -> str:
    return f"manual:{day}"


@dataclass(frozen=True)
class Plan:
    """What a run renders now: first and "4 more" sets, situational/guild photos (`rows`), the
    held sets that stay queued, the queued sets the photo checks refused in an earlier run today
    (they wait for the night), and the images they make (four a set, one a situation, a guild
    photo one a member)."""
    first: int = 0
    more: int = 0
    held: int = 0
    images: int = 0
    rows: tuple = ()
    refused: int = 0

    @property
    def snaps(self) -> int:
        return len(self.rows)

    @property
    def total(self) -> int:
        return self.first + self.more + self.snaps

    def kinds(self) -> dict:
        return {"first": self.first, "more": self.more, "snaps": self.snaps}


def plan(ctx, cfg, policy: RenderPolicy = POLICY, *, queued: bool) -> Plan:
    """The run's work on `ctx`: the queued sets todo() would give (not done in today's runs,
    not held), and the photos snapstage would render now. `queued` says queue() already ran on
    ctx; without it the members queue() would give a first set count as first sets too."""
    first = more = held = refused = 0
    for s in queued_sets(ctx.store):
        if not allows(ctx, s.guid):
            continue
        if ctx.done(policy.stage, f"{s.guid}:{s.set_no}"):
            refused += 1
        elif _set_held(ctx, _stored(ctx, s.guid), s.guid, s.set_no) is not None:
            held += 1
        elif s.reason == "first":
            first += 1
        else:
            more += 1
    if not queued:
        members = [g for g in ctx.members(include_away=True)
                   if _held(ctx, _stored(ctx, g)) is None]
        first += sum(1 for g in needing_first(ctx.store, members) if allows(ctx, g))
    rows, images = [], SET_SIZE * (first + more)
    if cfg.snaps:
        for row in snapstage._todo(ctx):
            try:
                ok = snapstage._renders(ctx, row)
            except Exception:
                ok = True  # as wants_gpu: the run fails that one unit and goes on
            if ok:
                rows.append(row)
                images += max(1, len(row.members)) if row.kind == "guild" else 1
    return Plan(first, more, held, images, tuple(rows), refused)


def per_image_s(conn) -> float | None:
    """Seconds an image, worker start included, over the newest measured passes: the booth's
    and this button's night_stage rows (calls: images rendered, call_s: the pass's seconds), else
    the night's selfies stage (its time over four images a unit). A pass faster than MIN_IMAGE_S
    an image only failed fast and says nothing. None with nothing measured."""
    rows = conn.execute("SELECT calls, call_s FROM night_stage WHERE stage IN (?, ?) AND calls > 0 "
                        "ORDER BY finished DESC LIMIT ?", ("selfies_booth", STAGE, MEASURED))
    samples = [r[1] / r[0] for r in rows if r[1] / r[0] >= MIN_IMAGE_S]
    if not samples:
        rows = conn.execute(
            "SELECT s.started, s.finished, (SELECT count(*) FROM night_step n WHERE "
            "n.night = s.night AND n.stage = s.stage AND n.unit <> 'queue') FROM night_stage s "
            "WHERE s.stage = 'selfies' AND s.finished IS NOT NULL ORDER BY s.night DESC LIMIT ?",
            (MEASURED,))
        samples = [x for st, f, n in rows if n and (x := (f - st) / (SET_SIZE * n)) >= MIN_IMAGE_S]
    return statistics.median(samples) if samples else None


def estimate(conn, p: Plan) -> int | None:
    """Rough seconds the run takes from earlier renders; None (no estimate yet) with nothing
    measured or nothing to render."""
    per = per_image_s(conn)
    return None if per is None or not p.images else max(1, round(p.images * per))


class _Tally(Counter):
    """A render's Counter that says when it moved (the run's progress)."""

    def __init__(self, moved):
        self._moved = moved
        super().__init__()

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        self._moved()


def _units(*tallies) -> int:
    return sum(t[k] for t in tallies for k in UNIT_KEYS)


def duration_words(seconds: float) -> str:
    minutes = max(1, round(seconds / 60))
    return f"{minutes} minute{'s' if minutes != 1 else ''}" if minutes < 90 else \
        f"{round(minutes / 60, 1):g} hours"


class ManualRender:
    """`app.selfie_manual` with [selfies] enabled (attach_selfies, after the booth). `worker` and
    `snap_worker` make the sets' worker and the photos' identity worker (tests pass fakes)."""

    def __init__(self, app, cfg, *, worker=None, snap_worker=None):
        self.app, self.cfg = app, cfg
        self.worker = worker or (lambda: SelfieWorker(
            worker_command(cfg, identity=cfg.identity_sets), env=worker_env(cfg)))
        self.snap_worker = snap_worker or (lambda: SelfieWorker(
            worker_command(cfg, identity=True), env=worker_env(cfg)))
        self._task: asyncio.Task | None = None
        self._stopping: str | None = None  # why the run was told to stop
        self._progress: dict | None = None
        self._tallies: tuple = ()
        self._calls = 0
        self._run_ctx = None  # the run's context, for a record its cancelled task never made
        self._began = 0.0
        self._recorded = True
        self._count: dict | None = None
        self._count_at = -math.inf
        self._count_warned = False
        self._lease_tasks: set[asyncio.Task] = set()
        self._resume: dict | None = None  # {"day", "after", "tries"}: a preempted run to resume
        self._resumes = ("", 0)  # (local day, automatic resumes that day)
        self._last = self._read_last()
        self._prior = app.arbiter._preempt  # the night's cancel_run, or the booth's preempt
        app.arbiter.set_preempt(self.preempt)
        app.night_scheduler.image_work = lambda: self.running
        app.register_shutdown_drain(self.close)

    # -- state -----------------------------------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def resume_pending(self) -> bool:
        return self._resume is not None

    def _read_last(self) -> dict | None:
        try:
            value = json.loads(self.app.store.get_meta(LAST) or "null")
        except (ValueError, TypeError):
            return None
        return value if isinstance(value, dict) else None

    def invalidate(self) -> None:
        """The backlog count is read again at the next snapshot (a photo change, a run's end)."""
        self._count_at = -math.inf

    def problem(self) -> tuple[str, str] | None:
        """(code, words) of why a run cannot start now, else None; what the backlog decides
        (the owed answers, the night's window) is late_problem's."""
        app, arbiter = self.app, self.app.arbiter
        if not self.cfg.enabled:
            return "off", OFF
        if getattr(app, "stopping", False):
            return "stopping", "The mind is stopping."
        if self.running:
            return "running", "A selfie render is already running."
        if getattr(arbiter, "paused", False):
            return "gpu_paused", "The GPU is paused. Resume the GPU first."
        holders = arbiter.holders
        if "human" in holders:
            return "in_game", IN_GAME
        try:
            # Known only from a hello on a live bridge, as Free GPU reads it.
            known = getattr(app, "hello", None) is not None and app.bridge.connected
            online = app.world.human_online, app.night_scheduler.presence.online
        except Exception:
            known = False
        if known and any(online):
            return "in_game", IN_GAME
        if not known or online[1] is None:
            return "presence_unknown", UNKNOWN
        if "offline_raid" in holders:
            return "offline_raid", "An offline raid holds the GPU. Wait for it to end (Raid tab)."
        if "voice_gpu" in holders:
            return "voice", "The voice service holds the GPU. Try again once it lets go."
        if arbiter.borrow_holder is not None or any(h.startswith("selfie_request")
                                                    for h in holders):
            return "request", "A photo request is rendering. Try again in a minute."
        if other := holders - {"server", "discord"}:
            return "lease", ("A live GPU job holds the GPU (" + ", ".join(sorted(other))
                             + "). Try again when it ends.")
        gate = getattr(arbiter, "live_admission", None)
        if gate is not None and gate.blocked:
            return "request", "A photo request's model reload is still pending. Try again in a minute."
        task = getattr(app.night_scheduler, "_task", None)
        if task is not None and not task.done():
            return "night", ("A night is running; its own photo stages render the queue. "
                             "Try again after it ends.")
        booth = getattr(app, "selfie_booth", None)
        if booth is not None and (booth.running or booth._exclusive.locked()):
            return "booth", "The photo booth is rendering. Try again when it finishes."
        return None

    def late_problem(self, seconds: int | None) -> tuple[str, str] | None:
        """(code, words) of why a run of about `seconds` (None: no estimate) cannot start now:
        answers to him on Discord being written or falling due within it, or the night's quiet
        window it would eat into."""
        if self._answering(ANSWER_HORIZON_S if seconds is None else seconds):
            return "answering", ("Members are still answering you on Discord, or owe you answers "
                                 "due while it would render. Try again once they have answered.")
        return self._window(seconds)

    def _answering(self, within: float) -> bool:
        """Answers to him on Discord the engine is writing, or that she put off to her own time
        and that fall due within `within` seconds: a run would hold those back (no live lease
        is left for them), so it waits for them instead."""
        from leeroy_mind.talk.owed import answers_in_flight
        engine = getattr(self.app, "engine", None)
        talk = getattr(self.app, "discord_talk", None)
        try:
            if engine is not None and answers_in_flight(engine, discord=True) > 0:
                return True
            return talk is not None and talk.answers_due(within) > 0
        except Exception:
            return False

    def _window(self, seconds: int | None) -> tuple[str, str] | None:
        """With [server] on the night runs only in its quiet window, and a run holds it back:
        inside the window, while tonight's night has not finished, no run starts; outside it, a
        run whose estimate is longer than the time left before the window opens does not."""
        from leeroy_mind.nightly.scheduler import tonight
        from leeroy_mind.nightly.store import run_get
        from leeroy_mind.server.clock import in_window, window_bounds
        scheduler = self.app.night_scheduler
        quiet = getattr(scheduler.settings, "quiet", None)
        if quiet is None:
            return None
        local, now = self.app.clock.local(), self.app.clock.now()
        start, end = window_bounds(local, quiet, clock=self.app.clock)
        span = f"{quiet[0]:%H:%M}-{quiet[1]:%H:%M}"
        if in_window(local, quiet):
            run = run_get(self.app.store, tonight(local, scheduler.settings))
            over = run is not None and (run.status in ("done", "abandoned") or (
                run.status == "failed" and run.attempts >= scheduler.settings.max_attempts))
            if not over:
                return "night_window", (f"The night's window is open ({span}) and tonight's "
                                        "night has not finished: a render now would hold it back. "
                                        "Try after the night.")
            return None
        if seconds is not None and seconds > start - now:
            opens = datetime.fromtimestamp(start, local.tzinfo)
            return "night_window", (f"This render takes about {duration_words(seconds)}, longer "
                                    f"than the {duration_words(start - now)} left before the "
                                    f"night's window opens at {opens:%H:%M}. Try after the night.")
        return None

    def _fence(self) -> str | None:
        """Why the run's first step must not take the GPU after all: the pause, a night or a
        booth pass, or any lease holder at all (admission let the presence leases go, so one
        here was taken after the click: his post, an owed answer, a lease job). Its `by` code,
        else None."""
        arbiter = self.app.arbiter
        if getattr(arbiter, "paused", False):
            return "gpu_pause"
        if held := arbiter.holders:
            return self._reason(held)
        task = getattr(self.app.night_scheduler, "_task", None)
        if task is not None and not task.done():
            return "night"
        booth = getattr(self.app, "selfie_booth", None)
        if booth is not None and (booth.running or booth._exclusive.locked()):
            return "booth"
        return None

    def _reason(self, holders) -> str:
        """The `by` code of what stopped a run: the GPU pause, his login, the Discord lease as
        whatever last took it (his post, an answer owed to him, a group DM's answer), an offline
        raid, the voice service, else another lease."""
        arbiter = self.app.arbiter
        if getattr(arbiter, "paused", False) or "gpu_pause" in holders:
            return "gpu_pause"
        if "human" in holders:
            return "login"
        if "discord" in holders:
            why = getattr(getattr(self.app, "discord_talk", None), "lease_reason", "post")
            return {"answer": "discord_answer", "group": "discord_group"}.get(why, "discord")
        for holder, code in (("offline_raid", "offline_raid"), ("voice_gpu", "voice")):
            if holder in holders:
                return code
        return "lease"

    def _peek(self):
        """A read-only context for the idle count: the night's settings and the app's personas,
        without reloading mind.toml (make_ctx does, for a run)."""
        from leeroy_mind.human import human_identity
        from leeroy_mind.nightly.runner import NightContext
        app = self.app
        now = app.clock.now()
        settings = replace(app.night_scheduler.settings, selfie_per_night=ALL, snaps_per_night=ALL)
        ctx = NightContext(store=app.store, night=night_key(app.clock.local().date().isoformat()),
                           window=(now, now), settings=settings, reasoner=None, embedder=None,
                           writer=None, arbiter=app.arbiter, clock=app.clock,
                           ident=human_identity(app.cfg, app.store, app.human_guid),
                           personas=app.personas)
        ctx.snaps_stage = SNAPS_STAGE
        return ctx

    def _ctx(self, night: str):
        now = self.app.clock.now()
        ctx = self.app.night_runner.make_ctx(night, (now, now))
        ctx.settings = replace(ctx.settings, selfie_per_night=ALL, snaps_per_night=ALL)
        ctx.snaps_stage = SNAPS_STAGE
        return ctx

    def backlog(self) -> dict | None:
        """What a run would render now (kinds, held sets, sets refused today, images, a rough
        estimate in seconds or None), read at most every COUNT_S; None when it could not be
        read."""
        now = time.monotonic()
        if now - self._count_at < COUNT_S:
            return self._count
        self._count_at = now
        try:
            p = plan(self._peek(), self.cfg, queued=False)
            self._count = {**p.kinds(), "held": p.held, "refused": p.refused, "total": p.total,
                           "images": p.images, "estimate_s": estimate(self.app.store.conn, p)}
            self._count_warned = False
        except Exception as e:
            self._count = None
            if not self._count_warned:
                self._count_warned = True
                log("selfies_manual_count_failed", level=logging.WARNING, error=type(e).__name__)
        return self._count

    def snapshot(self) -> dict:
        """The control status's `selfie_render`: running with its progress, or the backlog and
        why a run cannot start now; the last run's record either way."""
        out = {"selfies": True, "snaps": bool(self.cfg.snaps), "running": self.running,
               "stopping": self.running and self._stopping is not None, "progress": None,
               "backlog": None, "refusal": None, "code": None, "last": self._last,
               "resume": self.resume_pending}
        if self.running:
            out["progress"] = dict(self._progress or {})
            return out
        found = self.problem()
        backlog = out["backlog"] = self.backlog() if self.cfg.enabled else None
        if found is None and backlog is not None:
            if backlog["total"] == 0:
                found = ("nothing", self._nothing(backlog["refused"]))
            else:
                found = self.late_problem(backlog["estimate_s"])
        if found is not None:
            out["code"], out["refusal"] = found
        return out

    @staticmethod
    def _nothing(refused: int) -> str:
        if not refused:
            return NOTHING
        return (f"{NOTHING} {refused} set{'s' if refused != 1 else ''} the photo check refused "
                "today wait for the night, which renders them with new seeds.")

    # -- the control op ---------------------------------------------------------------------------

    async def action(self, name: str) -> dict:
        if name == "render_stop":
            return await self.stop_now()
        return await self.request()

    async def request(self) -> dict:
        """Start a run of the whole backlog, or refuse in words. Admission is one step on the
        loop (no await suspends it): the checks, the first sets' queueing, the release of the
        presence leases and the task's creation, so any lease taken after it is the run's
        first step's to find (_fence)."""
        found = self.problem()
        if found is not None:
            return self._refused(found)
        try:
            ctx = self._ctx(night_key(self.app.clock.local().date().isoformat()))
            queue(ctx, POLICY)  # the first sets, as the night and the booth queue them
            p = plan(ctx, self.cfg, queued=True)
            seconds = estimate(ctx.store.conn, p)
        except Exception as e:  # its text can carry stored data: the class only
            log("selfies_manual_render_refused", level=logging.WARNING, reason="plan_failed",
                error=type(e).__name__)
            return {"ok": False, "error": "plan_failed", "render": self.snapshot(),
                    "message": f"The render could not be planned ({type(e).__name__}). Nothing started."}
        if p.total == 0:
            self.invalidate()
            return self._refused(("nothing", self._nothing(p.refused)))
        found = self.late_problem(seconds)
        if found is not None:
            return self._refused(found)
        # The presence leases go here, not in the run: a post that lands before its first step
        # keeps its lease, and that step finds it (_fence). Neither release suspends.
        await self._release_presence()
        self._resume = None  # this run renders what a preempted one left
        self._stopping, self._calls, self._recorded = None, 0, False
        self._run_ctx, self._began = ctx, time.monotonic()
        self._progress = {"done": 0, "total": p.total, "started": ctx.clock.now(),
                          "phase": "sets" if p.first + p.more else "snaps",
                          "estimate_s": seconds, **p.kinds()}
        self._tallies = ()
        self._task = asyncio.create_task(self._run(ctx, p), name="selfies-manual")
        self._task.add_done_callback(self._finished)
        log("selfies_manual_render_started", total=p.total, kinds=p.kinds())
        return {"ok": True, "render": self.snapshot()}

    def _refused(self, found: tuple[str, str]) -> dict:
        log("selfies_manual_render_refused", reason=found[0])
        return {"ok": False, "error": found[0], "message": found[1], "render": self.snapshot()}

    async def stop_now(self) -> dict:
        """His Stop: the run is told to stop and its worker tree ends; answered once it has, or
        after STOP_WAIT_S with `pending`."""
        task = self._task
        if not self.running:
            if self._resume is not None:
                self._drop_resume("owner")
                return {"ok": True, "render": self.snapshot(),
                        "message": "The stopped render won't resume."}
            return {"ok": False, "error": "not_running", "message": "No selfie render is running.",
                    "render": self.snapshot()}
        self.stop("owner")
        done, _ = await asyncio.wait({task}, timeout=STOP_WAIT_S)
        if not done:
            return {"ok": True, "pending": True, "render": self.snapshot(),
                    "message": "Stopping: the photo worker is still shutting down."}
        return {"ok": True, "render": self.snapshot()}

    # -- stopping ---------------------------------------------------------------------------------

    def stop(self, reason: str) -> None:
        """Tell a running run to stop, with no await (Free GPU, his Stop); the first reason
        stays. With no run, the same reasons drop a resume that waits."""
        if self.running and self._stopping is None:
            self._stopping = reason
            self._task.cancel()
        elif not self.running and reason in NO_RESUME:
            self._drop_resume(reason)

    async def cancel(self, reason: str) -> None:
        task = self._task
        if self.running:
            self.stop(reason)
            # A cancelled hand-over must not cancel or abandon the worker's cleanup.
            await asyncio.wait({task})

    async def preempt(self) -> None:
        """The arbiter's preempt: the night's (and the booth's) first, then this run's."""
        if self._prior is not None:
            await self._prior()
        await self.cancel(self._reason(self.app.arbiter.holders))

    # -- resuming ---------------------------------------------------------------------------------

    def _today(self) -> str:
        return self.app.clock.local().date().isoformat()

    def _drop_resume(self, reason: str) -> None:
        if self._resume is not None:
            self._resume = None
            log("selfies_manual_resume_dropped", reason=reason)

    def _want_resume(self, rec: dict) -> None:
        """After a run ends: a lease's preemption is resumed later (tick); anything else ends any
        resume. The day's count of automatic resumes caps it."""
        if rec.get("outcome") != "preempted" or rec.get("by") in NO_RESUME or self._stopping in NO_RESUME:
            self._resume = None
            return
        day = self._today()
        tries = self._resumes[1] if self._resumes[0] == day else 0
        if tries >= MAX_RESUMES:
            self._drop_resume("capped")
            return
        self._resume = {"day": day, "after": self.app.clock.now() + RESUME_AFTER_S}

    async def tick(self) -> None:
        """Resume a preempted run when it is due and admission allows it (request() checks it
        all again); a pause, an empty backlog or a new day drops it, any other refusal waits."""
        resume = self._resume
        if resume is None or self.running or self.app.clock.now() < resume["after"]:
            return
        day = self._today()
        if resume["day"] != day:
            self._drop_resume("day")
            return
        found = self.problem()
        if found is not None:
            if found[0] in ("gpu_paused", "off"):
                self._drop_resume(found[0])
            return
        tries = (self._resumes[1] if self._resumes[0] == day else 0) + 1
        answer = await self.request()
        if answer.get("ok"):
            self._resumes = (day, tries)
            log("selfies_manual_render_resumed", tries=tries)
        elif answer.get("error") == "nothing":
            self._drop_resume("nothing")

    async def run_forever(self) -> None:
        """The app's background loop for resuming (registered with selfies on)."""
        while True:
            try:
                await self.tick()
            except Exception as e:
                log("selfies_manual_resume_failed", level=logging.WARNING, error=type(e).__name__)
            await self.app.clock.sleep(RESUME_TICK_S)

    async def close(self) -> None:
        """The app's shutdown drain: the run ends with its worker tree, and the lease tick."""
        await self.cancel("shutdown")
        tasks = [t for t in self._lease_tasks if not t.done()]
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # -- the run ---------------------------------------------------------------------------------

    def _moved(self) -> None:
        if self._progress is not None:
            done = _units(*self._tallies)
            self._progress["done"] = done
            self._progress["total"] = max(self._progress["total"], done)

    def _counting(self, worker):
        """Count the images that came back (an estimate's seconds an image), not the attempts
        a failure ended."""
        original = worker.render

        async def counted(*args, **kwargs):
            got = await original(*args, **kwargs)
            self._calls += 1
            return got
        worker.render = counted
        return worker

    async def _release_presence(self) -> None:
        """The server and Discord presence leases go, as Free GPU lets them go: the server's
        stays released while the run runs (ServerLease.tick); a Discord lease taken again (his
        next post, an answer owed to him) preempts the run."""
        server = getattr(self.app, "server", None)
        if server is not None:
            await server.lease._release("selfie_render")
        discord = getattr(self.app, "discord_talk", None)
        if discord is not None:
            await discord._release()

    async def _run(self, ctx, p: Plan) -> str:
        began, started = self._began, ctx.clock.now()
        sets, snaps, published = _Tally(self._moved), _Tally(self._moved), _Tally(self._moved)
        self._tallies = (sets, snaps, published)
        outcome, by = "failed", None
        arbiter = self.app.arbiter
        try:
            try:
                if (by := self._fence()) is not None:  # arrived after the click
                    outcome = "preempted"
                    return outcome
                await arbiter.make_room()
                if p.first + p.more:
                    w = self._counting(self.worker())
                    try:
                        await render(ctx, self.cfg, w, POLICY, tally=sets)
                    finally:
                        # Never call the arbiter from cancellation cleanup: hand_over holds
                        # its lock. The whole tree ends before the live preload can start.
                        await to_the_end(w.stop())
                    await settle_sets(ctx, self.cfg)  # [selfies] critic, the worker gone
                if p.rows:
                    self._progress["phase"] = "snaps"
                    await arbiter.make_room()  # the night's room for its next image stage
                    w = self._counting(self.snap_worker())
                    pending = []
                    try:
                        pending = await snapstage._render(ctx, self.cfg, w, list(p.rows),
                                                          tally=snaps)
                    finally:
                        await to_the_end(w.stop())
                    if pending:  # [selfies] critic: best-of-N, the worker's VRAM free now
                        # Measured photos are not rendered ones until published: their
                        # outcomes replace the measurement in the record.
                        snaps["measured"] -= len(pending)
                        published.update(await snapstage._publish(ctx, self.cfg, pending))
                        self._moved()
                # Failed: the worker never came up, or every unit broke. A set the NSFW gate
                # refused, or a photo that expired or waits, is a run that did its work.
                landed = sum(t[k] for t in (sets, snaps, published)
                             for k in ("ready", "refused", "skipped", "expired", "held"))
                outcome = "failed" if ctx.failures and not landed else "done"
                # Normal completion only: a chat model loaded beside the worker loads whole
                # again; a lease taken meanwhile owns the next cold preload.
                with suppress(LiveLeaseHeld):
                    await arbiter.make_room()
            except (LiveLeaseHeld, GpuPaused) as e:  # a lease, or the owner's GPU pause
                outcome, by = "preempted", self._stopping or self._reason(
                    getattr(e, "holders", frozenset()))
        except asyncio.CancelledError:
            outcome = "stopped" if self._stopping == "owner" else "preempted"
            by = self._stopping or "lease"
            raise
        except Exception as e:
            log("selfies_manual_render_failed", level=logging.WARNING, error=type(e).__name__)
        finally:
            self._record(ctx, started, began, outcome, by, sets, snaps, published)
        return outcome

    def _record(self, ctx, started, began, outcome, by, sets, snaps, published) -> None:
        self._recorded = True
        seconds = round(time.monotonic() - began, 3)
        failed = sets["refused"] + sets["failed"] + snaps["failed"] + published["failed"]
        held = sets["skipped"] + snaps["held"] + published["held"]
        progress = self._progress or {}
        rec = {"day": ctx.night.removeprefix("manual:"), "started": started,
               "finished": ctx.clock.now(), "outcome": outcome, "by": by,
               "total": progress.get("total", 0), "done": _units(sets, snaps, published),
               "sets": sets["ready"], "snaps": snaps["ready"] + published["ready"],
               "refused": sets["refused"], "failed": failed, "held": held,
               "expired": snaps["expired"] + published["expired"], "seconds": seconds}
        try:
            with ctx.store.transaction():
                # Calls are images that came back, so the next estimate reads seconds an image.
                ctx.store.conn.execute(
                    "INSERT INTO night_stage(night,stage,started,finished,calls,call_s) "
                    "VALUES(?,?,?,?,?,?) ON CONFLICT(night,stage) DO UPDATE SET "
                    "started=min(started,excluded.started),finished=max(finished,excluded.finished),"
                    "calls=calls+excluded.calls,call_s=call_s+excluded.call_s",
                    (ctx.night, STAGE, started, rec["finished"], self._calls, seconds))
                ctx.store.set_meta(LAST, json.dumps(rec))
        except Exception as e:
            log("selfies_manual_record_failed", level=logging.WARNING, error=type(e).__name__)
        self._last = rec
        log("selfies_manual_render_done", done=rec["done"], failed=failed, held=held,
            seconds=seconds, outcome=outcome, by=by or "none")

    def _finished(self, task: asyncio.Task) -> None:
        self.invalidate()
        if not self._recorded and self._run_ctx is not None:
            # Cancelled before its first step (a lease, the pause or his Stop in the same loop
            # pass as the click): the coroutine never ran, so its record is made here.
            empty = Counter()
            self._record(self._run_ctx, (self._progress or {}).get("started",
                         self._run_ctx.clock.now()), self._began,
                         "stopped" if self._stopping == "owner" else "preempted",
                         self._stopping or "lease", empty, empty, empty)
        if not task.cancelled() and (error := task.exception()) is not None:
            log("selfies_manual_render_failed", level=logging.WARNING, error=type(error).__name__)
        self._want_resume(self._last or {})
        if self._stopping == "shutdown" or getattr(self.app, "stopping", False):
            return
        server = getattr(self.app, "server", None)
        if server is None or not getattr(server.config, "enabled", False):
            return
        lease = asyncio.get_running_loop().create_task(self._lease_back(server),
                                                       name="selfies-manual-lease")
        self._lease_tasks.add(lease)
        lease.add_done_callback(self._lease_tasks.discard)

    async def _lease_back(self, server) -> None:
        """The server lease is taken again at once (outside the quiet window), not at its next
        tick; its hand-over reloads the live model whole after the worker."""
        try:
            await server.lease.tick(self.app.clock.now())
        except Exception as e:
            log("selfies_manual_lease_failed", level=logging.WARNING, error=type(e).__name__)
