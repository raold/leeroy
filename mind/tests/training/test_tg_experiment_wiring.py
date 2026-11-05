"""Actual guild/listener, ledger/scorer and owned experiment lane integration."""

import asyncio
from collections import deque
from dataclasses import asdict
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from leeroy_mind.guild.config import GuildConfig
from leeroy_mind.guild.offline import OfflineRaids
from leeroy_mind.guild.raid_night import RaidRun
from leeroy_mind.guild.session import RaidPlan, RaidSession
from leeroy_mind.training import drift, service, store
from leeroy_mind.training.experiment_lifecycle import ExperimentLifecycle
from leeroy_mind.training.spells import SpellBook

from guild.fakes import FakeArmory, member as guild_member, persona
from .test_tg_applier import vocabulary
from .test_tg_experiments import CTX, candidate, experiment, meta, pull
from .test_tg_gate import _run_async, boss
from .test_tg_ledger import header, member
from .test_tg_native_cleanup import progress, receipt


@pytest.fixture
def wired(fake, experiment, tmp_path):
    _, prior = experiment
    fake.connection_epoch = 1
    fake.set_lead = lambda guid: setattr(fake, "lead", guid)
    fake.disconnect = AsyncMock()
    for guid in range(101, 109):
        fake.members[guid] = guild_member(guid, f"Member{guid}", 1 if guid == 101 else 8, 2, map=631)
        fake.personas[guid] = persona(guid, f"Member{guid}")
    plan = RaidPlan("icc", 10, False, True, 101, 101, 101, list(range(101, 109)), [], False)
    meta(fake, f"guild:raid:{prior.session.raid_id}:plan", asdict(plan))
    session = asyncio.run(RaidSession.open(fake, GuildConfig(), FakeArmory({}, {}), plan))
    session.bind_instance(77)
    run = RaidRun(session.plan, session, False, False)
    offline = OfflineRaids.__new__(OfflineRaids)
    offline.ports, offline.run, offline.session = fake, run, session
    offline._owned = dict(id="original_job", phase="running", cancelled=False, terminal=None,
        sent_at=fake.clock.now()-1, accepted=list(plan.members), actual_plan=asdict(plan),
        plan=asdict(plan), instance=77, raid_id=session.raid_id, started=True, running_seen=True)
    offline._inputs = deque()
    offline._save()
    with fake.conn:
        fake.conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES ('guild_id','10')")
    offline._online, offline._listeners = False, []
    app = SimpleNamespace(data_dir=tmp_path, guild=SimpleNamespace(offline=offline,
                           raid_night=SimpleNamespace(active=False)))
    ground = service.TrainingGround(app, fake)
    ground._connected = ground._presence_known = ground._startup_ready = True
    ground.gate._phase = "passed"
    ground.vocab = vocabulary()
    ground.native.feed(progress(fake, run))
    fake.request = AsyncMock(side_effect=lambda cmd, *a, **k:
        [receipt(ident=cmd.id, members=tuple(run.plan.members))])
    offline.add_listener(ground.experiments)
    offline.add_listener(ground.listener)
    return fake, run, ground, offline


def pending(fake, run):
    return fake.conn.execute("SELECT value FROM meta WHERE key=?",
                             (f"tg:summary_pending:{run.session.raid_id}",)).fetchone()


async def finish(fake, run, ground, offline):
    # Actual session.close includes awaited standings and wrap-up scheduling.
    await run.session.close("completed")
    await offline._notify("on_end", "completed")
    offline._owned["phase"] = "complete"
    offline.run = offline.session = None


async def open_trial(fake, run, ground):
    candidate(fake)
    assert await ground.experiments.prepare(run)
    trial = store.trial_rows(fake.conn, status="open")[0]
    arms = ground.experimenter._arms(trial["id"])
    return trial, next(arm for arm in arms if arm["champion"])


