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
spends the booth's allowance and never reads as nightly work. A set refused or failed in a run is
skipped by later runs that day (the same seeds would render the same refusal): the night, under
its own key, tries it again.

The GPU: it refuses while he is in WoW or his presence is unknown, an offline raid, the voice
service or another job holds a lease, Free GPU is paused, a night or a booth pass runs or winds
down, a photo request renders, members are still answering him on Discord, or another run is on.
It releases the server and Discord presence leases (Free GPU does too), makes room as an image
stage does and starts the worker. While it runs no night is due (NightScheduler.image_work), the
booth does not start, a photo request's borrow is busy and the server lease stays released (its
end ticks that lease again). Every live lease preempts it as it preempts the booth: the arbiter's
preempt, chained after the night's and the booth's, cancels the run, whose worker's whole
process tree ends before the hand-over preloads; that hand-over reloads the live model whole
(make_room marked the GPU shared). Free GPU stops it before unloading. A cancel path never calls
the arbiter: hand_over holds its lock while it awaits the preempt.

Logs: selfies_manual_render_started total= kinds=, selfies_manual_render_done done= failed= held=
seconds= outcome= by=; codes, ids and counts only, never a prompt, a scene or a name."""
import asyncio
import json
import logging
import math
import statistics
import time
from collections import Counter
from contextlib import suppress
from dataclasses import dataclass, replace

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
                      record_failures=True, first_first=True)
STOP_WAIT_S = 60.0  # render_stop's wait for the worker tree, before it answers anyway
COUNT_S = 30.0  # the idle backlog count is read again after this long
START_S = 45.0  # one worker's start, for an estimate with nothing measured
IMAGE_S = 12.0  # one image, for an estimate with nothing measured
MEASURED = 10  # the newest records an estimate reads
UNIT_KEYS = ("ready", "refused", "failed", "skipped", "expired", "held", "measured")

OFF = "Selfies are off ([selfies] enabled in mind.toml)."
IN_GAME = "Log out of WoW first: photos render only while you are out of the game."
UNKNOWN = "Can't tell whether you're in WoW yet; try again in a moment."
NOTHING = "Nothing is queued to render."


def night_key(day: str) -> str:
    return f"manual:{day}"


@dataclass(frozen=True)
class Plan:
    """What a run renders now: first and "4 more" sets, situational/guild photos (`rows`), the
    held sets that stay queued, and the images they make (four a set, one a situation, a guild
    photo one a member)."""
    first: int = 0
    more: int = 0
    held: int = 0
    images: int = 0
    rows: tuple = ()

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
    first = more = held = 0
    for s in queued_sets(ctx.store):
        if not allows(ctx, s.guid) or ctx.done(policy.stage, f"{s.guid}:{s.set_no}"):
            continue
        if _set_held(ctx, _stored(ctx, s.guid), s.guid, s.set_no) is not None:
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
    return Plan(first, more, held, images, tuple(rows))


def per_image_s(conn) -> float | None:
    """Seconds an image, worker start included, over the newest measured passes: the booth's
    and this button's night_stage rows (calls: render attempts, call_s: the pass's seconds), else
    the night's selfies stage (its time over four images a unit). None with nothing measured."""
    rows = conn.execute("SELECT calls, call_s FROM night_stage WHERE stage IN (?, ?) AND calls > 0 "
                        "ORDER BY finished DESC LIMIT ?", ("selfies_booth", STAGE, MEASURED))
    samples = [r[1] / r[0] for r in rows if r[1] > 0]
    if not samples:
        rows = conn.execute(
            "SELECT s.started, s.finished, (SELECT count(*) FROM night_step n WHERE "
            "n.night = s.night AND n.stage = s.stage AND n.unit <> 'queue') FROM night_stage s "
            "WHERE s.stage = 'selfies' AND s.finished IS NOT NULL ORDER BY s.night DESC LIMIT ?",
            (MEASURED,))
        samples = [(f - st) / (SET_SIZE * n) for st, f, n in rows if n and f > st]
    return statistics.median(samples) if samples else None


