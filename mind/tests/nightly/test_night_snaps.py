"""The night's snaps stage (P4 Task 8): situational photos and guild photos rendered by the fake
identity worker (no torch, no model, no GPU) under the night's arbiter on a FakeAdmin. Every
person and scene here is invented."""
import asyncio
import json
import logging
from dataclasses import replace

import pytest

from conftest import TEST_CAST, run
from leeroy_mind.nightly import budget
from leeroy_mind.nightly.budget import DEGRADE, EXTENSION_STAGES, UNIT_CALLS
from leeroy_mind.nightly.gpu import LiveLeaseHeld
from leeroy_mind.nightly.runner import NightRunner, Stage
from leeroy_mind.nightly.store import run_begin, run_get
from leeroy_mind.personas.io import load_persona, save_persona
from leeroy_mind.selfies.config import SelfieCfg
from leeroy_mind.selfies.files import guild_photo, guild_tiles, history, snap, sync_chosen
from leeroy_mind.selfies.job import pid_alive
from leeroy_mind.selfies.scene import build_scene_prompt, build_tile_prompt, ref_crop, scene_from
from leeroy_mind.selfies.snapstage import (GUILD_THUMB_PX, GUTTER_PX, SNAP_THUMB_PX, STAGE,
                                           TILE_PX, make_snaps_stage, wants_gpu)
from leeroy_mind.selfies.snaps import _COLS, _read, looks_hash
from leeroy_mind.selfies.stage import CONTENDED, TRIES, seed_for
from leeroy_mind.selfies.store import current, pick
from leeroy_mind.selfies.worker import Identity, SelfieWorker
from leeroy_mind.wow import CLASSES, RACES
from nightly.conftest import FakeAdmin
from nightly.test_night_enrich import APPEARANCE
from selfies.fakes import JPEG, fake_command, fake_worker, planned_snap, ready, renderable_cast, until

SNAPS_CFG = SelfieCfg(enabled=True, snaps=True)
SCENE = {"kind": "snapshot", "framing": "upper_body", "setting": "on a wooden pier by the harbour",
         "activity": "throwing a ball for a scruffy terrier",
         "outfit": "a yellow raincoat over a grey jumper", "expression": "laughing",
         "when": "day", "audience": "anyone",
         "shows": "Them on a wooden pier throwing a ball for their dog."}
IDENTITY = {"FAKE_SELFIE_IDENTITY": "1"}


def snap_row(store, sid):
    return _read(store, f"SELECT {_COLS} FROM selfie_snap WHERE id=?", (sid,))[0]


def chosen_photo(h, guid, set_no=1):
    """Set `set_no` ready and its first photo picked, with history/<pick>.jpg and chosen.jpg
    written as the pick service writes them."""
    ready(h.store, h.settings.data_dir, guid, set_no, now=1.0)
    pick(h.store, guid, set_no, 0, source="console", now=2.0)
    sync_chosen(h.settings.data_dir, guid, current(h.store, guid))


def repick(h, guid):
    """The owner chose another photo after the plan: a new set's first photo."""
    with h.store.transaction():
        n = h.store.conn.execute("SELECT max(set_no) FROM selfie_set WHERE guid=?",
                                 (guid,)).fetchone()[0]
    chosen_photo(h, guid, n + 1)


def cast(h, *guids):
    renderable_cast(h.store, *guids)
    for g in guids:
        chosen_photo(h, g)


def situation(h, guid, *, scene=SCENE, **over):
    """A situational photo planned from the member's photo in force and looks."""
    return planned_snap(h.store, guid, pick=current(h.store, guid).pick,
                        looks=looks_hash(APPEARANCE), scene=scene, **over)


def guild(h, *guids, **over):
    return planned_snap(h.store, 0, kind="guild", source="raid", ref=over.pop("ref", "raid:1"),
                        scene={"shows": "The raid on a video call"}, pick=None, looks=None,
                        members=[(g, current(h.store, g).pick) for g in guids], **over)


def lines(log):
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