@_run_async
async def test_actual_guild_prepares_drift_before_listener_hold_or_application(wired, monkeypatch):
    fake, run, ground, offline = wired
    candidate(fake)
    assert await ground.experimenter.open_trials() == 1
    old = store.trial_rows(fake.conn, status="open")[0]
    head = fake.cfg.core_dir / "modules" / drift.MODULES[0] / ".git" / "HEAD"
    head.write_text("b" * 40, encoding="ascii")
    seen = []
    async def hold(tank):
        assert ground.listener.run is run
        assert ground.listener.experimenter is ground.experimenter
        assert fake.conn.execute("SELECT status FROM tg_trial WHERE id=?", (old["id"],)).fetchone()[0] == "drift"
        assert all(t["lever"] == "stock" for t in store.trial_rows(fake.conn, status="open"))
        assert ground.experiments.admission_allowed(run)
        seen.append(tank)
        return False
    monkeypatch.setattr(ground.applier, "hold", hold)
    await offline._notify("on_start", run)
    assert seen == [101]
    assert json.loads(fake.conn.execute("SELECT value FROM meta WHERE key='tg:drift_run'").fetchone()[0]) == run.session.raid_id
    assert not fake.sent  # The hold boundary was inspected without live commands.


@pytest.mark.parametrize("missing", ["observation", "presence", "instance", "owner", "revision", "gate"])
@_run_async
async def test_unknown_readiness_never_installs_or_starts_application(wired, monkeypatch, missing):
    fake, run, ground, offline = wired
    candidate(fake)
    if missing == "observation":
        fake.conn.execute("UPDATE tg_pull SET received=?", (fake.clock.now(),))
    elif missing == "presence":
        ground._presence_known = False
    elif missing == "instance":
        run.session.ctx.instance_id = None
    elif missing == "owner":
        offline.run = SimpleNamespace()
    elif missing == "revision":
        (fake.cfg.core_dir / "modules" / drift.MODULES[0] / ".git" / "HEAD").write_text("unknown", encoding="ascii")
    else:
        ground.gate._phase = "failed"
    hold = AsyncMock(return_value=True)
    monkeypatch.setattr(ground.applier, "hold", hold)
    await offline._notify("on_start", run)
    assert ground.listener.run is run
    assert ground.listener.experimenter is None and not ground.application_allowed()
    hold.assert_not_awaited()
    assert not store.trial_rows(fake.conn, status="open") and not fake.sent


@_run_async
async def test_terminal_retained_run_cannot_be_prepared_again(wired):
    fake, run, ground, offline = wired
    await open_trial(fake, run, ground)
    await ground.experiments.on_before_end(run)
    offline._owned["terminal"] = {"state": "done"}
    await ground.experiments.tick()
    assert ground.listener.experimenter is None
    assert not ground.experiments.admission_allowed(run)
    assert pending(fake, run) is None


@_run_async
async def test_stale_preparation_after_await_does_not_open_or_install(wired, monkeypatch):
    fake, run, ground, offline = wired
    candidate(fake)
    entered, release = asyncio.Event(), asyncio.Event()
    actual = ground.experimenter.drift_check
    async def suspended(observed):
        result = await actual(observed)
        entered.set()
        await release.wait()
        return result
    monkeypatch.setattr(ground.experimenter, "drift_check", suspended)
    work = asyncio.create_task(ground.experiments.prepare(run))
    await entered.wait()
    # Reconnect and authoritative owner replacement while awaiting a dependency.
    ground._epoch += 1
    ground.experiments.preempt()
    offline.run = SimpleNamespace()
    release.set()
    assert not await work
    assert ground.listener.experimenter is None and not store.trial_rows(fake.conn, status="open")


@_run_async
async def test_tick_can_prepare_late_startup_without_resetting_listener(wired, monkeypatch):
    fake, run, ground, offline = wired
    candidate(fake)
    ground._startup_ready = False
    await offline._notify("on_start", run)
    assert ground.listener.experimenter is None
    generation = ground.listener._generation
    ground._startup_ready = True
    await ground.experiments.tick()
    assert ground.listener.experimenter is ground.experimenter
    assert ground.listener._generation == generation and ground.listener.run is run
    assert ground.experiments.admission_allowed(run)
    # The native readiness adapter owns the separate explicit startup retry.
    assert ground.listener._start_ready is False
    held = []
    async def hold(guid):
        assert ground.native.for_run(run) is not None
        assert ground.experiments.admission_allowed(run)
        held.append(guid)
        return False
    monkeypatch.setattr(ground.applier, "hold", hold)
    await ground.listener.tick()
    await ground.listener.tick()
    assert held == [101] and ground.listener._start_attempted


@_run_async
async def test_late_readiness_after_first_combat_cannot_restart_startup(wired, monkeypatch):
    fake, run, ground, offline = wired
    ground._startup_ready = False
    await offline._notify("on_start", run)
    await ground.listener.on_boss(boss(fake, instance_id=77))
    assert not ground.listener._start_waiting
    ground._startup_ready = True
    await ground.experiments.tick()
    hold = AsyncMock(return_value=False)
    monkeypatch.setattr(ground.applier, "hold", hold)
    await ground.listener.tick()
    hold.assert_not_awaited()


