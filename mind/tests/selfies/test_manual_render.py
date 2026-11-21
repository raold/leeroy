"""His Render selfies button (selfies/manual.py): invented personas, fake clocks/admins and the
existing no-GPU worker protocol only."""
import asyncio
import json
import logging
from dataclasses import replace

import pytest

from conftest import run
from leeroy_mind.endpoint import BadRequest, ControlCfg, ControlEndpoint, selfie_args
from leeroy_mind.nightly.store import run_begin, run_finish
from leeroy_mind.personas.io import load_persona
from leeroy_mind.protocol import Login
from leeroy_mind.selfies import manual as manual_mod
from leeroy_mind.selfies.booth import STAGE as BOOTH_STAGE
from leeroy_mind.selfies.cli import _status
from leeroy_mind.selfies.files import staging_dir, sync_chosen
from leeroy_mind.selfies.job import WorkerError, pid_alive
from leeroy_mind.selfies.kind import _record
from leeroy_mind.selfies.manual import LAST, SNAPS_STAGE, STAGE, night_key
from leeroy_mind.selfies.scene import scene_from, scene_json
from leeroy_mind.selfies.scene_choices import compose_choices
from leeroy_mind.selfies.snaps import _COLS, _read, looks_hash
from leeroy_mind.selfies.store import (current, ensure_first_sets, pick, queue_more, queued_sets,
                                       ready_set)
from leeroy_mind.selfies.style import LUST
from leeroy_mind.selfies.worker import SelfieWorker
from nightly.conftest import FakeAdmin
from nightly.test_night_enrich import APPEARANCE

from .fakes import OWNER, fake_command, known_offline, mind_app, planned_snap, ready, until
from .test_photo_booth import MemoryWorker, finished_night
from .test_scene_choices import PICKS

ON = "[selfies]\nenabled=true\nsnaps=true\n"
SCENE = scene_json(scene_from(compose_choices(PICKS)))


def chosen_photo(app, guid, set_no=1):
    ready(app.store, app.data_dir, guid, set_no, now=1.0)
    pick(app.store, guid, set_no, 0, source="console", now=2.0)
    sync_chosen(app.data_dir, guid, current(app.store, guid))


def situation(app, guid):
    return planned_snap(app.store, guid, pick=current(app.store, guid).pick,
                        looks=looks_hash(APPEARANCE), scene=SCENE)


def snap_row(app, sid):
    return _read(app.store, f"SELECT {_COLS} FROM selfie_snap WHERE id=?", (sid,))[0]


class Order(MemoryWorker):
    """MemoryWorker noting each render's member and kind in one list shared by both workers."""
    def __init__(self, seen, kind, **kw):
        super().__init__(**kw)
        self.seen, self.kind = seen, kind

    async def render(self, req, *, rid=None):
        self.seen.append((self.kind, rid))
        return await super().render(req, rid=rid)


def setup_backlog(app):
    """101 needs a first set, 102 has a photo and four more queued, 103 a photo and a planned
    situational photo."""
    chosen_photo(app, 102)
    queue_more(app.store, 102, now=app.clock.now())
    chosen_photo(app, 103)
    return situation(app, 103)


@pytest.fixture
def app(tmp_path):
    app = mind_app(tmp_path, toml=ON)
    app.arbiter.admin = FakeAdmin([app.cfg.models.live, app.cfg.models.embedding])
    finished_night(app)
    known_offline(app)
    app.selfie_manual.worker = MemoryWorker
    app.selfie_manual.snap_worker = MemoryWorker
    yield app
    app.store.close()


async def finish(app):
    task = app.selfie_manual._task
    if task is not None:
        await asyncio.wait({task})


def day(app):
    return app.clock.local().date().isoformat()


def test_attached_with_selfies_on_without_the_booth(app):
    assert not hasattr(app, "selfie_booth")
    assert app.arbiter._preempt == app.selfie_manual.preempt
    assert app.selfie_manual._prior == app.night_scheduler.cancel_run
    assert app.night_scheduler.image_work() is False