def renders(log):
    """(request id, seed, identity reference) for each render the fake was sent."""
    out = []
    for line in lines(log):
        head, _, ref = line.partition(" identity ")
        rid, seed, *_ = head.split()
        if seed != "grid":
            out.append((rid, int(seed), ref))
    return out


def watching(tmp_path, seen, **env):
    """The fake identity worker, keeping every RenderRequest it is sent in `seen`."""
    cmd, environ = fake_command(tmp_path, **IDENTITY, **env)

    class Seen(SelfieWorker):
        async def render(self, req, *, rid=None):
            seen.append(req)
            return await super().render(req, rid=rid)
    return lambda: Seen(cmd, env=environ)


def stage(tmp_path, **env):
    return make_snaps_stage(SNAPS_CFG, worker=fake_worker(tmp_path, **IDENTITY, **env))


# -- Review Focus 2: a render always uses history/<pick>.jpg and never a stale photo -------------

def test_a_snap_whose_photo_changed_is_expired_not_rendered(h, tmp_path):
    renderable_cast(h.store, 101); chosen_photo(h, 101); s = planned_snap(h.store, 101, pick=current(h.store, 101).pick)
    repick(h, 101)                                   # the owner chose another photo after the plan
    log, starts = tmp_path / "requests.log", tmp_path / "starts.log"
    ctx = h.ctx()
    run(make_snaps_stage(SNAPS_CFG, worker=fake_worker(tmp_path, FAKE_SELFIE_IDENTITY="1", FAKE_SELFIE_LOG=str(log),
                                                       FAKE_SELFIE_STARTS=str(starts)))(ctx))
    assert snap_row(h.store, s).ended == "photo_changed" and (not log.exists() or "render" not in log.read_text())
    assert lines(log) == [] and not starts.exists()             # nothing to render: no worker at all
    assert ctx.done(STAGE, f"snap:{s}") and not ctx.failures


@pytest.mark.parametrize("why", ["looks_changed", "missing"])
def test_a_snap_whose_looks_changed_or_whose_reference_is_gone_is_expired_not_rendered(h, tmp_path, why):
    cast(h, 101)
    s = situation(h, 101)
    if why == "looks_changed":
        _, race, cls = TEST_CAST[101]
        save_persona(h.store, replace(load_persona(h.store, 101),
                                      appearance=replace(APPEARANCE, hair_colour="silver")),
                     source="seed", ts=2000.0, race=RACES[race], class_name=CLASSES[cls])
    else:
        history(h.settings.data_dir, 101, current(h.store, 101).pick).unlink()
    log = tmp_path / "requests.log"
    ctx = h.ctx()
    run(stage(tmp_path, FAKE_SELFIE_LOG=str(log))(ctx))
    row = snap_row(h.store, s)
    assert (row.status, row.ended) == ("expired", why) and lines(log) == []
    assert ctx.done(STAGE, f"snap:{s}") and not ctx.failures


def test_a_planned_snap_is_rendered_with_its_identity_reference_as_a_portrait(h, tmp_path):
    cast(h, 101); s = situation(h, 101)
    data, log, seen = h.settings.data_dir, tmp_path / "requests.log", []
    ctx = h.ctx()
    run(make_snaps_stage(SNAPS_CFG, worker=watching(tmp_path, seen, FAKE_SELFIE_LOG=str(log)))(ctx))
    ref = history(data, 101, current(h.store, 101).pick)
    seed = seed_for(ctx.night, 101, s, 0, 0)
    assert renders(log) == [(f"s{s}:0", seed, str(ref))]
    assert "chosen.jpg" not in log.read_text()
    (req,) = seen
    assert (req.shape, req.size, req.thumb_size, req.steps, req.cfg, req.nsfw_max) == (
        "portrait", 1024, SNAP_THUMB_PX, SNAPS_CFG.steps, SNAPS_CFG.guidance, SNAPS_CFG.nsfw_max)
    assert req.identity == Identity(ref, ref_crop("upper_body"), SNAPS_CFG.identity_scale)
    p = load_persona(h.store, 101)
    spec = build_scene_prompt(p, scene_from(SCENE))
    assert (req.phrases, req.negative) == (spec.phrases, spec.negative)
    assert (req.out, req.thumb) == (snap(data, 101, s), snap(data, 101, s, thumb=True))
    row = snap_row(h.store, s)
    assert row.status == "ready" and (row.width, row.height) == (832, 1216)
    assert (row.seed, row.tries, row.nsfw) == (seed, 1, 0.01)
    assert row.prompt == ", ".join(t for t, _ in spec.phrases)       # what was rendered
    assert snap(data, 101, s).read_bytes() == JPEG and snap(data, 101, s, thumb=True).read_bytes() == JPEG
    assert ctx.done(STAGE, f"snap:{s}") and not ctx.failures


