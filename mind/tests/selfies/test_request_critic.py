"""A requested photo with the photo critic on ([selfies] critic, best_of): best-of-N, a second
round at identity 0.45 when no seed is clean, the judge only after the worker has stopped, a
request the critic refuses failing `critic` (the caller's ready-made photo then), and the
fallback's promotion. Fake workers and scratch files, never a GPU; every person is invented."""
import json
import logging
from dataclasses import replace


from conftest import run
from leeroy_mind.photos import request_store
from leeroy_mind.selfies import critic, files
from leeroy_mind.selfies.judge import MODEL
from leeroy_mind.selfies.request_render import RETRY_SCALE, render_request, seed_for
from leeroy_mind.selfies.slot import TRIES
from leeroy_mind.selfies.snaps import mark_rendered, plan_snap
from leeroy_mind.testing.fake_llm import FakeLLM
from selfies.fakes import JPEG
from selfies.test_request_render import Room, Worker, plan
from selfies.test_request_scene import app  # noqa: F401

CLEAN = {"limbs": "ok", "hands": "ok", "fingers_ok": True, "eyes_ok": True,
         "extra_people": False, "same_person": True, "overall": 3}


class Measuring(Worker):
    """The render fake with the critic op: two faces for a render whose seed is in `bad`."""

    def __init__(self, room, *, bad=(), identity=None):
        super().__init__(room)
        self.bad, self.identity = set(bad), identity or {}
        self.seeds, self.running_now = {}, False
        self.measured = []

    @property
    def running(self):
        return self.running_now

    async def start(self):
        await super().start()
        self.running_now = True

    async def stop(self):
        await super().stop()
        self.running_now = False

    async def render(self, req, *, rid):
        got = await super().render(req, rid=rid)
        self.seeds[req.out] = req.seed
        return got

    async def critic(self, path, *, ref=None, crop=None, rid=None):
        seed = self.seeds[path]
        self.measured.append(seed)
        return critic.from_answer({
            "faces": 2 if seed in self.bad else 1, "hands": 2, "hand": [],
            "eyes": {"tilt": 0.5, "level": 0.01}, "identity": self.identity.get(seed, 0.8),
            "identity_code": "ok", "detectors": {"faces": "ok", "hands": "ok"}, "seconds": 0.01})


def critic_on(app, best_of=2, **over):
    app.selfies.cfg = replace(app.selfies.cfg, snaps=True, critic=True, best_of=best_of, **over)


def judged(app, answers, worker=None):
    """app.critic_llm: a FakeLLM judge answering from `answers` in turn (the last one again),
    that fails the test if the worker still runs when it is asked."""
    replies = list(answers)
    calls = []

    def script(model, messages):
        if worker is not None:
            assert not worker.running
        calls.append(model)
        return json.dumps(replies.pop(0) if len(replies) > 1 else replies[0])
    app.critic_llm = FakeLLM(script, models={MODEL})
    return calls


def rows(app, snap_id):
    return [tuple(r) for r in app.store.conn.execute(
        "SELECT seed, verdict, codes, chosen, scale FROM selfie_critic WHERE snap_id=? ORDER BY id",
        (snap_id,))]


def test_best_of_two_sends_the_clean_seed_and_keeps_both(app, caplog):
    critic_on(app)
    snap, room = plan(app), Room()
    first, second = seed_for(snap.request, 101, 0), seed_for(snap.request, 101, TRIES)
    worker = Measuring(room, bad={first})
    calls = judged(app, [CLEAN], worker)
    with caplog.at_level(logging.INFO, logger="leeroy_mind"):
        result = run(render_request(app, snap, room, worker_factory=lambda: worker))
    assert (result.outcome, result.code) == ("ready", None)
    assert [r.seed for r in worker.calls] == [first, second] and worker.starts == 1
    assert rows(app, snap.id) == [(first, "flagged", "extra_face", 0, 0.45),
                                  (second, "clean", "", 1, 0.45)]
    stored = request_store.snap(app.store, snap.id)
    assert (stored.status, stored.seed, stored.tries) == ("ready", second, 1)
    out = files.snap(app.data_dir, 101, snap.id)
    assert out.read_bytes() == JPEG
    assert out.samefile(files.snap_candidate(app.data_dir, 101, snap.id, second))
    assert files.snap_candidate(app.data_dir, 101, snap.id, first).is_file()  # kept, unsent
    assert len(calls) == 2  # both seeds judged, after the worker stopped
    assert ("selfie_request_critic: request=%d seeds=2 flagged=1 retried=false chosen=true"
            % snap.request) in caplog.text


