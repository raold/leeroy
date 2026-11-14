"""Request rendering through fake workers and scratch files, never a GPU."""
import asyncio
from dataclasses import replace
import inspect

import pytest

from conftest import run
from leeroy_mind.discord.store import post
from leeroy_mind.llm.borrow import BorrowInterrupted
from leeroy_mind.personas.io import load_persona
from leeroy_mind.photos import request_store
from leeroy_mind.selfies import files
from leeroy_mind.selfies.request_render import render_request, seed_for
from leeroy_mind.selfies.scene import build_scene_prompt, ref_crop, scene_from, scene_json
from leeroy_mind.selfies.scene_choices import compose_choices
from leeroy_mind.selfies.snaps import looks_hash
from leeroy_mind.selfies.store import current, pick
from leeroy_mind.selfies.worker import Identity, Rendered, WorkerError
from selfies.fakes import JPEG, OWNER, fake_worker, ready
from selfies.test_request_scene import app, save  # noqa: F401
from selfies.test_scene_choices import PICKS
from selfies.test_selfie_prompt import woman


class Room:
    def __init__(self):
        self.workers, self.timings = [], {}
        self.roomed, self.block = 0, False

    def check(self):
        if self.block:
            raise BorrowInterrupted("human")

    def own_worker(self, worker):
        self.check()
        self.workers.append(worker)

    async def make_room(self):
        self.check()
        self.roomed += 1

    async def stop(self):
        for worker in self.workers:
            await worker.stop()


class Worker:
    def __init__(self, room, *, flagged=0, startup=None, render_error=None, after=None):
        self.room, self.flagged = room, flagged
        self.startup = startup
        self.render_error, self.after = render_error, after
        self.starts, self.stops, self.calls = 0, 0, []

    async def start(self):
        assert self in self.room.workers
        self.starts += 1
        if self.startup and self.starts == 1:
            raise self.startup

    async def render(self, req, *, rid):
        self.calls.append(req)
        if self.render_error and len(self.calls) == 1:
            raise self.render_error
        flagged = len(self.calls) <= self.flagged
        # Even an incorrectly writing flagged fake may only touch its staging files.
        req.out.write_bytes(JPEG)
        req.thumb.write_bytes(JPEG)
        if self.after:
            result = self.after()
            if inspect.isawaitable(result):
                await result
        return Rendered(flagged, .9 if flagged else .01, "safe invented prompt", 55, .01,
                        12.0, 832, 1216)

    async def stop(self):
        self.stops += 1


def plan(app, *, scene=None):
    message = post(app.store, channel="dm:101", author=OWNER, origin="human",
                   text="send me a selfie", now=app.clock.now())
    request, code = request_store.claim(app.store, message=message, guid=101,
        asked_channel="dm:101", thread=None, photo_channel="dm:101", audience="him",
        now=app.clock.now())
    assert code == "accepted"
    scene = scene or scene_json(scene_from(compose_choices({**PICKS, "audience": "him", "when": "any"})))
    return request_store.plan_fresh(app.store, request, scene=scene,
        pick=current(app.store, 101).pick, looks=looks_hash(load_persona(app.store, 101).appearance),
        now=app.clock.now())


def render(app, snap, room, worker):
    return run(render_request(app, snap, room, worker_factory=lambda: worker))