def test_a_photo_changed_during_its_render_expires_the_snap_and_keeps_its_files(h, tmp_path):
    cast(h, 101); s = situation(h, 101)
    cmd, env = fake_command(tmp_path, **IDENTITY)

    class Moved(SelfieWorker):
        async def render(self, req, *, rid=None):
            got = await super().render(req, rid=rid)
            repick(h, 101)                               # his choice, from the console, mid-render
            return got
    ctx = h.ctx()
    run(make_snaps_stage(SNAPS_CFG, worker=lambda: Moved(cmd, env=env))(ctx))
    row = snap_row(h.store, s)
    assert (row.status, row.ended) == ("expired", "photo_changed") and row.rendered is None
    assert snap(h.settings.data_dir, 101, s).exists()   # R9: an expired snap keeps its files
    assert ctx.done(STAGE, f"snap:{s}") and not ctx.failures


def test_a_lust_scene_waits_while_she_lacks_the_lust_style(h, tmp_path, caplog):
    cast(h, 101); s = situation(h, 101, scene={**SCENE, "style": "lust"})
    log, starts = tmp_path / "requests.log", tmp_path / "starts.log"
    ctx = h.ctx()
    assert wants_gpu(ctx) is False
    with caplog.at_level(logging.INFO, logger="leeroy_mind"):
        run(stage(tmp_path, FAKE_SELFIE_LOG=str(log), FAKE_SELFIE_STARTS=str(starts))(ctx))
    assert snap_row(h.store, s).status == "planned" and lines(log) == [] and not starts.exists()
    assert f"selfies_snap_held: night=2026-10-29 snap={s} reason=restyle" in caplog.text
    assert ctx.done(STAGE, f"snap:{s}") and not ctx.failures


def test_three_flags_expire_only_that_snap(h, tmp_path, caplog):
    cast(h, 101, 102); a = situation(h, 101); b = situation(h, 102)
    flagged = ",".join(str(seed_for(h.night, 101, a, 0, t)) for t in range(TRIES))
    ctx = h.ctx()
    with caplog.at_level(logging.INFO, logger="leeroy_mind"):
        run(stage(tmp_path, FAKE_SELFIE_FLAG_SEEDS=flagged)(ctx))
    assert (snap_row(h.store, a).status, snap_row(h.store, a).ended) == ("expired", "flagged")
    assert snap_row(h.store, b).status == "ready"                      # the next one is rendered
    assert not snap(h.settings.data_dir, 101, a).exists()               # a flagged image is never written
    assert ctx.done(STAGE, f"snap:{a}") and not ctx.failures
    assert caplog.text.count("selfies_flagged") == TRIES
    assert f"selfies_snap_expired: night=2026-10-29 snap={a} reason=flagged" in caplog.text


# -- the guild photo ----------------------------------------------------------------------------

