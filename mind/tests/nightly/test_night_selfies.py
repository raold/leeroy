"""The night's profile photos (P2 Task 10): four checked candidates a member, rendered by the
fake worker (no torch, no model, no GPU) under the night's arbiter on a FakeAdmin. Every person
here is invented."""
import asyncio
import json
import logging
from dataclasses import replace

import pytest

from conftest import TEST_CAST, run
from leeroy_mind.nightly import budget
from leeroy_mind.nightly.budget import DEGRADE, UNIT_CALLS, plan_night
from leeroy_mind.nightly.runner import NightRunner, Stage
from leeroy_mind.nightly.store import run_begin, run_get
from leeroy_mind.personas.io import load_persona
from leeroy_mind.protocol import Member
from leeroy_mind.selfies.files import candidate, set_dir, staging_dir
from leeroy_mind.selfies.job import pid_alive
from leeroy_mind.selfies.prompt import build_prompt
from leeroy_mind.selfies.stage import (CONTENDED, SET_SIZE, STAGE, THUMB_PX, TRIES, SetRefused,
                                       make_stage, render_candidates, seed_for)
from leeroy_mind.selfies.store import ensure_first_sets, queue_more, queued_sets, ready_set
from leeroy_mind.selfies.worker import Rendered, SelfieWorker, WorkerError
from nightly.conftest import FakeAdmin
from nightly.test_night_enrich import APPEARANCE
from selfies.fakes import JPEG, SELFIE_CFG, fake_command, fake_worker, renderable_cast, until


def queued(h) -> list[tuple[int, int]]:
    return [(s.guid, s.set_no) for s in queued_sets(h.store)]


def requests(log) -> list[tuple[str, int]]:
    """The fake worker's request log: (request id, seed) for each render it was sent."""
    return [(rid, int(seed)) for rid, seed in
            (line.split() for line in log.read_text(encoding="utf-8").splitlines())]


# -- Review Focus 1 and 2 -------------------------------------------------------------------------

def test_his_login_mid_render_kills_the_worker_tree_before_the_live_preload(h, tmp_path):
    renderable_cast(h.store, 101)
    pids, log = tmp_path / "pids.json", tmp_path / "requests.log"; seen = {}; stops = []
    class Watched(SelfieWorker):
        async def stop(self):
            await super().stop()                  # waits until the whole tree is gone
            stops.append("done")
    class Admin(FakeAdmin):
        async def preload(self, model, keep_alive, *, options=None):
            got = json.loads(pids.read_text())
            seen["alive"] = (pid_alive(got["pid"]), pid_alive(got["child"]))
            seen["stops"] = list(stops)
            await super().preload(model, keep_alive, options=options)
    ctx = h.ctx(admin=Admin([h.EMB]))
    cmd, env = fake_command(tmp_path, FAKE_SELFIE_HANG="1", FAKE_SELFIE_CHILD="1",
                            FAKE_SELFIE_PIDS=str(pids), FAKE_SELFIE_LOG=str(log))
    stage = make_stage(SELFIE_CFG, worker=lambda: Watched(cmd, env=env))
    async def go():
        task = asyncio.create_task(stage(ctx))
        await until(lambda: log.exists() and "101:1" in log.read_text())   # mid-render
        async def preempt():
            task.cancel(); await asyncio.wait({task})
        ctx.arbiter.set_preempt(preempt)
        ctx.arbiter.hold("human")
        await ctx.arbiter.hand_over()
        assert task.cancelled()
    run(go())
    assert seen["alive"] == (False, False)                  # both gone before the live preload
    assert seen["stops"] == ["done"]                         # and the stage waited for it to be
    assert ("preload", h.LIVE, -1) in ctx.arbiter.admin.calls
    assert not ctx.done("selfies", "101:1") and not set_dir(h.settings.data_dir, 101, 1).exists()
    assert queued(h) == [(101, 1)] and not ctx.failures      # it waits for the next night