def test_renders_first_then_more_then_snaps_under_its_own_receipts(app, caplog):
    seen = []
    app.selfie_manual.worker = lambda: Order(seen, "set")
    app.selfie_manual.snap_worker = lambda: Order(seen, "snap")
    sid = setup_backlog(app)

    async def go():
        before = app.selfie_manual.snapshot()["backlog"]
        assert {k: before[k] for k in ("first", "more", "snaps", "total", "images")} == \
            {"first": 1, "more": 1, "snaps": 1, "total": 3, "images": 9}
        reply = await app.selfie_manual.request()
        assert reply["ok"] and reply["render"]["running"]
        assert reply["render"]["progress"]["total"] == 3
        assert await app.selfie_manual._task == "done"
    with caplog.at_level(logging.INFO, logger="leeroy_mind"):
        run(go())
    # First sets, then "4 more", then the situational photo: the set's four slots each.
    order = [(kind, rid.split(":")[0].lstrip("s")) for kind, rid in seen]
    assert order == [("set", "101")] * 4 + [("set", "102")] * 4 + [("snap", str(sid))]
    assert ready_set(app.store, 101, 1) and ready_set(app.store, 102, 2)
    assert snap_row(app, sid).status == "ready"
    steps = app.store.conn.execute("SELECT night, stage, unit FROM night_step "
                                   "ORDER BY stage, unit").fetchall()
    assert {tuple(r) for r in steps} == {
        (night_key(day(app)), STAGE, "101:1"), (night_key(day(app)), STAGE, "102:2"),
        (night_key(day(app)), STAGE, "queue"), (night_key(day(app)), SNAPS_STAGE, f"snap:{sid}")}
    rec = json.loads(app.store.get_meta(LAST))
    assert (rec["outcome"], rec["sets"], rec["snaps"], rec["done"], rec["total"]) == ("done", 2, 1, 3, 3)
    stage = app.store.conn.execute("SELECT calls FROM night_stage WHERE night=? AND stage=?",
                                   (night_key(day(app)), STAGE)).fetchone()
    assert stage["calls"] == 9
    assert "private prompt sentinel" not in caplog.text
    events = {getattr(r, "leeroy_event", "") for r in caplog.records}
    assert {"selfies_manual_render_started", "selfies_manual_render_done"} <= events
    assert 'selfies_manual_render_started: total=3 kinds={"first":1,"more":1,"snaps":1}' in caplog.text
    done = next(r.getMessage() for r in caplog.records
                if getattr(r, "leeroy_event", "") == "selfies_manual_render_done")
    assert "done=3 failed=0 held=0" in done and "outcome=done" in done
    after = app.selfie_manual.snapshot()
    assert not after["running"] and after["code"] == "nothing" and after["backlog"]["total"] == 0


def test_receipts_never_touch_the_night_or_the_booth(tmp_path):
    from leeroy_mind.eval.audit import night_health
    from leeroy_mind.nightly.cli import _status as night_status
    app = mind_app(tmp_path, toml=ON + "booth=true\nbooth_sets_per_day=1\n")
    try:
        app.arbiter.admin = FakeAdmin([app.cfg.models.embedding])
        known_offline(app)
        app.selfie_manual.worker = app.selfie_manual.snap_worker = MemoryWorker
        app.selfie_booth.worker = MemoryWorker
        now, today = app.clock.now(), day(app)
        prior = "2026-10-28"
        run_begin(app.store, prior, now - 172800, now - 86400, now=now)
        run_finish(app.store, prior, now=now, status="done")
        run_begin(app.store, today, now - 3600, now, now=now)
        app.store.conn.execute("UPDATE night_run SET status='interrupted',finished=NULL,resumes=3 "
                               "WHERE night=?", (today,))
        app.store.conn.commit()
        chosen_photo(app, 102)
        queue_more(app.store, 102, now=now)

        async def go():
            assert (await app.selfie_manual.request())["ok"]  # no ten-minute wait, no request age
            await finish(app)
            assert ready_set(app.store, 101, 1) and ready_set(app.store, 102, 2)
            real = night_status(app.store, today)
            assert real["run"]["resumes"] == 3
            assert STAGE not in real["units"] and all(s["stage"] != STAGE for s in real["stages"])
            assert STAGE not in night_health(app.store, (0, app.clock.now() + 1))["stage_seconds"]
            status = _status(app.store, app.clock)
            assert status["booth"] == {} and STAGE not in status["stages"]
            assert not app.store.conn.execute("SELECT 1 FROM night_run WHERE night LIKE 'manual:%'").fetchone()
            # The booth's day allowance is its own: one more set renders there today.
            queue_more(app.store, 101, now=app.clock.now())
            await app.clock.advance(600, step=600)
            assert app.selfie_booth.problem() is None
            assert await (await app.selfie_booth.request()) == "done"
            assert ready_set(app.store, 101, 2)
        run(go())
    finally:
        app.store.close()