def test_a_guild_photo_renders_each_member_alone_then_one_grid(h, tmp_path):
    cast(h, 101, 102, 103); g = guild(h, 101, 102, 103)
    data, log, seen = h.settings.data_dir, tmp_path / "requests.log", []
    ctx = h.ctx()
    run(make_snaps_stage(SNAPS_CFG, worker=watching(tmp_path, seen, FAKE_SELFIE_LOG=str(log)))(ctx))
    picks = {m: current(h.store, m).pick for m in (101, 102, 103)}
    assert lines(log)[-1] == f"g{g}:grid grid 3"                      # n tiles, then one grid
    assert renders(log) == [(f"g{g}:{i}:0", seed_for(ctx.night, m, g, i, 0), str(history(data, m, picks[m])))
                            for i, m in enumerate((101, 102, 103))]
    tiles = guild_tiles(data, g)
    for i, (m, req) in enumerate(zip((101, 102, 103), seen)):
        spec = build_tile_prompt(load_persona(h.store, m))
        assert (req.phrases, req.negative, req.shape, req.size, req.thumb_size) == (
            spec.phrases, spec.negative, "square", 1024, 128)
        assert req.identity == Identity(history(data, m, picks[m]), ref_crop("upper_body"),
                                        SNAPS_CFG.identity_scale)
        assert (req.out, req.thumb) == (tiles / f"t{i}.jpg", tiles / f"t{i}.thumb.jpg")
    row = snap_row(h.store, g)
    assert row.status == "ready" and (row.width, row.height) == (1048, 1048)   # 2 x 2 at 512 + 8
    assert row.members == tuple((m, picks[m]) for m in (101, 102, 103))
    assert (row.tries, row.nsfw) == (3, 0.01)
    assert guild_photo(data, g).read_bytes() == JPEG and guild_photo(data, g, thumb=True).exists()
    assert not tiles.exists() and ctx.done(STAGE, f"snap:{g}") and not ctx.failures
    assert (TILE_PX, GUTTER_PX, GUILD_THUMB_PX, SNAP_THUMB_PX, STAGE) == (512, 8, 512, 384, "snaps")


def test_a_guild_photo_leaves_out_a_member_whose_photo_changed(h, tmp_path, caplog):
    cast(h, 101, 102, 103, 104); g = guild(h, 101, 102, 103, 104)
    repick(h, 102)
    data, log = h.settings.data_dir, tmp_path / "requests.log"
    ctx = h.ctx()
    run(stage(tmp_path, FAKE_SELFIE_LOG=str(log))(ctx))
    assert [rid for rid, _, _ in renders(log)] == [f"g{g}:0:0", f"g{g}:2:0", f"g{g}:3:0"]
    row = snap_row(h.store, g)
    assert row.status == "ready" and [m for m, _ in row.members] == [101, 103, 104]
    # fewer than three left: it ends, its files (none yet) untouched
    g2 = guild(h, 101, 103, 104, ref="raid:2")
    repick(h, 103)
    with caplog.at_level(logging.INFO, logger="leeroy_mind"):
        run(stage(tmp_path, FAKE_SELFIE_LOG=str(log))(ctx))
    assert (snap_row(h.store, g2).status, snap_row(h.store, g2).ended) == ("expired", "photo_changed")
    assert not any(rid.startswith(f"g{g2}:") for rid, _, _ in renders(log))
    assert not guild_tiles(data, g2).exists() and not ctx.failures
    assert f"selfies_snap_expired: night=2026-10-29 snap={g2} reason=photo_changed" in caplog.text


def test_a_member_who_changes_photo_mid_guild_photo_is_left_out_of_it(h, tmp_path):
    cast(h, 101, 102, 103, 104); g = guild(h, 101, 102, 103, 104)
    log = tmp_path / "requests.log"
    cmd, env = fake_command(tmp_path, **IDENTITY, FAKE_SELFIE_LOG=str(log))

    class Moved(SelfieWorker):
        async def render(self, req, *, rid=None):
            if rid == f"g{g}:1:0":
                repick(h, 103)                           # before her own tile is reached
            return await super().render(req, rid=rid)
    run(make_snaps_stage(SNAPS_CFG, worker=lambda: Moved(cmd, env=env))(h.ctx()))
    assert [m for m, _ in snap_row(h.store, g).members] == [101, 102, 104]
    assert [rid for rid, _, _ in renders(log)] == [f"g{g}:0:0", f"g{g}:1:0", f"g{g}:3:0"]