def test_an_oom_makes_room_and_retries_once_and_contention_ends_the_stage(h, tmp_path, caplog):
    renderable_cast(h.store, 101, 102, 103)
    admin = FakeAdmin([h.EMB, h.LIVE])            # Orion came back while the stage ran
    ctx = h.ctx(admin=admin)
    run(make_stage(SELFIE_CFG, worker=fake_worker(tmp_path, FAKE_SELFIE_OOM="1"))(ctx))
    assert ("unload", h.LIVE) in admin.calls and all(ready_set(h.store, g, 1) for g in (101, 102, 103))
    assert not ctx.failures
    for g in (101, 102, 103):
        queue_more(h.store, g, now=h.T)
    log = tmp_path / "requests.log"
    ctx2 = h.ctx(admin=FakeAdmin([h.EMB]), night="2026-10-30")
    with caplog.at_level(logging.INFO, logger="leeroy_mind"):
        run(make_stage(SELFIE_CFG, worker=fake_worker(tmp_path, FAKE_SELFIE_OOM="99",
                                                      FAKE_SELFIE_LOG=str(log)))(ctx2))
    assert ctx2.failures["selfies"] == CONTENDED
    assert [(s.guid, s.set_no) for s in queued_sets(h.store)] == [(101, 2), (102, 2), (103, 2)]
    assert not any(set_dir(h.settings.data_dir, g, 2).exists() for g in (101, 102, 103))
    assert "103:2" not in log.read_text()      # the third set was never started
    # each set's first slot asked once more after making room, then gave up
    assert [rid for rid, _ in requests(log)] == ["101:2:0:0"] * 2 + ["102:2:0:0"] * 2
    assert caplog.text.count("selfies_stopped") == 1
    assert "selfies_stopped: night=2026-10-30 reason=oom" in caplog.text
    # the sets finished the night before are kept, files and all
    assert all(ready_set(h.store, g, 1) and candidate(h.settings.data_dir, g, 1, 3).exists()
               for g in (101, 102, 103))


def test_a_lease_taken_mid_stage_ends_it_once_without_a_failure_and_keeps_finished_sets(
        h, tmp_path, caplog):
    renderable_cast(h.store, 101, 102, 103)
    admin = FakeAdmin([h.EMB]); ctx = h.ctx(admin=admin)
    run_begin(h.store, ctx.night, *ctx.window, now=ctx.window[1])
    pids = tmp_path / "pids.json"
    cmd, env = fake_command(tmp_path, FAKE_SELFIE_CHILD="1", FAKE_SELFIE_PIDS=str(pids))
    class Contended(SelfieWorker):
        async def render(self, req, *, rid=None):
            if rid == "102:1:0:0":
                ctx.arbiter.hold("voice")         # a lease whose preempt has not reached the run
                raise WorkerError("oom")          # and Orion's load meanwhile: make_room refuses
            return await super().render(req, rid=rid)
    ran = []
    async def after(c): ran.append("after")
    with caplog.at_level(logging.INFO, logger="leeroy_mind"):
        run(NightRunner([Stage(STAGE, make_stage(SELFIE_CFG, worker=lambda: Contended(cmd, env=env)),
                               "image"), Stage("after", after, None)],
                        lambda n, w: ctx).run(ctx.night, ctx.window))
    assert ready_set(h.store, 101, 1) and queued(h) == [(102, 1), (103, 1)]
    assert not ctx.failures and run_get(h.store, ctx.night).error is None and ran == ["after"]
    assert caplog.text.count("nightly_image_refused") == 1 and admin.calls == []
    got = json.loads(pids.read_text())
    assert not pid_alive(got["pid"]) and not pid_alive(got["child"])


# -- a night's sets -------------------------------------------------------------------------------

def test_a_night_renders_four_checked_candidates_for_each_member_without_a_photo(h, tmp_path):
    renderable_cast(h.store, *TEST_CAST)
    ctx = h.ctx()
    run(make_stage(SELFIE_CFG, worker=fake_worker(tmp_path))(ctx))
    data = h.settings.data_dir
    for g in TEST_CAST:
        s = ready_set(h.store, g, 1)
        assert s is not None and s.night == ctx.night and s.reason == "first"
        assert s.persona_version == h.store.persona_versions()[g]
        assert ctx.done(STAGE, f"{g}:1") and not staging_dir(data, g, 1).exists()
        for i in range(SET_SIZE):
            assert candidate(data, g, 1, i).read_bytes() == JPEG
            assert candidate(data, g, 1, i, thumb=True).read_bytes() == JPEG
    rows = h.store.conn.execute("SELECT guid, set_no, idx, seed, tries, nsfw, prompt "
                                "FROM selfie_candidate ORDER BY guid, idx").fetchall()
    assert len(rows) == SET_SIZE * len(TEST_CAST)
    assert all(r["tries"] == 1 and r["nsfw"] < SELFIE_CFG.nsfw_max for r in rows)
    shown = ", ".join(t for t, _ in build_prompt(load_persona(h.store, 101)).phrases)
    assert [r["prompt"] for r in rows if r["guid"] == 101] == [shown] * SET_SIZE   # what was rendered
    assert [r["seed"] for r in rows if r["guid"] == 101] == [seed_for(ctx.night, 101, 1, i, 0)
                                                             for i in range(SET_SIZE)]
    assert ctx.done(STAGE, "queue") and queued(h) == [] and not ctx.failures