def test_held_sets_stay_queued_without_a_receipt(app):
    ensure_first_sets(app.store, [101], now=app.clock.now())
    p = load_persona(app.store, 101)
    _record(app.store, 101, LUST, p.photo)  # a lust kind on a member no longer lust-styled
    chosen_photo(app, 102)
    queue_more(app.store, 102, now=app.clock.now())

    async def go():
        backlog = app.selfie_manual.snapshot()["backlog"]
        assert (backlog["held"], backlog["first"], backlog["more"]) == (1, 1, 1)  # 103 first
        assert (await app.selfie_manual.request())["ok"]
        await finish(app)
    run(go())
    assert ready_set(app.store, 102, 2) and ready_set(app.store, 103, 1)
    assert not ready_set(app.store, 101, 1)
    assert [s.set_no for s in queued_sets(app.store) if s.guid == 101] == [1]
    assert not app.store.conn.execute("SELECT 1 FROM night_step WHERE unit LIKE '101:%'").fetchone()


def test_snaps_off_leaves_planned_photos_to_the_night(tmp_path):
    app = mind_app(tmp_path, toml="[selfies]\nenabled=true\n")
    try:
        app.arbiter.admin = FakeAdmin([])
        finished_night(app)
        known_offline(app)
        app.selfie_manual.worker = MemoryWorker
        chosen_photo(app, 103)
        sid = situation(app, 103)

        async def go():
            assert app.selfie_manual.snapshot()["backlog"]["snaps"] == 0
            assert (await app.selfie_manual.request())["ok"]  # 101 and 102 still need first sets
            await finish(app)
        run(go())
        assert snap_row(app, sid).status == "planned"
        assert ready_set(app.store, 101, 1)
    finally:
        app.store.close()


@pytest.mark.parametrize("case,code", [
    ("in_game", "in_game"), ("unknown", "presence_unknown"), ("offline_raid", "offline_raid"),
    ("voice", "voice"), ("paused", "gpu_paused"), ("night", "night"), ("running", "running"),
    ("off", "off"), ("nothing", "nothing"), ("answering", "answering"), ("request", "request")])
def test_refusals_in_words(app, case, code):
    async def go():
        m = app.selfie_manual
        if case == "in_game":
            app.night_scheduler.presence._say(True)
        if case == "unknown":
            app.hello = None
        if case == "offline_raid":
            app.arbiter.hold("offline_raid")
        if case == "voice":
            app.arbiter.hold("voice_gpu")
        if case == "paused":
            app.arbiter.paused = True
        if case == "night":
            app.night_scheduler._task = asyncio.get_running_loop().create_future()
        if case == "running":
            m._task = asyncio.get_running_loop().create_future()
        if case == "off":
            m.cfg = replace(m.cfg, enabled=False)
        if case == "nothing":
            for guid in (101, 102, 103):
                chosen_photo(app, guid)
        if case == "answering":
            m._answering = lambda within: True
        if case == "request":
            app.arbiter.hold("selfie_request:7")
        reply = await m.action("render")
        assert reply["ok"] is False and reply["error"] == code
        assert isinstance(reply["message"], str) and reply["message"]
        assert not app.arbiter.admin.calls
        assert not app.store.conn.execute("SELECT 1 FROM night_step WHERE stage=? AND unit<>'queue'",
                                          (STAGE,)).fetchone()
        if case != "running":
            assert not m.running
    run(go())


def test_booth_running_refuses_and_the_booth_waits_for_a_run(tmp_path):
    app = mind_app(tmp_path, toml=ON + "booth=true\n")
    try:
        app.arbiter.admin = FakeAdmin([])
        finished_night(app)
        known_offline(app)
        gate, rendering = asyncio.Event(), asyncio.Event()

        async def hold(req):
            rendering.set()
            await gate.wait()
        app.selfie_manual.worker = lambda: MemoryWorker(on_render=hold)

        async def go():
            await app.clock.advance(600, step=600)
            app.selfie_booth._task = asyncio.get_running_loop().create_future()
            assert (await app.selfie_manual.action("render"))["error"] == "booth"
            app.selfie_booth._task = None
            assert (await app.selfie_manual.request())["ok"]
            await rendering.wait()
            assert "Render selfies" in app.selfie_booth.problem()
            assert not app.selfie_booth._eligible()
            gate.set()
            await finish(app)
            assert "Render selfies" not in (app.selfie_booth.problem() or "")
        run(go())
    finally:
        app.store.close()


def test_no_night_falls_due_while_a_run_renders(app):
    gate = asyncio.Event()

    async def hold(req):
        await gate.wait()
    app.selfie_manual.worker = lambda: MemoryWorker(on_render=hold)

    async def go():
        app.store.conn.execute("DELETE FROM night_run")
        app.store.conn.commit()
        await app.clock.advance(3600, step=3600)
        assert app.night_scheduler.due() is not None
        assert (await app.selfie_manual.request())["ok"]
        assert app.night_scheduler.due() is None
        await app.night_scheduler.tick()
        assert app.night_scheduler._task is None
        gate.set()
        await finish(app)
        assert app.night_scheduler.due() is not None
    run(go())


