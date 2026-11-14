"""Identity snap rendering inside a caller-owned, bounded GPU borrow.

The room owns teardown even when startup or rendering is cancelled. Output is staged away
from retained snaps; publishing never overwrites a file left by delivery or a prior crash.
"""
import hashlib
import os
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
import tempfile
import time

from leeroy_mind.personas.io import load_persona
from leeroy_mind.photos import request_store
from leeroy_mind.imagefiles import read_image

from . import files
from .config import worker_command, worker_env
from .ration import SEND_MIN_AGE
from .render import start_worker
from .scene import build_scene_prompt, ref_crop, scene_from, scene_renderable
from .scene_choices import RECIPE
from .slot import SetRefused, TRIES, render_slot
from .snaps import looks_hash, mark_rendered
from .store import current, picked_framing
from .worker import Identity, RenderRequest, SelfieWorker, WorkerError

SIZE = 1024
THUMB_PX = 384


@dataclass(frozen=True)
class RenderOutcome:
    outcome: str
    code: str | None = None
    start_ms: float = 0.0
    render_ms: float = 0.0
    tries: int = 0


def seed_for(request: int, guid: int, attempt: int) -> int:
    digest = hashlib.sha256(f"request|{request}|{guid}|{attempt}".encode()).digest()
    return int.from_bytes(digest[:4], "big")


def _fresh(app, snap):
    """Re-read both identity and the plan; no await separates this from persistence."""
    latest = request_store.snap(app.store, snap.id)
    if latest is None or latest.status != "planned":
        return None, "not_planned"
    if (latest.source != "request" or latest.kind != "situation" or latest.request is None
            or (latest.guid, latest.request, latest.pick, latest.looks, latest.scene,
                latest.audience) != (snap.guid, snap.request, snap.pick, snap.looks,
                                      snap.scene, snap.audience)):
        return None, "plan_changed"
    if latest.expires <= app.clock.now():
        return None, "passed"
    try:
        p = load_persona(app.store, snap.guid)
    except ValueError:
        return None, "unrenderable"
    if p is None or p.identity.age < SEND_MIN_AGE or p.appearance is None:
        return None, "unrenderable"
    pick = current(app.store, snap.guid)
    if pick is None or pick.pick != snap.pick:
        return None, "photo_changed"
    if looks_hash(p.appearance) != snap.looks:
        return None, "looks_changed"
    if not files.history(app.data_dir, snap.guid, snap.pick).is_file():
        return None, "missing"
    if type(snap.scene.get("recipe")) is not int or snap.scene["recipe"] != RECIPE:
        return None, "legacy_scene_schema"
    try:
        if scene_renderable(p, scene_from(snap.scene)):
            return None, "unrenderable"
    except (KeyError, TypeError, ValueError):
        return None, "scene_invalid"
    return p, None


def _framing(app, p, pick: int) -> str:
    """A varied set's look framing when that photo is her cover, else her photo kind's."""
    with app.store.transaction():
        return picked_framing(app.store.conn, pick) or p.photo.framing


async def render_request(app, snap, room, *, worker_factory=None) -> RenderOutcome:
    """Render once with the existing three-seed/NSFW/OOM rules; caller changes failure state.

    A ready request is recoverable after its original deadline. It is never re-rendered.
    Planned crash leftovers fail closed and stay on disk rather than being overwritten.
    """
    room.check()
    stored = request_store.snap(app.store, snap.id)
    if stored is not None and stored.status == "ready" and stored.source == "request":
        return RenderOutcome("ready")
    p, code = _fresh(app, snap)
    if code:
        return RenderOutcome("failed", code)
    cfg = app.selfies.cfg
    out = files.snap(app.data_dir, snap.guid, snap.id).absolute()
    thumb = files.snap(app.data_dir, snap.guid, snap.id, thumb=True).absolute()
    if out.exists() or thumb.exists():
        return RenderOutcome("failed", "files_exist")
    scene = scene_from(snap.scene)
    spec = build_scene_prompt(p, scene)
    identity = Identity(files.history(app.data_dir, snap.guid, snap.pick).absolute(),
                        ref_crop(_framing(app, p, snap.pick)), cfg.identity_scale)
    out.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f".{snap.id}.request-", dir=out.parent))
    staged, small = stage / "photo.jpg", stage / "thumb.jpg"
    worker = (worker_factory() if worker_factory is not None else
              SelfieWorker(worker_command(cfg, identity=True), env=worker_env(cfg)))
    room.own_worker(worker)  # startup itself can leave a process that needs teardown
    start_ms = render_ms = 0.0
    tries = 0

    def request(attempt):
        nonlocal tries
        room.check()
        tries = attempt + 1
        return RenderRequest(spec.phrases, spec.negative, seed_for(snap.request, snap.guid, attempt),
            cfg.steps, cfg.guidance, SIZE, staged, small, THUMB_PX, cfg.nsfw_max,
            identity=identity, shape="portrait")

    try:
        began = time.perf_counter()
        try:
            await start_worker(worker, room.make_room)
        finally:
            start_ms = (time.perf_counter() - began) * 1000
            room.timings["start_ms"] = start_ms
        room.check()
        began = time.perf_counter()
        try:
            got, tries = await render_slot(worker, request,
                rid=lambda attempt: f"request:{snap.request}:{attempt}", make_room=room.make_room)
        finally:
            render_ms = (time.perf_counter() - began) * 1000
            room.timings["render_ms"] = render_ms
        room.check()
        if not staged.is_file() or not small.is_file():
            raise WorkerError("unwritten")
        with app.store.transaction():
            _, code = _fresh(app, snap)
            if code:
                return RenderOutcome("failed", code, start_ms, render_ms, tries)
            # Exclusive links cannot replace a retained file, even if a crash/race left one.
            os.link(staged, out)
            os.link(small, thumb)
            image = read_image(out, app.data_dir)
            if image is None:
                raise WorkerError('unwritten')
            request_store.remember_image(app.store, snap.id, identity=f'snap:{snap.id}',
                                         digest=hashlib.sha256(image).hexdigest())
            mark_rendered(app.store, snap.id, width=got.width, height=got.height,
                seed=seed_for(snap.request, snap.guid, tries - 1), tries=tries, nsfw=got.nsfw,
                prompt=got.prompt, now=app.clock.now())
        # These were private staging links; the retained image and thumb stay in place.
        with suppress(OSError):
            staged.unlink()
            small.unlink()
            stage.rmdir()
        return RenderOutcome("ready", None, start_ms, render_ms, tries)
    except SetRefused:
        return RenderOutcome("failed", "flagged", start_ms, render_ms, TRIES)
    except WorkerError as error:
        code = error.reason if error.reason in ("oom", "vram", "unwritten", "stopped", "timeout") else "worker"
        return RenderOutcome("failed", code, start_ms, render_ms, tries)
    except OSError:
        return RenderOutcome("failed", "files", start_ms, render_ms, tries)