def test_a_flagged_image_is_replaced_with_a_new_seed_and_three_flags_fail_only_that_member(
        h, tmp_path, caplog):
    renderable_cast(h.store, 101, 102, 103)
    once = seed_for(h.night, 101, 1, 2, 0)
    always = [seed_for(h.night, 102, 1, 1, attempt) for attempt in range(TRIES)]
    ctx = h.ctx()
    flagged = ",".join(map(str, [once, *always]))
    with caplog.at_level(logging.INFO, logger="leeroy_mind"):
        run(make_stage(SELFIE_CFG, worker=fake_worker(tmp_path, FAKE_SELFIE_FLAG_SEEDS=flagged))(ctx))
    row = h.store.conn.execute("SELECT seed, tries FROM selfie_candidate WHERE guid=101 AND idx=2").fetchone()
    assert (row["seed"], row["tries"]) == (seed_for(h.night, 101, 1, 2, 1), 2)
    assert candidate(h.settings.data_dir, 101, 1, 2).read_bytes() == JPEG
    assert ready_set(h.store, 101, 1) and ready_set(h.store, 103, 1)      # the next member is rendered
    assert ready_set(h.store, 102, 1) is None and queued(h) == [(102, 1)]
    data = h.settings.data_dir
    assert not set_dir(data, 102, 1).exists() and not staging_dir(data, 102, 1).exists()
    assert ctx.failures[STAGE] == 1 and not ctx.done(STAGE, "102:1")
    assert caplog.text.count("selfies_flagged") == 1 + TRIES
    assert "selfies_flagged: night=2026-10-29 guid=101 set=1 idx=2 attempt=0 nsfw=0.95" in caplog.text
    assert "nightly_unit_failed: night=2026-10-29 stage=selfies unit=102:1 error=SetRefused" in caplog.text


def test_members_without_a_persona_looks_or_photo_kind_are_not_queued(h, tmp_path, caplog):
    renderable_cast(h.store, 101)
    h.persona(102, appearance=APPEARANCE)                              # looks, no photo kind yet
    # 103-105: no looks yet; 106: in the roster at 80 with no persona
    h.store.upsert_cast([Member(guid=106, name="Fenna", race=1, class_=1, gender=0, level=80,
                                rank=3, online=False, map=0, zone=0, spec_tab=-1)], h.T)
    ensure_first_sets(h.store, [104], now=h.T)       # queued before 104's looks went: skipped, kept
    log = tmp_path / "requests.log"
    ctx = h.ctx()
    with caplog.at_level(logging.INFO, logger="leeroy_mind"):
        run(make_stage(SELFIE_CFG, worker=fake_worker(tmp_path, FAKE_SELFIE_LOG=str(log)))(ctx))
    assert ready_set(h.store, 101, 1) and queued(h) == [(104, 1)]
    assert ctx.done(STAGE, "104:1") and not ctx.failures
    assert {rid.split(":")[0] for rid, _ in requests(log)} == {"101"}
    with h.store.transaction():
        sets = h.store.conn.execute("SELECT guid FROM selfie_set ORDER BY guid").fetchall()
    assert [r["guid"] for r in sets] == [101, 104]
    assert ('selfies_queued: night=2026-10-29 queued=1 '
            'missing={"no looks yet":3,"no persona":1,"no photo kind yet":1}') in caplog.text