def test_progress_rides_the_status_snapshot_and_stop_keeps_the_set_queued(app):
    chosen_photo(app, 102)
    queue_more(app.store, 102, now=app.clock.now())
    blocked = asyncio.Event()
    workers = []

    async def hold(req):
        if "102" in req.out.parts:
            blocked.set()
            await asyncio.Event().wait()

    def make():
        workers.append(MemoryWorker(on_render=hold))
        return workers[-1]
    app.selfie_manual.worker = make

    async def go():
        endpoint = ControlEndpoint(app, ControlCfg())
        assert (await endpoint.selfie({"action": "render"}))["ok"]
        await blocked.wait()
        status = endpoint.status()["selfie_render"]
        # First sets (101, 103) before the "4 more" (102), which blocks.
        assert status["running"] and status["progress"]["done"] == 2
        assert status["progress"]["total"] == 3 and status["progress"]["phase"] == "sets"
        assert status["progress"]["first"] == 2 and status["progress"]["more"] == 1
        reply = await endpoint.selfie({"action": "render_stop"})
        assert reply["ok"] and not reply["render"]["running"]
        assert reply["render"]["last"]["outcome"] == "stopped"
        assert reply["render"]["last"]["by"] == "owner"
        assert workers[0].stops == 1 and not workers[0].running
        assert ready_set(app.store, 101, 1) and not ready_set(app.store, 102, 2)
        assert [s.set_no for s in queued_sets(app.store) if s.guid == 102] == [2]
        assert staging_dir(app.data_dir, 102, 2).exists()  # left as the booth leaves it
        assert not (await endpoint.selfie({"action": "render_stop"}))["ok"]
        # The next run renders it: the stopped set has no receipt, its .part is cleared first.
        app.selfie_manual.worker = MemoryWorker
        assert (await endpoint.selfie({"action": "render"}))["ok"]
        await finish(app)
        assert ready_set(app.store, 102, 2) and not staging_dir(app.data_dir, 102, 2).exists()
    run(go())


def test_selfie_render_arguments_take_nothing_else():
    assert selfie_args({"action": "render"}) == {"action": "render"}
    assert selfie_args({"action": "render_stop"}) == {"action": "render_stop"}
    for body in ({"action": "render", "guid": 101}, {"action": "render_stop", "now": True}):
        with pytest.raises(BadRequest, match="takes no arguments"):
            selfie_args(body)


def test_status_has_no_selfie_render_with_selfies_off(tmp_path):
    app = mind_app(tmp_path, toml="")
    try:
        assert "selfie_render" not in ControlEndpoint(app, ControlCfg()).status()
        reply = run(ControlEndpoint(app, ControlCfg()).selfie({"action": "render"}))
        assert reply["ok"] is False
    finally:
        app.store.close()


def test_failed_renders_are_skipped_for_the_day_and_counted(app):
    chosen_photo(app, 102)
    chosen_photo(app, 103)
    w = MemoryWorker(flagged=True)
    app.selfie_manual.worker = lambda: w

    async def go():
        assert (await app.selfie_manual.request())["ok"]
        await finish(app)
        rec = json.loads(app.store.get_meta(LAST))
        assert (rec["outcome"], rec["refused"], rec["failed"]) == ("done", 1, 1)
        assert len(w.requests) == 3 and not ready_set(app.store, 101, 1)
        # The same seeds would be refused again today: the night tries it under its own key.
        reply = await app.selfie_manual.action("render")
        assert reply["error"] == "nothing"
        assert "1 set the photo check refused today" in reply["message"]
        app.selfie_manual.invalidate()
        assert app.selfie_manual.snapshot()["backlog"]["refused"] == 1
    run(go())


class Broken(MemoryWorker):
    """A worker whose renders end in a worker error (a timeout), never a refusal."""
    async def render(self, req, *, rid=None):
        self.requests.append(req)
        raise WorkerError("timeout")


def test_a_worker_error_is_not_skipped_for_the_day(app):
    chosen_photo(app, 102)
    chosen_photo(app, 103)
    app.selfie_manual.worker = Broken

    async def go():
        assert (await app.selfie_manual.request())["ok"]
        await finish(app)
        rec = json.loads(app.store.get_meta(LAST))
        assert (rec["outcome"], rec["refused"], rec["failed"], rec["sets"]) == ("failed", 0, 1, 0)
        assert not app.store.conn.execute("SELECT 1 FROM night_step WHERE stage=? AND unit='101:1'",
                                          (STAGE,)).fetchone()
        # Calls count images that came back: a failing pass measures no time an image.
        assert manual_mod.per_image_s(app.store.conn) is None
        app.selfie_manual.worker = MemoryWorker
        assert (await app.selfie_manual.request())["ok"]  # offered again the same day
        await finish(app)
        assert ready_set(app.store, 101, 1)
    run(go())