@pytest.mark.parametrize("boundary", ["native", "hold"])
@_run_async
async def test_consumed_offline_engage_fences_inflight_startup(wired, monkeypatch, boundary):
    fake, run, ground, offline = wired
    entered, release = asyncio.Event(), asyncio.Event()
    holds = []
    async def query(command, *args, **kwargs):
        if boundary == "native":
            entered.set()
            await release.wait()
        return [receipt(ident=command.id, members=tuple(run.plan.members))]
    async def hold(guid):
        holds.append(guid)
        if boundary == "hold":
            entered.set()
            await release.wait()
        return True
    fake.request = query
    monkeypatch.setattr(ground.applier, "hold", hold)
    standing = AsyncMock()
    monkeypatch.setattr(ground.applier, "apply_standing", standing)
    starting = asyncio.create_task(offline._notify("on_start", run))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        await offline.on_boss(boss(fake, instance_id=77))
        await asyncio.wait_for(offline._drain(), 1)
        assert not offline._inputs and ground.listener._pull
        assert not ground.listener._start_waiting
        release.set()
        await asyncio.wait_for(starting, 1)
        assert holds == ([] if boundary == "native" else [101])
        standing.assert_not_awaited()
        assert not ground.listener._start_ready
    finally:
        release.set()
        await starting


@_run_async
async def test_actual_after_store_scores_then_observes_without_inline_transport(wired, monkeypatch):
    fake, run, ground, _ = wired
    _, champion = await open_trial(fake, run, ground)
    ground._book = SpellBook()
    ground.listener.run = run
    ground.ledger.set_run(run)
    ground.ledger.set_in_play(CTX, card_id=champion["card"], arm_id=champion["id"])
    # Observation remains wired independently of current arm installation.
    ground.listener.set_experimenter(None)
    h = header(fake, start=int(fake.clock.now() * 1000) + 1000)
    await fake.clock.advance(61, step=61)
    async def forbidden(*args, **kwargs):
        pytest.fail("inline experiment callback attempted transport")
    monkeypatch.setattr(fake, "say", forbidden)
    monkeypatch.setattr(fake, "send", forbidden)
    await ground.ledger.on_event(member(h, class_=1, spec_tab=2, tank=True))
    await ground.ledger.on_event(h)
    row = fake.conn.execute("SELECT * FROM tg_pull WHERE id=?", (h.id,)).fetchone()
    assert row["checks_version"] > 0 and row["score"] is not None and row["score_basis"] == "deaths_time"
    obs = store.obs_rows(fake.conn, champion["id"])
    assert len(obs) == 1 and obs[0]["pull"] == h.id
    assert fake.conn.execute("SELECT value FROM meta WHERE key=?", (f"tg:obs:{champion['id']}:{h.id}",)).fetchone()
    await ground.ledger.on_event(h)
    assert len(store.obs_rows(fake.conn, champion["id"])) == 1


@_run_async
async def test_human_champion_observations_do_not_require_installed_offline_listener(wired):
    fake, run, ground, _ = wired
    _, champion = await open_trial(fake, run, ground)
    for index in range(3):
        await fake.clock.advance(101, step=101)
        pull(fake, f"champion-{index}", arm=champion["id"], card=champion["card"], raid=run.session.raid_id)
        await ground.experiments.after_store(SimpleNamespace(header=SimpleNamespace(id=f"champion-{index}")), None)
    ground.experiments.invalidate(run)
    await fake.clock.advance(101, step=101)
    pull(fake, "human-champion", card=champion["card"], kind="his", raid=None)
    await ground.experiments.after_store(SimpleNamespace(header=SimpleNamespace(id="human-champion")), None)
    assert ground.listener.experimenter is None
    assert len(store.obs_rows(fake.conn, champion["id"])) == 4


