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

from leeroy_mind.logs import log
from leeroy_mind.personas.io import load_persona
from leeroy_mind.photos import request_store
from leeroy_mind.imagefiles import read_image

from . import files
from .config import worker_command, worker_env
from .ration import SEND_MIN_AGE
from .render import scene_scale, start_worker
from .scene import build_scene_prompt, ref_crop, scene_from, scene_renderable
from .scene_choices import RECIPE
from .slot import SetRefused, TRIES, render_slot
from .snaps import looks_hash, mark_rendered
from .store import current, face_pick
from .words_scene import (BEHIND, WORDS_RECIPE, build_words_prompt, face_scale, is_words,
                          names_in)
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
    recipe = snap.scene.get("recipe")
    if type(recipe) is not int or recipe not in (RECIPE, WORDS_RECIPE):
        return None, "legacy_scene_schema"
    try:
        if scene_renderable(p, scene_from(snap.scene)):
            return None, "unrenderable"
        if is_words(snap.scene):
            build_words_prompt(p, snap.scene)
    except (KeyError, TypeError, ValueError):
        return None, "scene_invalid"
    if is_words(snap.scene):  # a deferred render: the cast's names as they are now
        from .request_scene import request_names
        if names_in(snap.scene, request_names(app, snap.guid)):
            return None, "scene_names"
    return p, None


def _face(app, p, guid: int, pick: int):
    """(the reference file, its framing) while her cover is change `pick`: the newest photo up to
    it that shows her face (store.face_pick), never one from behind; None when there is none."""
    with app.store.transaction():
        got = face_pick(app.store.conn, guid, pick)
    if got is None:
        return None
    face, framing = got
    return files.history(app.data_dir, guid, face).absolute(), framing or p.photo.framing


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
    if is_words(snap.scene):  # read from his words ([photos] scene_from_words)
        spec = build_words_prompt(p, snap.scene)
    else:
        spec = build_scene_prompt(p, scene)
    face = _face(app, p, snap.guid, snap.pick)
    if face is None or not face[0].is_file():
        return RenderOutcome("failed", "no_face_ref" if face is None else "missing")
    if getattr(cfg, "critic", False):
        return await _render_best(app, snap, room, p, scene, spec, face, out, thumb,
                                  worker_factory=worker_factory)
    # a scene read from his words from behind takes the face at REAR_SCALE of the scene's own
    identity = Identity(face[0], ref_crop(face[1]),
                        face_scale(snap.scene, scene_scale(cfg.identity_scale, scene.framing)))
    log("selfies_snap_face", snap=snap.id, guid=snap.guid, request=snap.request,
        scale=identity.scale)
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


# -- [selfies] critic: best-of-N for a requested photo -----------------------------------------

RETRY_SCALE = 0.45  # a request with no clean seed renders once more with her face this light
JUDGE_ALLOWANCE_S = 30.0  # what a second round leaves the judge, inside the borrow's budget
BORROW_S = 180.0  # the arbiter's borrow_timeout, when the room does not say


def judge_llm(app):
    """The judge's client for a request: one that does not wait on the live-chat admission the
    borrow has closed (the app's own would wait for the borrow to end), but honours the GPU
    pause. Tests set app.critic_llm."""
    llm = getattr(app, "critic_llm", None)
    if llm is not None:
        return llm
    from leeroy_mind.llm.client import OllamaClient
    llm = OllamaClient(app.cfg.ollama.url)
    gate = getattr(getattr(app, "gpu", None), "gate", None)
    if gate is not None:
        llm.gpu_gate = gate
    return llm