def test_estimate_reads_measured_passes(app):
    conn = app.store.conn
    assert manual_mod.per_image_s(conn) is None
    p = manual_mod.Plan(first=1, more=1, images=8)
    assert manual_mod.estimate(conn, p) is None  # no estimate yet, never 0
    conn.execute("INSERT INTO night_stage(night,stage,started,finished,calls,call_s) "
                 "VALUES('booth:2026-10-28',?,0,100,10,200)", (BOOTH_STAGE,))
    conn.commit()
    assert manual_mod.per_image_s(conn) == 20 and manual_mod.estimate(conn, p) == 160
    # A pass whose renders failed fast measures nothing.
    conn.execute("INSERT INTO night_stage(night,stage,started,finished,calls,call_s) "
                 "VALUES('manual:2026-10-29',?,0,200,10,0.5)", (STAGE,))
    conn.commit()
    assert manual_mod.per_image_s(conn) == 20
    assert manual_mod.estimate(conn, manual_mod.Plan()) is None  # nothing to render


# -- preemption: every live lease, the tree gone before the preload --------------------------------

def hanging(tmp_path, app):
    pids, requests = tmp_path / "pids.json", tmp_path / "requests.log"
    command, env = fake_command(tmp_path, FAKE_SELFIE_HANG="1", FAKE_SELFIE_CHILD="1",
                                FAKE_SELFIE_PIDS=str(pids), FAKE_SELFIE_LOG=str(requests))
    app.selfie_manual.worker = lambda: SelfieWorker(command, env=env)
    return pids, requests


@pytest.mark.parametrize("lease,by", [("human", "login"), ("discord", "discord"),
                                      ("offline_raid", "offline_raid"), ("voice_gpu", "voice")])
def test_every_live_lease_preempts_and_the_tree_ends_before_the_preload(app, tmp_path, lease, by):
    pids, requests = hanging(tmp_path, app)
    seen = []

    class Admin(FakeAdmin):
        async def preload(self, *args, **kwargs):
            row = json.loads(pids.read_text())
            seen.append((pid_alive(row["pid"]), pid_alive(row["child"])))
            await super().preload(*args, **kwargs)
    app.arbiter.admin = Admin([app.cfg.models.embedding])

    async def go():
        assert (await app.selfie_manual.request())["ok"]
        await until(requests.exists)
        if lease == "human":
            await app.night_scheduler.on_event(Login(ts=app.clock.now(), guid=OWNER, human=True))
            await app.night_scheduler.handback()
        elif lease == "discord":
            app.discord_talk._take_lease()  # his post
            await app.discord_talk.handback()
        else:
            assert app.arbiter.hold(lease)
            await app.arbiter.hand_over()
        assert app.selfie_manual._task.cancelled()
        assert seen == [(False, False)]
        rec = json.loads(app.store.get_meta(LAST))
        assert (rec["outcome"], rec["by"]) == ("preempted", by)
        assert not app.store.conn.execute("SELECT 1 FROM selfie_set WHERE status='ready'").fetchone()
        assert [s.guid for s in queued_sets(app.store)] == [101, 102, 103]
        if lease == "discord":
            await app.discord_talk.close()
    run(go())


# -- a run a lease preempted resumes by itself ----------------------------------------------------

async def preempted_by(app, lease):
    """A run the `lease` stopped at its first step, the lease then ended (call setup_backlog
    first: a run needs something to render)."""
    m = app.selfie_manual
    assert (await m.request())["ok"]
    app.arbiter.hold(lease)
    await finish(app)
    assert json.loads(app.store.get_meta(LAST))["outcome"] == "preempted"
    await app.arbiter.release_live(lease)
    return m


def test_a_run_a_lease_stopped_resumes_once_the_gpu_is_free_and_waits_out_retries(app, caplog):
    caplog.set_level(logging.INFO, logger="leeroy_mind")

    async def go():
        m = app.selfie_manual
        assert (await m.request())["ok"]
        app.arbiter.hold("offline_raid")  # a raid's start, refused by native moments later
        await finish(app)
        assert m.resume_pending
        await m.tick()
        assert not m.running  # the raid still holds the GPU
        await app.arbiter.release_live("offline_raid")
        await m.tick()
        assert not m.running  # too soon: the raid's own retry would preempt it again
        await app.clock.advance(manual_mod.RESUME_AFTER_S)
        await m.tick()
        assert m.running and not m.resume_pending
        await finish(app)
        rec = json.loads(app.store.get_meta(LAST))
        assert rec["outcome"] == "done" and not m.resume_pending
        assert not queued_sets(app.store)
    setup_backlog(app)
    run(go())
    assert any(r.getMessage().startswith("selfies_manual_render_resumed") for r in caplog.records)