def test_identity_worker_renders_portrait_with_profile_crop_and_request_seed(app, tmp_path):
    p = load_persona(app.store, 101)
    save(app, replace(p, photo=replace(p.photo, framing="face")))
    snap, room, seen = plan(app), Room(), []
    factory = fake_worker(tmp_path, FAKE_SELFIE_IDENTITY="1")
    worker = factory()
    original = worker.render
    async def record(req, *, rid):
        seen.append(req)
        return await original(req, rid=rid)
    worker.render = record
    async def exercise():
        try:
            return await render_request(app, snap, room, worker_factory=lambda: worker)
        finally:
            await room.stop()
    result = run(exercise())
    assert (result.outcome, result.code, result.tries) == ("ready", None, 1)
    assert result.start_ms > 0 and result.render_ms > 0
    assert room.timings == {"start_ms": result.start_ms, "render_ms": result.render_ms}
    req, = seen
    assert (req.size, req.shape, req.thumb_size, req.steps, req.cfg, req.nsfw_max) == (
        1024, "portrait", 384, app.selfies.cfg.steps, app.selfies.cfg.guidance, app.selfies.cfg.nsfw_max)
    assert req.identity == Identity(files.history(app.data_dir, 101, snap.pick).absolute(),
                                    ref_crop("face"), app.selfies.cfg.identity_scale)
    assert "chosen.jpg" not in str(req.identity.ref)
    spec = build_scene_prompt(load_persona(app.store, 101), scene_from(snap.scene))
    assert (req.phrases, req.negative) == (spec.phrases, spec.negative)
    stored = request_store.snap(app.store, snap.id)
    assert (stored.status, stored.seed, stored.tries, stored.width, stored.height) == (
        "ready", seed_for(snap.request, 101, 0), 1, 832, 1216)
    assert files.snap(app.data_dir, 101, snap.id).is_file()
    assert files.snap(app.data_dir, 101, snap.id, thumb=True).is_file()
    assert req.out != files.snap(app.data_dir, 101, snap.id)


def test_seeds_are_request_scoped_repeatable_and_distinct():
    assert len({seed_for(request, guid, attempt) for request in (1, 2)
                for guid in (101, 102) for attempt in range(3)}) == 12
    assert seed_for(1, 101, 0) == seed_for(1, 101, 0)


@pytest.mark.parametrize("flagged", [1, 2, 3])
def test_three_seed_limit_and_flagged_outputs_never_touch_retained_files(app, flagged):
    snap, room = plan(app), Room()
    history = files.history(app.data_dir, 101, snap.pick)
    before = history.read_bytes()
    worker = Worker(room, flagged=flagged)
    result = render(app, snap, room, worker)
    assert result.tries == min(flagged + 1, 3)
    assert len(worker.calls) == result.tries
    assert len({r.seed for r in worker.calls}) == len(worker.calls)
    assert history.read_bytes() == before
    assert worker.stops == 0  # the room retains ownership through teardown/reload
    if flagged == 3:
        assert (result.outcome, result.code) == ("failed", "flagged")
        assert request_store.snap(app.store, snap.id).status == "planned"
        assert not files.snap(app.data_dir, 101, snap.id).exists()
        assert not worker.calls[-1].out.exists() and not worker.calls[-1].thumb.exists()
    else:
        assert result.outcome == "ready"


def test_startup_vram_and_render_oom_each_use_borrow_room(app):
    snap, room = plan(app), Room()
    worker = Worker(room, startup=WorkerError("vram"), render_error=WorkerError("oom"))
    assert render(app, snap, room, worker).outcome == "ready"
    assert worker.starts == 2 and room.roomed == 2
    assert len(worker.calls) == 2 and worker.calls[0] == worker.calls[1]


@pytest.mark.parametrize("stage", ["start", "render"])
def test_worker_errors_leave_teardown_with_room_and_plan_with_caller(app, stage):
    snap, room = plan(app), Room()
    kwargs = {"startup" if stage == "start" else "render_error": WorkerError("private worker text")}
    worker = Worker(room, **kwargs)
    result = render(app, snap, room, worker)
    assert (result.outcome, result.code) == ("failed", "worker")
    assert room.workers == [worker] and worker.stops == 0
    assert request_store.snap(app.store, snap.id).status == "planned"


@pytest.mark.parametrize("stage", ["start", "render"])
def test_cancellation_propagates_with_worker_already_owned(app, stage):
    snap, room = plan(app), Room()
    kwargs = {"startup" if stage == "start" else "render_error": asyncio.CancelledError()}
    worker = Worker(room, **kwargs)
    with pytest.raises(asyncio.CancelledError):
        render(app, snap, room, worker)
    assert room.workers == [worker]
    assert request_store.snap(app.store, snap.id).status == "planned"


@pytest.mark.parametrize("change,code", [("pick", "photo_changed"), ("looks", "looks_changed"),
    ("age", "unrenderable"), ("missing", "missing"), ("expired", "passed"),
    ("scene", "plan_changed")])