def test_a_set_directory_without_a_ready_row_is_cleared_before_rendering(h, tmp_path):
    renderable_cast(h.store, 101)
    queue_more(h.store, 101, now=h.T)
    data = h.settings.data_dir
    d = set_dir(data, 101, 1); d.mkdir(parents=True)                   # a crash after the rename
    for i in range(SET_SIZE):
        (d / f"c{i}.jpg").write_bytes(b"left by a crash")
    (d / "stray.jpg").write_bytes(b"left by a crash")
    staging_dir(data, 101, 1).mkdir()
    (staging_dir(data, 101, 1) / "c1.jpg").write_bytes(b"half")
    run(make_stage(SELFIE_CFG, worker=fake_worker(tmp_path))(h.ctx()))
    assert ready_set(h.store, 101, 1) is not None
    assert sorted(p.name for p in d.iterdir()) == sorted(
        n for i in range(SET_SIZE) for n in (f"c{i}.jpg", f"c{i}.thumb.jpg"))
    assert all(candidate(data, 101, 1, i).read_bytes() == JPEG for i in range(SET_SIZE))
    assert not staging_dir(data, 101, 1).exists()


def test_a_resumed_night_renders_only_unfinished_sets_with_the_same_seeds(h, tmp_path):
    renderable_cast(h.store, 101, 102)
    log = tmp_path / "requests.log"
    cmd, env = fake_command(tmp_path, FAKE_SELFIE_LOG=str(log))
    class Interrupted(SelfieWorker):
        async def render(self, req, *, rid=None):
            if rid == "102:1:2:0":                            # his login, halfway through a set
                asyncio.current_task().cancel()
                await asyncio.sleep(0)
            return await super().render(req, rid=rid)
    with pytest.raises(asyncio.CancelledError):
        run(make_stage(SELFIE_CFG, worker=lambda: Interrupted(cmd, env=env))(h.ctx()))
    first = requests(log)
    assert [rid for rid, _ in first] == [f"101:1:{i}:0" for i in range(4)] + ["102:1:0:0", "102:1:1:0"]
    assert ready_set(h.store, 101, 1) and queued(h) == [(102, 1)]
    ctx = h.ctx()
    run(make_stage(SELFIE_CFG, worker=fake_worker(tmp_path, FAKE_SELFIE_LOG=str(log)))(ctx))
    again = requests(log)[len(first):]
    assert again == [(f"102:1:{i}:0", seed_for(h.night, 102, 1, i, 0)) for i in range(4)]
    assert again[:2] == first[4:]                                 # the same seeds as before
    assert ready_set(h.store, 102, 1) and queued(h) == [] and not ctx.failures
    assert not staging_dir(h.settings.data_dir, 102, 1).exists()


def test_the_night_caps_its_sets_across_a_resume(h, tmp_path):
    renderable_cast(h.store, 101, 102, 103)
    log = tmp_path / "requests.log"
    first = h.ctx()
    first.settings = replace(first.settings, selfie_per_night=2)
    run(make_stage(SELFIE_CFG, worker=fake_worker(tmp_path, FAKE_SELFIE_LOG=str(log)))(first))
    again = h.ctx()
    again.settings = replace(again.settings, selfie_per_night=2)
    run(make_stage(SELFIE_CFG, worker=fake_worker(tmp_path, FAKE_SELFIE_LOG=str(log)))(again))
    assert queued(h) == [(103, 1)]                              # two tonight, the rest tomorrow
    assert {rid.split(":")[0] for rid, _ in requests(log)} == {"101", "102"}


# -- the worker -----------------------------------------------------------------------------------

@pytest.mark.parametrize("how,reason", [("cuda", "cuda"), ("missing", "FileNotFoundError")])
def test_a_worker_that_cannot_start_fails_one_unit_and_not_the_night(h, tmp_path, caplog, how,
                                                                    reason):
    renderable_cast(h.store, 101, 102)
    ctx = h.ctx()
    run_begin(h.store, ctx.night, *ctx.window, now=ctx.window[1])
    worker = (fake_worker(tmp_path, FAKE_SELFIE_READY_ERROR=how) if how != "missing" else
              (lambda: SelfieWorker([str(tmp_path / "no-such-python.exe")])))
    ran = []
    async def after(c): ran.append("after")
    with caplog.at_level(logging.INFO, logger="leeroy_mind"):
        run(NightRunner([Stage(STAGE, make_stage(SELFIE_CFG, worker=worker), "image"),
                         Stage("after", after, None)], lambda n, w: ctx).run(ctx.night, ctx.window))
    assert ctx.failures == {STAGE: 1} and ran == ["after"]
    assert json.loads(run_get(h.store, ctx.night).error) == {STAGE: 1}
    assert f"selfies_unavailable: night=2026-10-29 reason={reason}" in caplog.text
    assert "nightly_unit_failed: night=2026-10-29 stage=selfies unit=worker" in caplog.text
    assert queued(h) == [(101, 1), (102, 1)] and ctx.done(STAGE, "queue")