@pytest.mark.parametrize("stop", ["owner", "gpu_pause", "shutdown"])
def test_his_stop_free_gpu_and_shutdown_never_resume(app, tmp_path, stop):
    hanging(tmp_path, app)

    async def go():
        m = app.selfie_manual
        assert (await m.request())["ok"]
        await asyncio.sleep(0)
        await m.cancel(stop)
        assert not m.resume_pending
    setup_backlog(app)
    run(go())


def test_a_pause_or_an_empty_backlog_drops_a_pending_resume(app):
    async def go():
        m = await preempted_by(app, "voice_gpu")
        assert m.resume_pending
        app.arbiter.paused = True
        await app.clock.advance(manual_mod.RESUME_AFTER_S)
        await m.tick()
        assert not m.running and not m.resume_pending  # he took the GPU for himself
    setup_backlog(app)
    run(go())


def test_his_stop_with_nothing_running_cancels_a_pending_resume(app):
    async def go():
        m = await preempted_by(app, "voice_gpu")
        answer = await m.stop_now()
        assert answer["ok"] and not m.resume_pending
        await app.clock.advance(manual_mod.RESUME_AFTER_S)
        await m.tick()
        assert not m.running
    setup_backlog(app)
    run(go())


def test_resumes_are_capped_a_day(app):
    async def go():
        m = app.selfie_manual
        for _ in range(manual_mod.MAX_RESUMES):
            m = await preempted_by(app, "voice_gpu") if not m.resume_pending else m
            await app.clock.advance(manual_mod.RESUME_AFTER_S)
            await m.tick()
            assert m.running
            app.arbiter.hold("voice_gpu")
            await finish(app)
            await app.arbiter.release_live("voice_gpu")
        await app.clock.advance(manual_mod.RESUME_AFTER_S)
        assert not m.resume_pending
        await m.tick()
        assert not m.running
    setup_backlog(app)
    run(go())


def test_a_resume_left_from_yesterday_is_dropped(app):
    async def go():
        m = await preempted_by(app, "voice_gpu")
        await app.clock.advance(86400, step=3600)
        await m.tick()
        assert not m.running and not m.resume_pending
    setup_backlog(app)
    run(go())


def test_the_resume_loop_is_attached_with_selfies_on(app):
    assert app.selfie_manual.run_forever in app._background


def test_a_lease_arriving_after_the_request_never_starts_the_worker(app):
    async def go():
        assert (await app.selfie_manual.request())["ok"]
        app.arbiter.hold("voice_gpu")
        await finish(app)
        rec = json.loads(app.store.get_meta(LAST))
        assert (rec["outcome"], rec["by"]) == ("preempted", "voice")
        assert not app.arbiter.admin.calls
    run(go())


def test_presence_leases_are_released_and_free_gpu_stops_the_run(app, tmp_path):
    pids, requests = hanging(tmp_path, app)
    unloaded = []
    original = app.arbiter.admin.unload

    async def unload(model):
        if requests.exists():
            row = json.loads(pids.read_text())
            unloaded.append((pid_alive(row["pid"]), pid_alive(row["child"])))
        await original(model)
    app.arbiter.admin.unload = unload

    async def go():
        app.discord_talk._take_lease()
        await app.discord_talk.handback()
        assert app.arbiter.holders == {"discord"}
        assert (await app.selfie_manual.request())["ok"]
        await until(requests.exists)
        assert not app.arbiter.holders  # Discord's presence lease was let go for the run
        reply = await app.gpu.action({"action": "pause"})
        assert reply["ok"] and app.selfie_manual._task.cancelled()
        assert unloaded and all(pair == (False, False) for pair in unloaded)
        rec = json.loads(app.store.get_meta(LAST))
        assert (rec["outcome"], rec["by"]) == ("preempted", "gpu_pause")
        await app.discord_talk.close()
    run(go())


def test_photo_request_borrow_is_busy_while_a_run_renders(app):
    from leeroy_mind.nightly.wiring import enable_borrow
    gate = asyncio.Event()

    async def hold(req):
        await gate.wait()
    app.selfie_manual.worker = lambda: MemoryWorker(on_render=hold)
    enable_borrow(app)

    async def go():
        assert (await app.selfie_manual.request())["ok"]
        assert app.arbiter.borrow_busy()
        gate.set()
        await finish(app)
        assert not app.arbiter.borrow_busy()
    run(go())