def test_changes_during_render_are_rechecked_before_publishing(app, change, code):
    snap, room = plan(app), Room()
    async def changed():
        p = load_persona(app.store, 101)
        if change == "pick":
            ready(app.store, app.data_dir, 101, 2)
            pick(app.store, 101, 2, 0, source="console", now=app.clock.now())
            files.sync_chosen(app.data_dir, 101, current(app.store, 101))
        elif change == "looks":
            save(app, replace(p, appearance=replace(p.appearance, hair_colour="silver")))
        elif change == "age":
            save(app, replace(p, identity=replace(p.identity, age=24)))
        elif change == "missing":
            files.history(app.data_dir, 101, snap.pick).unlink()
        else:
            with app.store.transaction():
                if change == "expired":
                    app.store.conn.execute("UPDATE selfie_snap SET expires=? WHERE id=?",
                                           (app.clock.now() + .001, snap.id))
                    await app.clock.advance(1, step=1)
                else:
                    app.store.conn.execute("UPDATE selfie_snap SET scene=json_set(scene,'$.when','late') WHERE id=?", (snap.id,))
    worker = Worker(room, after=changed)
    result = render(app, snap, room, worker)
    assert (result.outcome, result.code) == ("failed", code)
    assert request_store.snap(app.store, snap.id).status == "planned"
    assert not files.snap(app.data_dir, 101, snap.id).exists()


def test_lust_style_change_during_render_holds_image(app):
    save(app, woman(stance="lust", age=30))
    choices = {**PICKS, "audience": "him", "when": "any", "activity": "relaxing", "prop": "none",
               "setting": "her living room couch", "outfit": "satin midi dress",
               "colour": "navy", "expression": "laughing"}
    snap, room = plan(app, scene=scene_json(scene_from(compose_choices(choices)))), Room()
    def changed():
        p = load_persona(app.store, 101)
        save(app, replace(p, romance=replace(p.romance, stance="none")))
    result = render(app, snap, room, Worker(room, after=changed))
    assert (result.outcome, result.code) == ("failed", "unrenderable")
    assert not files.snap(app.data_dir, 101, snap.id).exists()


def test_hard_lease_during_render_prevents_publish_and_propagates(app):
    snap, room = plan(app), Room()
    worker = Worker(room, after=lambda: setattr(room, "block", True))
    with pytest.raises(BorrowInterrupted):
        render(app, snap, room, worker)
    assert not files.snap(app.data_dir, 101, snap.id).exists()
    assert request_store.snap(app.store, snap.id).status == "planned"


@pytest.mark.parametrize("thumb", [False, True])
def test_planned_crash_files_are_never_overwritten_or_deleted(app, thumb):
    snap, room = plan(app), Room()
    existing = files.snap(app.data_dir, 101, snap.id, thumb=thumb)
    existing.parent.mkdir(parents=True, exist_ok=True)
    existing.write_bytes(b"retained crash file")
    worker = Worker(room)
    result = render(app, snap, room, worker)
    assert (result.outcome, result.code) == ("failed", "files_exist")
    assert existing.read_bytes() == b"retained crash file" and worker.starts == 0


def test_ready_recovery_after_deadline_never_starts_worker_or_changes_files(app):
    snap, room = plan(app), Room()
    assert render(app, snap, room, Worker(room)).outcome == "ready"
    stored = request_store.snap(app.store, snap.id)
    before = files.snap(app.data_dir, 101, snap.id).read_bytes()
    run(app.clock.advance(86401, step=86401))
    worker = Worker(room)
    assert render(app, stored, room, worker).outcome == "ready"
    assert worker.starts == 0
    assert files.snap(app.data_dir, 101, snap.id).read_bytes() == before
    assert request_store.snap(app.store, snap.id) == stored


def test_legacy_scene_never_loads_worker(app):
    snap, room = plan(app, scene={"shows": "old free prose", "audience": "him"}), Room()
    worker = Worker(room)
    result = render(app, snap, room, worker)
    assert result.code == "legacy_scene_schema" and not room.workers


def test_unwritten_worker_output_does_not_mark_ready(app):
    snap, room = plan(app), Room()
    worker = Worker(room)
    original = worker.render
    async def incomplete(req, *, rid):
        answer = await original(req, rid=rid)
        req.thumb.unlink()
        return answer
    worker.render = incomplete
    assert render(app, snap, room, worker).code == "unwritten"
    assert request_store.snap(app.store, snap.id).status == "planned"