def test_a_worker_refused_for_vram_makes_room_and_is_started_once_more(h, tmp_path, caplog):
    renderable_cast(h.store, 101)
    admin = FakeAdmin([h.EMB, h.LIVE]); ctx = h.ctx(admin=admin)       # Orion, loaded meanwhile
    cmd, env = fake_command(tmp_path, FAKE_SELFIE_READY_ERROR="vram")
    starts = []
    class Counted(SelfieWorker):
        async def start(self):
            starts.append(admin.loaded.copy())
            return await super().start()
    with caplog.at_level(logging.INFO, logger="leeroy_mind"):
        run(make_stage(SELFIE_CFG, worker=lambda: Counted(cmd, env=env))(ctx))
    assert starts == [[h.EMB, h.LIVE], [h.EMB]] and admin.calls == [("unload", h.LIVE)]
    assert ctx.failures == {STAGE: 1} and "selfies_unavailable: night=2026-10-29 reason=vram" in caplog.text


def test_a_stop_during_the_start_is_his_login_not_an_unavailable_worker(h, tmp_path, caplog):
    renderable_cast(h.store, 101)
    ctx = h.ctx()
    class Stopped(SelfieWorker):
        async def start(self):
            raise WorkerError("stopped")
    with caplog.at_level(logging.INFO, logger="leeroy_mind"):
        run(make_stage(SELFIE_CFG, worker=lambda: Stopped(["unused"]))(ctx))
    assert not ctx.failures and "selfies_unavailable" not in caplog.text
    assert "selfies_preempted: night=2026-10-29" in caplog.text and queued(h) == [(101, 1)]


def test_a_worker_that_dies_is_started_again_once_in_the_stage(h, tmp_path, caplog):
    renderable_cast(h.store, 101, 102, 103, 104)
    log = tmp_path / "requests.log"
    cmd, env = fake_command(tmp_path, FAKE_SELFIE_LOG=str(log))
    class Dying(SelfieWorker):
        async def render(self, req, *, rid=None):
            if rid in ("101:1:0:0", "103:1:0:0"):
                self._tree.kill()                   # the worker crashed: its answer never comes
            return await super().render(req, rid=rid)
    ctx = h.ctx()
    with caplog.at_level(logging.INFO, logger="leeroy_mind"):
        run(make_stage(SELFIE_CFG, worker=lambda: Dying(cmd, env=env))(ctx))
    assert ready_set(h.store, 102, 1) and queued(h) == [(101, 1), (103, 1), (104, 1)]
    assert ctx.failures == {STAGE: 2}                       # 101 and 103; 104 was never asked for
    assert not any(rid.startswith("104:") for rid, _ in requests(log))
    assert "selfies_stopped: night=2026-10-29 reason=gone" in caplog.text


# -- the slots ------------------------------------------------------------------------------------

def test_seeds_are_the_nights_and_render_candidates_asks_for_each_slot(h, tmp_path):
    import hashlib
    digest = hashlib.sha256(b"2026-10-29|101|2|3|1").digest()
    assert seed_for("2026-10-29", 101, 2, 3, 1) == int.from_bytes(digest[:4], "big")
    assert (STAGE, SET_SIZE, TRIES, THUMB_PX, CONTENDED) == ("selfies", 4, 3, 512, 2)
    renderable_cast(h.store, 101)
    spec = build_prompt(load_persona(h.store, 101))
    out = tmp_path / "out"; out.mkdir()
    rooms = []
    async def room(): rooms.append(1)
    async def go():
        w = fake_worker(tmp_path, FAKE_SELFIE_OOM="1")()
        await w.start()
        try:
            return await render_candidates(w, spec, out, night=h.night, guid=101, set_no=2,
                                           cfg=SELFIE_CFG, make_room=room)
        finally:
            await w.stop()
    got = run(go())
    assert [c.idx for c in got] == [0, 1, 2, 3] and rooms == [1]
    assert [c.seed for c in got] == [seed_for(h.night, 101, 2, i, 0) for i in range(4)]
    assert sorted(p.name for p in out.iterdir()) == sorted(
        n for i in range(4) for n in (f"c{i}.jpg", f"c{i}.thumb.jpg"))