def test_server_lease_is_let_go_for_the_run_and_taken_back_after(tmp_path):
    from datetime import datetime, timezone
    app = mind_app(tmp_path, toml=ON + "[server]\nenabled=true\n")
    try:
        app.clock._t = datetime(2026, 11, 13, 12, tzinfo=timezone.utc).timestamp()
        app.arbiter.admin = FakeAdmin([app.cfg.models.live, app.cfg.models.embedding])
        known_offline(app)
        gate, rendering = asyncio.Event(), asyncio.Event()

        async def hold(req):
            rendering.set()
            await gate.wait()
        app.selfie_manual.worker = lambda: MemoryWorker(on_render=hold)

        async def go():
            lease = app.server.lease
            await lease.tick(app.clock.now())
            await lease.handback()
            assert app.arbiter.holders == {"server"}
            assert (await app.selfie_manual.request())["ok"]
            await rendering.wait()
            assert not lease.held and not app.arbiter.holders
            assert ("unload", app.cfg.models.live) in app.arbiter.admin.calls
            await lease.tick(app.clock.now())  # its own tick while the run renders
            assert not lease.held and not app.arbiter.holders
            gate.set()
            await finish(app)
            while app.selfie_manual._lease_tasks:
                await asyncio.wait(set(app.selfie_manual._lease_tasks))
            await lease.handback()
            assert lease.held and app.arbiter.holders == {"server"}
            # The hand-over after the worker preloads the live model again.
            assert app.arbiter.admin.calls[-1][:2] == ("preload", app.cfg.models.live)
        run(go())
    finally:
        app.store.close()


def test_server_lease_stays_released_while_a_run_renders():
    from types import SimpleNamespace
    from server.test_server_lease import fixture
    app, lease = fixture()
    app.selfie_manual = SimpleNamespace(running=True)

    async def go():
        await lease.tick(app.clock.now())
        assert not lease.held and not app.arbiter.hold_calls
        app.selfie_manual.running = False
        await lease.tick(app.clock.now())
        assert lease.held and app.arbiter.hold_calls == ["server"]
        app.selfie_manual.running = True
        await lease.tick(app.clock.now())
        assert not lease.held and not app.arbiter.holders
    run(go())


# -- fix round 1: admission, attribution, owed answers, the night's window -------------------------

@pytest.mark.parametrize("reason,by", [("post", "discord"), ("answer", "discord_answer"),
                                       ("group", "discord_group")])
def test_a_discord_lease_taken_between_the_click_and_the_first_step_stops_the_run(app, reason, by):
    """Admission lets go of the presence leases; a lease taken after it (his post, an owed
    answer's rescue, a group DM tick) is kept and stops the run, never dropped by it."""
    seen = []
    app.selfie_manual.worker = lambda: Order(seen, "set")

    async def go():
        app.discord_talk.take_lease("post")  # an earlier post: its lease is let go at admission
        await app.discord_talk.handback()
        assert (await app.selfie_manual.request())["ok"]
        assert not app.arbiter.holders
        app.discord_talk.take_lease(reason)  # the same loop pass as the click
        await finish(app)
        await app.discord_talk.handback()
        rec = json.loads(app.store.get_meta(LAST))
        assert (rec["outcome"], rec["by"], rec["done"]) == ("preempted", by, 0)
        assert app.arbiter.holders == {"discord"} and app.discord_talk._held
        assert not seen  # the worker never started
        assert app.arbiter.admin.calls[-1][:2] == ("preload", app.cfg.models.live)
        await app.discord_talk.close()
    run(go())


def test_a_cancel_before_the_first_step_leaves_a_record_and_a_log_line(app, caplog):
    async def go():
        assert (await app.selfie_manual.request())["ok"]
        app.selfie_manual.stop("owner")  # before the task's first step
        await finish(app)
        rec = json.loads(app.store.get_meta(LAST))
        assert (rec["outcome"], rec["by"], rec["done"], rec["total"]) == ("stopped", "owner", 0, 3)
        assert app.selfie_manual.snapshot()["last"] == rec
        assert not app.arbiter.admin.calls
    with caplog.at_level(logging.INFO, logger="leeroy_mind"):
        run(go())
    assert any(getattr(r, "leeroy_event", "") == "selfies_manual_render_done" for r in caplog.records)


def test_answers_owed_on_discord_within_the_run_hold_a_start(app):
    asked = []

    def due(within):
        asked.append(within)
        return 1
    app.discord_talk.answers_due = due

    async def go():
        reply = await app.selfie_manual.request()
        assert reply["error"] == "answering" and "owe you answers" in reply["message"]
        assert manual_mod.ANSWER_HORIZON_S in asked  # no estimate yet: the horizon
        app.selfie_manual.invalidate()
        assert app.selfie_manual.snapshot()["code"] == "answering"
        app.discord_talk.answers_due = lambda within: 0
        assert (await app.selfie_manual.request())["ok"]
        await finish(app)
    run(go())