def test_a_photo_changed_during_the_grid_leaves_the_guild_photo_for_a_later_night(h, tmp_path):
    cast(h, 101, 102, 103, 104); g = guild(h, 101, 102, 103, 104)
    cmd, env = fake_command(tmp_path, **IDENTITY)

    class Moved(SelfieWorker):
        async def grid(self, tiles, **kw):
            got = await super().grid(tiles, **kw)
            repick(h, 102)                               # after the tiles, before the commit
            return got
    ctx = h.ctx()
    run(make_snaps_stage(SNAPS_CFG, worker=lambda: Moved(cmd, env=env))(ctx))
    assert snap_row(h.store, g).status == "planned" and ctx.done(STAGE, f"snap:{g}") and not ctx.failures
    log = tmp_path / "again.log"
    run(stage(tmp_path, FAKE_SELFIE_LOG=str(log))(h.ctx(night="2026-10-30")))
    row = snap_row(h.store, g)
    assert row.status == "ready" and [m for m, _ in row.members] == [101, 103, 104]


def test_his_login_mid_guild_photo_leaves_no_partial_photo_and_resumes_next_night(h, tmp_path):
    cast(h, 101, 102, 103); g = guild(h, 101, 102, 103)
    data = h.settings.data_dir
    pids, log = tmp_path / "pids.json", tmp_path / "requests.log"; seen, stops = {}, []

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
    cmd, env = fake_command(tmp_path, **IDENTITY, FAKE_SELFIE_HANG_AFTER="2", FAKE_SELFIE_CHILD="1",
                            FAKE_SELFIE_PIDS=str(pids), FAKE_SELFIE_LOG=str(log))
    snaps = make_snaps_stage(SNAPS_CFG, worker=lambda: Watched(cmd, env=env))

    async def go():
        task = asyncio.create_task(snaps(ctx))
        await until(lambda: f"g{g}:2:0" in "\n".join(lines(log)))   # the third tile hangs
        async def preempt():
            task.cancel(); await asyncio.wait({task})
        ctx.arbiter.set_preempt(preempt)
        ctx.arbiter.hold("human")
        await ctx.arbiter.hand_over()
        assert task.cancelled()
    run(go())
    assert seen["alive"] == (False, False) and not guild_photo(h.settings.data_dir, g).exists()
    assert seen["stops"] == ["done"] and ("preload", h.LIVE, -1) in ctx.arbiter.admin.calls
    assert snap_row(h.store, g).status == "planned" and not ctx.done(STAGE, f"snap:{g}") and not ctx.failures
    run(make_snaps_stage(SNAPS_CFG, worker=fake_worker(tmp_path, FAKE_SELFIE_IDENTITY="1"))(h.ctx(night="2026-10-30")))
    assert snap_row(h.store, g).status == "ready" and not guild_tiles(h.settings.data_dir, g).exists()
    assert guild_photo(data, g).read_bytes() == JPEG


# -- the GPU and the worker ---------------------------------------------------------------------

def test_an_oom_makes_room_and_contention_ends_the_stage(h, tmp_path, caplog):
    cast(h, 101, 102, 103)
    first = [situation(h, g) for g in (101, 102, 103)]
    admin = FakeAdmin([h.EMB, h.LIVE])            # Orion came back while the stage ran
    ctx = h.ctx(admin=admin)
    run(stage(tmp_path, FAKE_SELFIE_OOM="1")(ctx))
    assert ("unload", h.LIVE) in admin.calls and not ctx.failures
    assert all(snap_row(h.store, s).status == "ready" for s in first)
    d, e, f = (situation(h, g) for g in (101, 102, 103))
    log = tmp_path / "requests.log"
    ctx2 = h.ctx(admin=FakeAdmin([h.EMB]), night="2026-10-30")
    with caplog.at_level(logging.INFO, logger="leeroy_mind"):
        run(stage(tmp_path, FAKE_SELFIE_OOM="99", FAKE_SELFIE_LOG=str(log))(ctx2))
    assert ctx2.failures[STAGE] == CONTENDED
    assert [snap_row(h.store, s).status for s in (d, e, f)] == ["planned"] * 3
    # each one's first attempt asked once more after making room, then gave up; the third never
    assert [rid for rid, _, _ in renders(log)] == [f"s{d}:0"] * 2 + [f"s{e}:0"] * 2
    assert caplog.text.count("selfies_snaps_stopped") == 1
    assert "selfies_snaps_stopped: night=2026-10-30 reason=oom" in caplog.text