def test_a_second_oom_of_a_slot_propagates_and_three_flags_refuse_the_set(h, tmp_path):
    renderable_cast(h.store, 101)
    spec = build_prompt(load_persona(h.store, 101))
    out = tmp_path / "out"; out.mkdir()
    async def room(): pass
    async def go(**env):
        w = fake_worker(tmp_path, **env)()
        await w.start()
        try:
            return await render_candidates(w, spec, out, night=h.night, guid=101, set_no=1,
                                           cfg=SELFIE_CFG, make_room=room)
        finally:
            await w.stop()
    with pytest.raises(WorkerError, match="oom"):
        run(go(FAKE_SELFIE_OOM="2"))
    seeds = ",".join(str(seed_for(h.night, 101, 1, 0, a)) for a in range(TRIES))
    with pytest.raises(SetRefused):
        run(go(FAKE_SELFIE_FLAG_SEEDS=seeds))
    assert list(out.iterdir()) == []                       # a flagged image is never written


def test_an_image_scored_at_the_limit_is_flagged_whatever_the_worker_says(h, tmp_path):
    renderable_cast(h.store, 101)
    spec = build_prompt(load_persona(h.store, 101))
    out = tmp_path / "out"; out.mkdir()
    class Lax:                                              # a worker that wrote it anyway
        async def render(self, req, *, rid=None):
            req.out.write_bytes(JPEG); req.thumb.write_bytes(JPEG)
            return Rendered(False, SELFIE_CFG.nsfw_max, "unused", 1, 0.01, 10.5)
    async def room(): pass
    with pytest.raises(SetRefused):
        run(render_candidates(Lax(), spec, out, night=h.night, guid=101, set_no=1,
                              cfg=SELFIE_CFG, make_room=room))
    assert list(out.iterdir()) == []


# -- the budget and the logs ----------------------------------------------------------------------

def test_the_counter_and_the_degrade_step_cap_the_sets(h, monkeypatch):
    renderable_cast(h.store, 101, 102, 103)
    ensure_first_sets(h.store, [101], now=h.T)              # one queued already
    ctx = h.ctx()
    assert budget.COUNTERS[STAGE](ctx) == 3                  # 101's set, and 102 and 103 need one
    ctx.settings = replace(ctx.settings, selfie_per_night=2)
    assert budget.COUNTERS[STAGE](ctx) == 2
    assert UNIT_CALLS[STAGE] == {"json": 2.4} and STAGE in budget.EXTENSION_STAGES
    names = [step.name for step in DEGRADE]
    assert names[names.index("coach") + 1] == STAGE
    monkeypatch.setattr(budget, "COUNTERS", {STAGE: lambda c: c.settings.selfie_per_night})
    monkeypatch.setattr(budget, "night_budget_s", lambda *args: 30 * 48)
    plan = run(plan_night(h.ctx(), stages=frozenset({STAGE})))
    assert plan["steps"] == [STAGE] and plan["settings"] == {"selfie_per_night": 30}
    monkeypatch.setattr(budget, "night_budget_s", lambda *args: 100)
    tight = h.ctx()
    plan = run(plan_night(tight, stages=frozenset({STAGE})))
    assert plan["steps"][0] == STAGE and tight.settings.selfie_per_night == 10   # its floor
    plan = run(plan_night(h.ctx(), stages=frozenset()))     # not on this runner
    assert STAGE not in plan["steps"] and plan["units"] == {}


def test_logs_carry_no_prompt(h, tmp_path, caplog):
    renderable_cast(h.store, 101, 102)
    flag = seed_for(h.night, 101, 1, 0, 0)
    with caplog.at_level(logging.DEBUG):
        run(make_stage(SELFIE_CFG, worker=fake_worker(tmp_path, FAKE_SELFIE_FLAG_SEEDS=str(flag)))(h.ctx()))
    text = caplog.text
    assert "selfies_set_ready" in text and "selfies_flagged" in text and "selfies_queued" in text
    for guid in (101, 102):
        p = load_persona(h.store, guid)
        spec = build_prompt(p)
        for phrase, _ in spec.phrases + spec.negative:
            assert phrase not in text
        assert p.character_name not in text and p.identity.real_name not in text