@_run_async
async def test_summary_waits_for_real_close_then_late_observation_and_only_attempts_once(wired):
    fake, run, ground, offline = wired
    _, champion = await open_trial(fake, run, ground)
    await fake.clock.advance(101, step=101)
    pull(fake, "late-proof", arm=champion["id"], card=champion["card"], raid=run.session.raid_id)
    await ground.experiments.on_before_end(run)
    await ground.experiments.on_end("ended", run=run)
    assert pending(fake, run) is None  # Retained cleanup is not a completed end.
    await finish(fake, run, ground, offline)
    assert pending(fake, run) is not None
    fake.said.clear()
    ground.experiments._replay_due = fake.clock.now() + 3600
    await fake.clock.advance(59, step=59)
    await ground.experiments.tick()
    assert not fake.said
    await fake.clock.advance(1, step=1)
    await ground.experiments.tick()
    assert not fake.said and pending(fake, run) is not None  # No trial observation yet.
    await ground.experiments.after_store(SimpleNamespace(header=SimpleNamespace(id="late-proof")), None)
    # Recovery is durable across a process/controller reconstruction.
    ground.experiments = ExperimentLifecycle(ground, ground.experimenter)
    await ground.experiments.tick()
    assert len(fake.said) == 1 and fake.said[0][1] == "guild"
    assert "results recorded" in fake.said[0][2] and "DPS" not in fake.said[0][2]
    assert pending(fake, run) is None
    # A duplicated end notification cannot produce a second daily attempt.
    await ground.experiments.on_end("ended", run=run)
    await ground.experiments.tick()
    assert len(fake.said) == 1 and pending(fake, run) is None
    receipt = fake.conn.execute("SELECT value FROM meta WHERE key GLOB 'tg:summary:????-??-??'").fetchone()
    assert "attempted" in json.loads(receipt[0])


@_run_async
async def test_recovery_replays_only_bounded_committed_scored_pulls(wired):
    fake, run, ground, _ = wired
    _, champion = await open_trial(fake, run, ground)
    for index in range(10):
        await fake.clock.advance(101, step=101)
        pull(fake, f"recovery-{index}", arm=champion["id"], card=champion["card"], raid=run.session.raid_id)
    await ground.experiments.tick()
    assert len(store.obs_rows(fake.conn)) == ground.experiments.REPLAY_BATCH
    await ground.experiments.tick()
    assert len(store.obs_rows(fake.conn)) == 10
    await ground.experiments.tick()
    assert len(store.obs_rows(fake.conn)) == 10


@_run_async
async def test_owned_summary_finishes_sql_before_canceled_shutdown_returns(wired, monkeypatch):
    fake, run, ground, offline = wired
    _, champion = await open_trial(fake, run, ground)
    await fake.clock.advance(101, step=101)
    pull(fake, "summary-owned", arm=champion["id"], card=champion["card"], raid=run.session.raid_id)
    await ground.experiments.after_store(SimpleNamespace(header=SimpleNamespace(id="summary-owned")), None)
    await finish(fake, run, ground, offline)
    await fake.clock.advance(60, step=60)
    entered, release = asyncio.Event(), asyncio.Event()
    async def retained_say(*args, **kwargs):
        entered.set()
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                pass
        assert fake.conn.execute("SELECT count(*) FROM tg_obs").fetchone()[0] == 1
        return True
    monkeypatch.setattr(fake, "say", retained_say)
    lane = asyncio.create_task(ground.experiments.tick())
    ground._lanes.add(lane)
    await entered.wait()
    caller = asyncio.create_task(ground.close())
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    assert not ground._close_task.done()
    assert fake.conn.execute("SELECT 1").fetchone()[0] == 1
    release.set()
    await ground.close()
    assert ground._close_task.done() and lane.done() and ground.experiments._speech is None
    assert pending(fake, run) is None


@_run_async
async def test_presence_preemption_cancels_summary_but_preserves_ticker(wired, monkeypatch):
    fake, run, ground, offline = wired
    _, champion = await open_trial(fake, run, ground)
    await fake.clock.advance(101, step=101)
    pull(fake, "preempt-summary", arm=champion["id"], card=champion["card"], raid=run.session.raid_id)
    await ground.experiments.after_store(SimpleNamespace(header=SimpleNamespace(id="preempt-summary")), None)
    await finish(fake, run, ground, offline)
    await fake.clock.advance(60, step=60)
    entered = asyncio.Event()
    async def speech(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(fake, "say", speech)
    lane = asyncio.create_task(ground.experiments.tick())
    await entered.wait()
    fake.online = True
    ground.experiments.preempt()
    await lane  # Child cancellation is consumed; the service lane can continue.
    assert not lane.cancelled() and ground.experiments._speech is None
    assert pending(fake, run) is None
    await ground.experiments.tick()