def test_a_lease_taken_between_renders_sends_no_render_and_fails_nothing(h, tmp_path, caplog):
    cast(h, 101, 102); a = situation(h, 101); b = situation(h, 102)
    ctx = h.ctx()
    run_begin(h.store, ctx.night, *ctx.window, now=ctx.window[1])
    pids, log = tmp_path / "pids.json", tmp_path / "requests.log"
    cmd, env = fake_command(tmp_path, **IDENTITY, FAKE_SELFIE_LOG=str(log), FAKE_SELFIE_CHILD="1",
                            FAKE_SELFIE_PIDS=str(pids))

    class Leased(SelfieWorker):
        async def render(self, req, *, rid=None):
            got = await super().render(req, rid=rid)
            if rid == f"s{a}:0":
                ctx.arbiter.hold("voice")                     # taken between two renders
            return got
    with caplog.at_level(logging.INFO, logger="leeroy_mind"):
        run(NightRunner([Stage(STAGE, make_snaps_stage(SNAPS_CFG, worker=lambda: Leased(cmd, env=env)),
                               "image", wants_gpu)], lambda n, w: ctx).run(ctx.night, ctx.window))
    assert snap_row(h.store, a).status == "ready" and snap_row(h.store, b).status == "planned"
    assert not any(rid.startswith(f"s{b}") for rid, _, _ in renders(log))   # never sent
    assert not ctx.failures and run_get(h.store, ctx.night).error is None
    assert 'nightly_image_refused: night=2026-10-29 stage=snaps holders=["voice"]' in caplog.text
    got = json.loads(pids.read_text())
    assert not pid_alive(got["pid"]) and not pid_alive(got["child"])


def test_a_lease_held_before_the_start_loads_no_worker(h, tmp_path):
    cast(h, 101); s = situation(h, 101)
    starts = tmp_path / "starts.log"
    ctx = h.ctx()
    ctx.arbiter.hold("preview")
    with pytest.raises(LiveLeaseHeld):
        run(stage(tmp_path, FAKE_SELFIE_STARTS=str(starts))(ctx))
    assert not starts.exists() and snap_row(h.store, s).status == "planned" and not ctx.failures


def test_a_worker_that_cannot_start_fails_one_unit_and_not_the_night(h, tmp_path, caplog):
    cast(h, 101, 102); a = situation(h, 101); b = situation(h, 102)
    ctx = h.ctx()
    with caplog.at_level(logging.INFO, logger="leeroy_mind"):
        run(stage(tmp_path, FAKE_SELFIE_READY_ERROR="cuda")(ctx))
    assert ctx.failures == {STAGE: 1}
    assert "selfies_unavailable: night=2026-10-29 stage=snaps reason=cuda" in caplog.text
    assert "nightly_unit_failed: night=2026-10-29 stage=snaps unit=worker" in caplog.text
    assert [snap_row(h.store, s).status for s in (a, b)] == ["planned", "planned"]


def test_a_night_with_nothing_to_render_takes_no_gpu(h, tmp_path):
    admin = FakeAdmin([h.REFL, h.EMB]); ctx = h.ctx(admin=admin)
    run_begin(h.store, ctx.night, *ctx.window, now=ctx.window[1])
    assert wants_gpu(ctx) is False                                 # nothing planned
    cast(h, 101, 102, 103)
    stale = situation(h, 101); repick(h, 101)
    assert wants_gpu(ctx) is False                                 # only a stale one
    g = guild(h, 101, 102, 103)
    assert wants_gpu(ctx) is True                                  # three faces still current
    repick(h, 102)
    assert wants_gpu(ctx) is False
    run(NightRunner([Stage(STAGE, stage(tmp_path), "image", wants_gpu)],
                    lambda n, w: ctx).run(ctx.night, ctx.window))
    assert admin.calls == []                                       # the chat models were left alone
    assert [snap_row(h.store, s).ended for s in (stale, g)] == ["photo_changed", "photo_changed"]
    assert situation(h, 103) and wants_gpu(ctx) is True


# -- the budget and the logs ----------------------------------------------------------------------