def test_no_clean_seed_on_the_detectors_renders_a_second_round_at_045(app):
    critic_on(app, identity_scale=0.8)  # a head-and-shoulders-free scene would take 0.6 here
    snap, room = plan(app), Room()
    bad = {seed_for(snap.request, 101, k * TRIES) for k in range(2)}
    worker = Measuring(room, bad=bad)
    judged(app, [CLEAN])
    result = run(render_request(app, snap, room, worker_factory=lambda: worker))
    assert result.outcome == "ready" and worker.starts == 1  # the same worker, still up
    assert [r.identity.scale for r in worker.calls] == [0.6, 0.6, RETRY_SCALE, RETRY_SCALE]
    assert request_store.snap(app.store, snap.id).seed == seed_for(snap.request, 101, 2 * TRIES)


def test_a_judge_that_refuses_every_seed_starts_the_worker_again_once(app):
    critic_on(app)
    snap, room = plan(app), Room()
    worker = Measuring(room)
    calls = judged(app, [{**CLEAN, "hands": "defect", "overall": 1}] * 2 + [CLEAN], worker)
    result = run(render_request(app, snap, room, worker_factory=lambda: worker))
    assert result.outcome == "ready" and worker.starts == 2 and len(calls) == 4
    assert [r.identity.scale for r in worker.calls[2:]] == [RETRY_SCALE] * 2
    assert [r[1] for r in rows(app, snap.id)] == ["flagged", "flagged", "clean", "clean"]


def test_a_request_the_critic_refuses_twice_fails_critic_and_sends_nothing(app):
    critic_on(app)
    snap, room = plan(app), Room()
    worker = Measuring(room)
    judged(app, [{**CLEAN, "extra_people": True, "overall": 0}], worker)
    result = run(render_request(app, snap, room, worker_factory=lambda: worker))
    assert (result.outcome, result.code) == ("failed", "critic")
    assert request_store.snap(app.store, snap.id).status == "planned"
    assert not files.snap(app.data_dir, 101, snap.id).exists()
    assert {r[1] for r in rows(app, snap.id)} == {"flagged"} and len(rows(app, snap.id)) == 4


def test_with_the_critic_off_the_request_renders_one_seed_unmeasured(app):
    snap, room = plan(app), Room()
    worker = Worker(room)  # no critic op at all: a call would fail the test
    result = run(render_request(app, snap, room, worker_factory=lambda: worker))
    assert result.outcome == "ready" and len(worker.calls) == 1
    assert rows(app, snap.id) == []


def test_a_refused_fresh_render_hands_its_place_to_the_ready_made_copy(app):
    snap = plan(app)
    request = request_store.get(app.store, snap.request)
    with app.store.transaction():
        copy = plan_snap(app.store, kind="situation", guid=101, source="request",
                         request=request.message, ref=f"request:{request.message}:fallback",
                         night="request", scene=dict(snap.scene), audience=request.audience,
                         phases=request_store.PHASES, not_before=0.0, expires=request.expires,
                         now=1.0, pick=snap.pick, looks=snap.looks)
        request_store.bind_snap(app.store, request.message, copy, kind="fallback")
    assert not request_store.promote_fallback(app.store, request.message, now=2.0)  # not ready
    mark_rendered(app.store, copy, width=832, height=1216, seed=1, tries=1, nsfw=0.01,
                  prompt="an invented prompt", now=2.0)
    assert request_store.promote_fallback(app.store, request.message, now=3.0)
    after = request_store.get(app.store, request.message)
    assert (after.fresh_snap, after.fallback_snap) == (copy, None)
    old = request_store.snap(app.store, snap.id)
    assert (old.status, old.ended) == ("expired", "flagged")
    assert not request_store.promote_fallback(app.store, request.message, now=4.0)