def test_deferred_dm_and_group_dm_answers_count_by_when_they_fall_due(app):
    from types import SimpleNamespace
    from leeroy_mind.groups.service import Groups
    talk = app.discord_talk
    app.groups = None
    talk.own_time = lambda: True
    talk._owed = lambda: [(101, 7, app.clock.now() - 60), (102, 8, app.clock.now() - 60)]
    talk._due_of = lambda guid, mid, ts: app.clock.now() + (100 if guid == 101 else 5000)
    assert talk.answers_due(600) == 1 and talk.answers_due(6000) == 2
    talk.own_time = lambda: False  # with the switch off they go at once
    assert talk.answers_due(0) == 2
    groups = Groups.__new__(Groups)
    groups.app, groups.config = app, SimpleNamespace(enabled=True)
    with app.store.transaction():
        app.store.set_meta("groups:owed:41", json.dumps({
            "101": {"due": app.clock.now() + 50, "urgent": False},
            "102": {"due": app.clock.now() + 9000, "urgent": False},
            "103": {"due": app.clock.now() + 9000, "urgent": True}}))
    assert groups.answers_due(app.clock.now() + 600) == 2
    app.groups = groups
    assert talk.answers_due(0) == 3  # two DMs at once, and the urgent group answer
    groups.config = SimpleNamespace(enabled=False)
    assert groups.answers_due(app.clock.now() + 600) == 0


def window_app(tmp_path, hour, minute=0):
    from datetime import datetime, timezone
    app = mind_app(tmp_path, toml=ON + "[server]\nenabled=true\n")
    app.clock._t = datetime(2026, 10, 30, hour, minute, tzinfo=timezone.utc).timestamp()
    app.arbiter.admin = FakeAdmin([app.cfg.models.embedding])
    known_offline(app)
    app.selfie_manual.worker = app.selfie_manual.snap_worker = MemoryWorker
    # 60 s an image from an earlier booth pass: three first sets are about 12 minutes
    app.store.conn.execute("INSERT INTO night_stage(night,stage,started,finished,calls,call_s) "
                           "VALUES('booth:2026-10-29',?,0,100,10,600)", (BOOTH_STAGE,))
    app.store.conn.commit()
    return app


@pytest.mark.parametrize("minute,refused", [(50, True), (40, False)])
def test_a_run_longer_than_the_time_before_the_nights_window_is_refused(tmp_path, minute, refused):
    app = window_app(tmp_path, 2, minute)
    try:
        async def go():
            reply = await app.selfie_manual.request()
            if refused:
                assert reply["error"] == "night_window"
                assert "about 12 minutes, longer than the 10 minutes left" in reply["message"]
                assert "opens at 03:00" in reply["message"]
            else:
                assert reply["ok"]
                await finish(app)
        run(go())
    finally:
        app.store.close()


def test_inside_the_window_no_run_starts_before_tonights_night_has_finished(tmp_path):
    from leeroy_mind.nightly.scheduler import tonight
    app = window_app(tmp_path, 4)
    try:
        async def go():
            reply = await app.selfie_manual.request()
            assert reply["error"] == "night_window" and "window is open (03:00-08:00)" in reply["message"]
            night = tonight(app.clock.local(), app.night_scheduler.settings)
            now = app.clock.now()
            run_begin(app.store, night, now - 3600, now, now=now)
            run_finish(app.store, night, now=now, status="done")
            assert (await app.selfie_manual.request())["ok"]
            await finish(app)
        run(go())
    finally:
        app.store.close()


def test_measured_photos_count_only_once_published(app, monkeypatch):
    """[selfies] critic: a measured photo is not a rendered one until _publish says so."""
    from collections import Counter
    from leeroy_mind.selfies import snapstage
    for guid in (101, 102, 103):
        chosen_photo(app, guid)
    situation(app, 102)
    situation(app, 103)

    async def measured(ctx, cfg, worker, rows, tally=None):
        tally["measured"] += len(rows)
        return list(rows)

    async def publish(ctx, cfg, pending):
        return Counter(ready=1, held=1)
    monkeypatch.setattr(snapstage, "_render", measured)
    monkeypatch.setattr(snapstage, "_publish", publish)

    async def go():
        assert (await app.selfie_manual.request())["ok"]
        await finish(app)
        rec = json.loads(app.store.get_meta(LAST))
        assert (rec["outcome"], rec["snaps"], rec["held"], rec["done"]) == ("done", 1, 1, 2)
    run(go())