def test_the_counter_counts_images_and_the_degrade_step_caps_them(h):
    cast(h, 101, 102, 103)
    situation(h, 101); situation(h, 102); guild(h, 101, 102, 103)
    ctx = h.ctx()
    assert budget.COUNTERS[STAGE](ctx) == 5                     # three tiles and two situations
    ctx.settings = replace(ctx.settings, snaps_per_night=4)
    assert budget.COUNTERS[STAGE](ctx) == 4
    ctx.settings = replace(ctx.settings, snaps_per_night=1)
    assert budget.COUNTERS[STAGE](ctx) == 1                     # the guild photo comes first
    assert UNIT_CALLS[STAGE] == {"json": 0.6} and STAGE in EXTENSION_STAGES
    assert STAGE in [step.name for step in DEGRADE]


def test_the_night_caps_its_snaps_across_a_resume(h, tmp_path):
    cast(h, 101, 102, 103)
    snaps = [situation(h, g) for g in (101, 102, 103)]
    for _ in range(2):
        ctx = h.ctx()
        ctx.settings = replace(ctx.settings, snaps_per_night=2)
        run(stage(tmp_path)(ctx))
    assert [snap_row(h.store, s).status for s in snaps] == ["ready", "ready", "planned"]


def test_held_snaps_do_not_eat_the_nights_room(h, tmp_path):
    cast(h, 101, 102, 103)
    held = situation(h, 101, scene={**SCENE, "style": "lust"})   # waits: she lacks the lust style
    snaps = [situation(h, g) for g in (102, 103)]
    for _ in range(2):
        ctx = h.ctx()
        ctx.settings = replace(ctx.settings, snaps_per_night=2)
        run(stage(tmp_path)(ctx))
    assert [snap_row(h.store, s).status for s in (held, *snaps)] == ["planned", "ready", "ready"]


def test_a_resumed_night_renders_no_more_than_the_cap(h, tmp_path):
    cast(h, 101, 102, 103)
    snaps = [situation(h, g) for g in (101, 102, 103)]
    ctx = h.ctx()
    ctx.settings = replace(ctx.settings, snaps_per_night=2)
    run(stage(tmp_path)(ctx))
    ctx = h.ctx()
    ctx.settings = replace(ctx.settings, snaps_per_night=2)
    run(stage(tmp_path)(ctx))
    assert sum(snap_row(h.store, s).status == "ready" for s in snaps) == 2


def test_a_grid_file_left_for_a_still_planned_guild_photo_is_never_ready(h, tmp_path):
    cast(h, 101, 102, 103)
    g = guild(h, 101, 102, 103)
    out = guild_photo(h.settings.data_dir, g)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(JPEG)                                  # a crash left the grid, not the row
    ctx = h.ctx()
    ready_rows = _read(h.store, f"SELECT {_COLS} FROM selfie_snap WHERE status='ready'")
    assert ready_rows == [] and snap_row(h.store, g).status == "planned"
    from leeroy_mind.selfies.snaps import ready_guild
    assert ready_guild(h.store, now=ctx.clock.now(), phase="evening") is None


def test_logs_carry_no_prompt(h, tmp_path, caplog):
    cast(h, 101, 102, 103)
    s = situation(h, 101); g = guild(h, 101, 102, 103)
    flag = seed_for(h.night, 101, s, 0, 0)
    with caplog.at_level(logging.DEBUG):
        run(stage(tmp_path, FAKE_SELFIE_FLAG_SEEDS=str(flag))(h.ctx()))
    text = caplog.text
    assert "selfies_snap_ready" in text and "selfies_flagged" in text and "selfies_snaps_done" in text
    assert snap_row(h.store, s).status == "ready" and snap_row(h.store, g).status == "ready"
    for field in ("setting", "activity", "outfit", "shows"):
        assert SCENE[field] not in text
    for guid in (101, 102, 103):
        p = load_persona(h.store, guid)
        specs = [build_tile_prompt(p)] + ([build_scene_prompt(p, scene_from(SCENE))] if guid == 101 else [])
        for spec in specs:
            for phrase, _ in spec.phrases + spec.negative:
                assert phrase not in text
        assert p.character_name not in text and p.identity.real_name not in text