def estimate(conn, p: Plan) -> tuple[int, bool]:
    """(rough seconds the run takes, whether earlier renders measured it, not a first guess)."""
    per = per_image_s(conn)
    if per is None:
        workers = (p.first + p.more > 0) + (p.snaps > 0)
        return round(workers * START_S + p.images * IMAGE_S), False
    return round(p.images * per), True


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
        self._count: dict | None = None
        self._count_at = -math.inf
        self._count_warned = False
        self._lease_tasks: set[asyncio.Task] = set()
        self._last = self._read_last()
        self._prior = app.arbiter._preempt  # the night's cancel_run, or the booth's preempt
        app.arbiter.set_preempt(self.preempt)
        app.night_scheduler.image_work = lambda: self.running
        app.register_shutdown_drain(self.close)

    # -- state -----------------------------------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

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
        """(code, words) of why a run cannot start now, else None. The backlog is not asked."""
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
        if self._answering():
            return "answering", "Members are still answering you on Discord. Try again in a minute."
        return None

    def _answering(self) -> bool:
        from leeroy_mind.talk.owed import answers_in_flight
        engine = getattr(self.app, "engine", None)
        try:
            return engine is not None and answers_in_flight(engine, discord=True) > 0
        except Exception:
            return False

    def _fence(self) -> str | None:
        """Why the run's first step must not take the GPU after all (a lease, a pause, a night or
        a booth pass that arrived after the request): its `by` code, else None."""
        arbiter = self.app.arbiter
        if getattr(arbiter, "paused", False):
            return "gpu_pause"
        if held := arbiter.holders - {"server", "discord"}:
            return _reason(held)
        task = getattr(self.app.night_scheduler, "_task", None)
        if task is not None and not task.done():
            return "night"
        booth = getattr(self.app, "selfie_booth", None)
        if booth is not None and (booth.running or booth._exclusive.locked()):
            return "booth"
        return None

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
        """What a run would render now (kinds, held sets, images, a rough estimate in seconds),
        read at most every COUNT_S; None when it could not be read."""
        now = time.monotonic()
        if now - self._count_at < COUNT_S:
            return self._count
        self._count_at = now
        try:
            p = plan(self._peek(), self.cfg, queued=False)
            seconds, measured = estimate(self.app.store.conn, p)
            self._count = {**p.kinds(), "held": p.held, "total": p.total, "images": p.images,
                           "estimate_s": seconds, "estimate_measured": measured}
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
               "backlog": None, "refusal": None, "code": None, "last": self._last}
        if self.running:
            out["progress"] = dict(self._progress or {})
            return out
        found = self.problem()
        out["backlog"] = self.backlog() if self.cfg.enabled else None
        if found is None and out["backlog"] is not None and out["backlog"]["total"] == 0:
            found = ("nothing", NOTHING)
        if found is not None:
            out["code"], out["refusal"] = found
        return out

    # -- the control op ---------------------------------------------------------------------------

    async def action(self, name: str) -> dict:
        if name == "render_stop":
            return await self.stop_now()
        return self.request()

    def request(self) -> dict:
        """Start a run of the whole backlog, or refuse in words. No await: the checks, the first
        sets' queueing and the task's creation are one step on the loop."""
        found = self.problem()
        if found is not None:
            log("selfies_manual_render_refused", reason=found[0])
            return {"ok": False, "error": found[0], "message": found[1], "render": self.snapshot()}
        try:
            ctx = self._ctx(night_key(self.app.clock.local().date().isoformat()))
            queue(ctx, POLICY)  # the first sets, as the night and the booth queue them
            p = plan(ctx, self.cfg, queued=True)
        except Exception as e:  # its text can carry stored data: the class only
            log("selfies_manual_render_refused", level=logging.WARNING, reason="plan_failed",
                error=type(e).__name__)
            return {"ok": False, "error": "plan_failed", "render": self.snapshot(),
                    "message": f"The render could not be planned ({type(e).__name__}). Nothing started."}
        if p.total == 0:
            self.invalidate()
            log("selfies_manual_render_refused", reason="nothing")
            return {"ok": False, "error": "nothing", "message": NOTHING, "render": self.snapshot()}
        self._stopping, self._calls = None, 0
        self._progress = {"done": 0, "total": p.total, "started": ctx.clock.now(),
                          "phase": "sets" if p.first + p.more else "snaps",
                          "estimate_s": estimate(ctx.store.conn, p)[0], **p.kinds()}
        self._task = asyncio.create_task(self._run(ctx, p), name="selfies-manual")
        self._task.add_done_callback(self._finished)
        log("selfies_manual_render_started", total=p.total, kinds=p.kinds())
        return {"ok": True, "render": self.snapshot()}

    async def stop_now(self) -> dict:
        """His Stop: the run is told to stop and its worker tree ends; answered once it has, or
        after STOP_WAIT_S with `pending`."""
        task = self._task
        if not self.running:
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
        stays."""
        if self.running and self._stopping is None:
            self._stopping = reason
            self._task.cancel()

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
        await self.cancel(_reason(self.app.arbiter.holders, self.app.arbiter))

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
        original = worker.render

        async def counted(*args, **kwargs):
            self._calls += 1
            return await original(*args, **kwargs)
        worker.render = counted
        return worker

    async def _release_presence(self) -> None:
        """The server and Discord presence leases go, as Free GPU lets them go: the server's
        stays released while the run runs (ServerLease.tick); his next Discord post takes the
        Discord one again, and that preempts the run."""
        server = getattr(self.app, "server", None)
        if server is not None:
            await server.lease._release("selfie_render")
        discord = getattr(self.app, "discord_talk", None)
        if discord is not None:
            await discord._release()

    async def _run(self, ctx, p: Plan) -> str:
        began, started = time.monotonic(), ctx.clock.now()
        sets, snaps = _Tally(self._moved), _Tally(self._moved)
        self._tallies = (sets, snaps)
        outcome, by = "failed", None
        arbiter = self.app.arbiter
        try:
            try:
                if (by := self._fence()) is not None:  # arrived after the request
                    outcome = "preempted"
                    return outcome
                await self._release_presence()
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
                        await snapstage._publish(ctx, self.cfg, pending)
                # Failed: the worker never came up, or every unit broke. A set the NSFW gate
                # refused, or a photo that expired or waits, is a run that did its work.
                landed = (sets["ready"] + sets["refused"] + sets["skipped"] + snaps["ready"]
                          + snaps["measured"] + snaps["expired"] + snaps["held"])
                outcome = "failed" if ctx.failures and not landed else "done"
                # Normal completion only: a chat model loaded beside the worker loads whole
                # again; a lease taken meanwhile owns the next cold preload.
                with suppress(LiveLeaseHeld):
                    await arbiter.make_room()
            except (LiveLeaseHeld, GpuPaused) as e:  # a lease, or the owner's GPU pause
                outcome, by = "preempted", self._stopping or _reason(
                    getattr(e, "holders", frozenset()), arbiter)
        except asyncio.CancelledError:
            outcome = "stopped" if self._stopping == "owner" else "preempted"
            by = self._stopping or "lease"
            raise
        except Exception as e:
            log("selfies_manual_render_failed", level=logging.WARNING, error=type(e).__name__)
        finally:
            self._record(ctx, started, began, outcome, by, sets, snaps)
        return outcome

    def _record(self, ctx, started, began, outcome, by, sets, snaps) -> None:
        seconds = round(time.monotonic() - began, 3)
        failed = sets["refused"] + sets["failed"] + snaps["failed"]
        held = sets["skipped"] + snaps["held"]
        progress = self._progress or {}
        rec = {"day": ctx.night.removeprefix("manual:"), "started": started,
               "finished": ctx.clock.now(), "outcome": outcome, "by": by,
               "total": progress.get("total", 0), "done": _units(sets, snaps),
               "sets": sets["ready"], "snaps": snaps["ready"] + snaps["measured"],
               "refused": sets["refused"], "failed": failed, "held": held,
               "expired": snaps["expired"], "seconds": seconds}
        try:
            with ctx.store.transaction():
                # Calls are actual render attempts, so the next estimate reads seconds an image.
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
        if not task.cancelled() and (error := task.exception()) is not None:
            log("selfies_manual_render_failed", level=logging.WARNING, error=type(error).__name__)
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


def _reason(holders, arbiter=None) -> str:
    """The `by` code of what stopped a run: the GPU pause, his login, his Discord post, an
    offline raid, the voice service, else another lease."""
    if (arbiter is not None and getattr(arbiter, "paused", False)) or "gpu_pause" in holders:
        return "gpu_pause"
    for holder, code in (("human", "login"), ("discord", "discord"),
                         ("offline_raid", "offline_raid"), ("voice_gpu", "voice")):
        if holder in holders:
            return code
    return "lease"