async def _render_best(app, snap, room, p, scene, spec, face, out, thumb, *,
                       worker_factory=None) -> RenderOutcome:
    """best_of seeds (seed k's attempts are render's attempts k*TRIES.., so k 0 is the seed it
    would render), each into snaps/critic/ and measured by the worker; when no seed is clean on
    the detectors, best_of more at RETRY_SCALE with the same worker. The worker then exits and
    the judge settles every seed; the best clean one is linked into place. With none clean and
    no second round yet, a second round runs (the worker started again) while the borrow's
    budget allows, else the request fails `critic` and the caller sends a ready-made photo."""
    from . import bestof, critic
    from .judge import VisionJudge
    from .params import Params, choose
    from .worker import to_the_end
    cfg = app.selfies.cfg
    data = app.data_dir
    crop = ref_crop(face[1])
    # a scene read from his words keeps its own scale (from behind: REAR_SCALE of it, and her
    # face is not expected to show); the parameter bandit draws only for the composed scenes
    words = is_words(snap.scene)
    behind = words and snap.scene["words"].get("framing") == BEHIND
    control = (face_scale(snap.scene, scene_scale(cfg.identity_scale, scene.framing)), cfg.steps)
    if words:
        codes = (snap.scene["words"].get("framing"), snap.scene["words"].get("wearing"), "none")
    else:
        codes = critic.scene_codes(scene)
    run = f"request:{snap.request}"
    worker = (worker_factory() if worker_factory is not None else
              SelfieWorker(worker_command(cfg, identity=True), env=worker_env(cfg)))
    room.own_worker(worker)  # startup itself can leave a process that needs teardown
    state = {"k": 0, "start_ms": 0.0, "render_ms": 0.0, "tries": 0, "slowest": 0.0}
    shots: list = []
    clean_on_detectors: set = set()

    async def start() -> None:
        began = time.perf_counter()
        try:
            await start_worker(worker, room.make_room)
        finally:
            ms = (time.perf_counter() - began) * 1000
            state["start_ms"] += ms
            room.timings["start_ms"] = state["start_ms"]
            state["last_start_ms"] = ms
        room.check()

    async def round_(retry: bool) -> None:
        for _ in range(cfg.best_of):
            k = state["k"]
            state["k"] += 1
            params = (Params(face_scale(snap.scene, RETRY_SCALE), cfg.steps) if retry else
                      Params(*control) if words else
                      choose(app.store, cfg, data, shape="portrait", framing=scene.framing,
                             control=control))
            identity = Identity(face[0], crop, params.scale)
            key = seed_for(snap.request, snap.guid, k * TRIES)
            cand = files.snap_candidate(data, snap.guid, snap.id, key).absolute()
            small = files.snap_candidate(data, snap.guid, snap.id, key, thumb=True).absolute()
            cand.parent.mkdir(parents=True, exist_ok=True)
            log("selfies_snap_face", snap=snap.id, guid=snap.guid, request=snap.request,
                scale=params.scale)

            def request(attempt, k=k, params=params, identity=identity, cand=cand, small=small):
                room.check()
                return RenderRequest(spec.phrases, spec.negative,
                                     seed_for(snap.request, snap.guid, k * TRIES + attempt),
                                     params.steps, cfg.guidance, SIZE, cand, small, THUMB_PX,
                                     cfg.nsfw_max, identity=identity, shape="portrait")
            began = time.perf_counter()
            try:
                got, tries = await render_slot(
                    worker, request, rid=lambda attempt, k=k: f"request:{snap.request}:{k}:{attempt}",
                    make_room=room.make_room)
            except SetRefused:
                state["tries"] += TRIES
                continue
            finally:
                spent = time.perf_counter() - began
                state["render_ms"] += spent * 1000
                state["slowest"] = max(state["slowest"], spent)
                room.timings["render_ms"] = state["render_ms"]
            state["tries"] += tries
            room.check()
            if not cand.is_file() or not small.is_file():
                raise WorkerError("unwritten")
            measured = await bestof.measure(worker, cand, identity)
            seed = seed_for(snap.request, snap.guid, k * TRIES + tries - 1)
            row_id = critic.insert(
                app.store, run=run, purpose="request", guid=snap.guid, snap_id=snap.id, seed=seed,
                file=critic.rel(data, cand), ref=critic.rel(data, face[0]), crop=crop,
                critique=measured, framing=codes[0], outfit=codes[1], prop=codes[2],
                scale=params.scale, steps=params.steps, arm=params.arm,
                face_expected=not behind, now=app.clock.now())
            shots.append(bestof.Shot(row_id, cand, small, got, seed, tries))
            if critic.verdict(measured, None, face_expected=not behind).clean:
                clean_on_detectors.add(row_id)

    def outcome(kind, code=None):
        return RenderOutcome(kind, code, state["start_ms"], state["render_ms"], state["tries"])

    judge = VisionJudge(judge_llm(app), cfg.critic_model)
    verdicts: dict = {}
    retried = False
    try:
        await start()
        await round_(False)
        if not clean_on_detectors:
            retried = True
            await round_(True)
        await to_the_end(worker.stop())
        room.check()
        verdicts.update(await bestof.settle(app.store, cfg, data, judge,
                                            [s.row_id for s in shots], shape="portrait",
                                            now=app.clock.now()))
        room.check()
        chosen = critic.best(verdicts)
        arbiter = getattr(room, "arbiter", None)
        budget = getattr(arbiter, "borrow_timeout", BORROW_S)
        left = budget - (time.monotonic() - getattr(room, "started", time.monotonic()))
        need = (state.get("last_start_ms", 0.0) / 1000 + cfg.best_of * state["slowest"]
                + JUDGE_ALLOWANCE_S)
        if chosen is None and not retried and left > need:
            retried = True
            before = len(shots)
            await start()
            await round_(True)
            await to_the_end(worker.stop())
            room.check()
            verdicts.update(await bestof.settle(app.store, cfg, data, judge,
                                                [s.row_id for s in shots[before:]],
                                                shape="portrait", now=app.clock.now()))
            room.check()
            chosen = critic.best(verdicts)
        if judge.used:
            await critic.unload(getattr(arbiter, "admin", None), judge.model)
        flagged = sum(1 for v in verdicts.values() if not v.clean)
        log("selfie_request_critic", request=snap.request, seeds=len(shots), flagged=flagged,
            retried=retried, chosen=chosen is not None)
        if not shots:
            return outcome("failed", "flagged")
        if chosen is None:
            return outcome("failed", "critic")
        shot = next(s for s in shots if s.row_id == chosen)
        with app.store.transaction():
            _, code = _fresh(app, snap)
            if code:
                return outcome("failed", code)
            # Exclusive links cannot replace a retained file, even if a crash/race left one.
            os.link(shot.out, out)
            os.link(shot.thumb, thumb)
            image = read_image(out, app.data_dir)
            if image is None:
                raise WorkerError('unwritten')
            request_store.remember_image(app.store, snap.id, identity=f'snap:{snap.id}',
                                         digest=hashlib.sha256(image).hexdigest())
            mark_rendered(app.store, snap.id, width=shot.got.width, height=shot.got.height,
                          seed=shot.seed, tries=shot.tries, nsfw=shot.got.nsfw,
                          prompt=shot.got.prompt, now=app.clock.now())
            critic.mark_chosen(app.store.conn, chosen)
        return outcome("ready")
    except WorkerError as error:
        code = error.reason if error.reason in ("oom", "vram", "unwritten", "stopped", "timeout") else "worker"
        return outcome("failed", code)
    except OSError:
        return outcome("failed", "files")
