"""Training source snapshots retain ownership through producer finalization."""

import asyncio
from dataclasses import asdict
import json
from types import SimpleNamespace as NS

import pytest

from leeroy_mind.bodies import BodyLedger
from leeroy_mind.guild.config import GuildConfig
from leeroy_mind.guild.loot import _get, _put
from leeroy_mind.guild.protocol import BotDo
from leeroy_mind.guild.raid_night import RaidRun
from leeroy_mind.guild.session import RaidPlan, RaidSession
from leeroy_mind.realm.ports import LivePorts as RealmPorts, RealmEvidenceUnavailable
from leeroy_mind.training import store
from leeroy_mind.training.applier import _encode
from leeroy_mind.training.lab_runner import ATTEMPT_PREFIX, OWNED_KEY, SUMMARY_PREFIX
from leeroy_mind.training.native_cleanup import PREFIX
from leeroy_mind.training.ports import LivePorts
from leeroy_mind.training.service import TrainingGround

from guild.fakes import FakeArmory, member, persona
from .test_tg_benchmark import bench, step
from .test_tg_lab_runner import complete, lab, start
from .test_tg_native_cleanup import offline, progress, receipt


def install_benchmark_ledger(bench):
    bodies = BodyLedger(bench.clock)
    bodies.add_holder_source("training-jobs", bench.runner.body_holds)
    bench.holder, bench.claims_for = bodies.holder, bodies.claims_for
    bench.claim = lambda owner, guids, *, until: bodies.claim(owner, guids, rank="training", until=until)
    bench.release = bodies.release
    return bodies


def test_dynamic_benchmark_source_exists_before_claim_and_survives_release(bench):
    bodies = install_benchmark_ledger(bench)
    original = bench.claim
    claims = []
    def claim(owner, guids, *, until):
        row, = bench.runner.body_holds()
        assert row.owner == owner == "benchmark:" + row.scope_id
        assert row.rank == 2 and not row.retained
        claims.append(row)
        return original(owner, guids, until=until)
    bench.claim = claim
    asyncio.run(step(bench))
    row, = bench.runner.body_holds()
    assert claims and row.owner == claims[0].owner and row.retained
    assert bodies.claim(row.owner, row.guids, rank="training", until=bench.clock.now() + 600) == set(row.guids)
    assert bodies.release(row.owner) == set(row.guids)
    assert bodies.claims_for(row.owner) == frozenset()
    assert bench.runner.body_holds() == (row,)
    assert bodies.holder(min(row.guids)) == row.owner
    assert not bodies.may_move(row.owner, min(row.guids))


def test_benchmark_effective_handback_waits_for_actual_finish_boundary(bench):
    bodies = install_benchmark_ledger(bench)
    releases, observed = [], []
    bodies.on_release(lambda owner, guids: releases.append((owner, guids)))
    asyncio.run(step(bench))
    row, = bench.runner.body_holds()
    original = bench.release
    def release(owner, guids=None):
        assert bench.runner.body_holds() == (row,)
        removed = original(owner, guids)
        assert bench.runner.body_holds() == (row,) and not releases
        observed.append(removed)
        return removed
    bench.release = release
    bench.cleanup_state = "complete"
    asyncio.run(step(bench))
    assert observed == [set(row.guids)]
    assert bench.runner.body_holds() == ()
    assert bodies.holder(min(row.guids)) is None
    assert releases == [(row.owner, set(row.guids))]


@pytest.mark.parametrize("expire", [False, True])
def test_benchmark_release_accepts_missing_or_expired_local_claims(bench, expire):
    bodies = install_benchmark_ledger(bench)
    async def scenario():
        await step(bench)
        row, = bench.runner.body_holds()
        if expire:
            await bench.clock.advance(20000, step=20000)
        else:
            bodies.release(row.owner)
        assert bodies.claims_for(row.owner) == frozenset()
        bench.runner._release_owned()
        assert bench.runner.body_holds() == (row,)
        assert bodies.holder(min(row.guids)) == row.owner
    asyncio.run(scenario())


@pytest.mark.parametrize("field,value", [("owner", "training"), ("command_id", "other"),
    ("members", [True]), ("dispatched", False), ("conflict", "false")])
def test_benchmark_corruption_never_becomes_an_empty_snapshot(bench, field, value):
    asyncio.run(step(bench))
    bench.runner._state[field] = value
    with pytest.raises(ValueError, match="benchmark body evidence unavailable"):
        bench.runner.body_holds()


def test_lab_reservation_survives_local_release_until_phase_complete(fake, lab):
    runner, _, _ = lab
    bodies = BodyLedger(fake.clock)
    bodies.add_holder_source("lab", runner.body_holds)
    fake.holder, fake.claims_for = bodies.holder, bodies.claims_for
    fake.claim = lambda owner, guids, *, until: bodies.claim(owner, guids, rank="training", until=until)
    releases, snapshots = [], []
    bodies.on_release(lambda owner, guids: releases.append((owner, guids)))
    def release(owner, guids=None):
        row, = runner.body_holds()
        assert row.scope_id == runner._owned["id"] and row.retained
        assert runner._owned["phase"] == "restoring"
        snapshots.append(row)
        removed = bodies.release(owner, guids)
        assert runner.body_holds() == (row,) and not releases
        return removed
    fake.release = release
    async def scenario():
        await runner.tick()
        row, = runner.body_holds()
        assert row.owner == "training" and row.rank == 2 and row.retained
        assert row.guids == frozenset(fake.members)
        await runner._finish("test_finished")
        assert runner.body_holds() == ()
        assert _get(fake.conn, OWNED_KEY)["phase"] == "complete"
        assert bodies.holder(min(row.guids)) is None
        assert snapshots == [row] and releases == [("training", set(row.guids))]
    asyncio.run(scenario())


@pytest.mark.parametrize("phase", ["preparing", "ready", "waiting", "blocked", "restoring"])
def test_lab_all_active_phases_retain_full_manifest_after_claim_expiry(fake, lab, phase):
    runner, _, _ = lab
    asyncio.run(runner.tick())
    runner._owned.update(phase=phase, halt="canceled", preempted=[101], alive=[102])
    row, = runner.body_holds()
    assert row.retained and row.guids == frozenset(fake.members)
    assert row.scope_id == runner._owned["id"]


@pytest.mark.parametrize("field,value", [("id", ""), ("guids", []), ("guids", [True]),
    ("guids", [101, 101]), ("phase", "unknown")])
def test_lab_malformed_operation_is_unavailable(fake, lab, field, value):
    runner, _, _ = lab
    asyncio.run(runner.tick())
    runner._owned[field] = value
    with pytest.raises((ValueError, KeyError)):
        runner.body_holds()


def test_lab_desired_setups_still_prevent_admission(fake, lab):
    runner, _, applier = lab
    for guid in fake.members:
        store.put_setup(fake.conn, dict(guid=guid, since=fake.clock.now(), toggles=["+damage"]))
    asyncio.run(runner.tick())
    assert not runner.active and runner.body_holds() == ()
    assert runner.reason == "no_restorable_idle_damage_pair" and not fake.claims
    applier.apply_standing.assert_not_awaited()


def test_release_adapter_returns_real_removed_claims_and_explicit_no_ledger(app_stub):
    ports = LivePorts(app_stub)
    assert ports.claims_for("training") == frozenset()
    assert ports.release("training", [101]) == set()
    bodies = app_stub.bodies = BodyLedger(app_stub.clock)
    assert ports.claim("training", [101], until=app_stub.clock.now() + 10) == {101}
    assert ports.claims_for("training") == frozenset({101})
    assert ports.release("training", [101, 102]) == {101}
    assert ports.release("training", [101]) == set()
    assert bodies.claims_for("training") == frozenset()


def journal_ground(fake):
    fake.connection_epoch = 1
    for guid in (101, 102):
        fake.members[guid] = member(guid, f"M{guid}", 1 if guid == 101 else 8, 2, map=631)
        fake.personas[guid] = persona(guid, f"M{guid}")
    plan = RaidPlan("icc", 10, False, False, 101, 101, 102, [101, 102], [], True)
    session = asyncio.run(RaidSession.open(fake, GuildConfig(), FakeArmory({}, {}), plan))
    session.bind_instance(77)
    run = RaidRun(session.plan, session, True, False)
    ground = TrainingGround(NS(bodies=object()), fake)
    ground.listener.run = run
    key = ground.listener.run_key(run)
    scope = dict(run_key=key, session_key=f"guild:raid:{session.raid_id}:{session.started}",
                 instance_id=77, connection_epoch=1, body_owner="raid", tank_guid=101, lead_is_human=False)
    command = BotDo("journal", (102,), "nc", "+threat")
    envelope = dict(wire=_encode(command), scope=scope, keys=[[102, "threat"]], settled=True,
                    members={"102": asdict(ground.members_for(run)[102])})
    ident = store.log_applied(fake.conn, key, "boss", "631:0:0", envelope,
                              _encode(BotDo("inverse", (102,), "restore", "*")), fake.clock.now())
    return ground, run, ident, envelope


@pytest.mark.parametrize("retained", [False, True])
def test_journals_are_accounted_for_by_exact_current_or_retained_session(fake, retained):
    ground, run, _, _ = journal_ground(fake)
    if retained:
        ground.listener.invalidate(run)
    assert ground.body_holds() == ()  # Raid source owns the validated reservation.
    assert ground.listener.retained_run(ground.listener.run_key(run)) is run


@pytest.mark.parametrize("change", ["orphan", "session", "members", "owner", "epoch", "scope", "json",
                                    "manifest", "keys", "negative_ts"])
def test_malformed_or_orphan_journals_make_whole_training_source_unavailable(fake, change):
    ground, run, ident, envelope = journal_ground(fake)
    if change == "orphan":
        ground.listener.run = None
    elif change == "session":
        envelope["scope"]["session_key"] += "replacement"
    elif change == "members":
        envelope["wire"]["data"]["guids"] = [999]
    elif change == "owner":
        envelope["scope"]["body_owner"] = "training"
    elif change == "epoch":
        envelope["scope"]["connection_epoch"] = True
    elif change == "scope":
        del envelope["scope"]
    elif change == "manifest":
        envelope["members"]["102"]["guid"] = True
    elif change == "keys":
        envelope["keys"] = [[True, "threat"]]
    with fake.conn:
        fake.conn.execute("UPDATE tg_applied SET command=? WHERE id=?",
                          ("private corrupt payload" if change == "json" else json.dumps(envelope), ident))
        if change == "negative_ts":
            fake.conn.execute("UPDATE tg_applied SET ts=-1 WHERE id=?", (ident,))
    with pytest.raises(RuntimeError, match="^training body evidence unavailable$"):
        ground.body_holds()


def test_run_owner_evidence_never_weakens_native_command_binding_or_fence(offline):
    ports, run, controller, ground = offline
    # Use the existing actual-controller fixture, not a manufactured native proof.
    async def scenario():
        ground.listener.run = run
        async def query(command, *args, **kwargs):
            return [receipt(ident=command.id, members=tuple(run.plan.members))]
        ports.request = query
        ground.native.feed(progress(ports, run))
        assert await ground.native.prepare(run)
        binding = ground.native.for_run(run)
        assert binding is not None
        controller._owned["id"] = "replacement_command"
        controller._save()
        assert ground.native.for_run(run) is None
        controller._owned["id"] = binding.job_id
        controller._save()
        ground.applier.fence_native(binding)
        assert ground.native.for_run(run) is None
        assert ground.applier._state(ground.listener.run_key(run)) is None
        assert _get(ports.conn, PREFIX + binding.run_key) is not None
    asyncio.run(scenario())


def lab_source(fake, runner, attached):
    app = NS(clock=fake.clock, store=NS(conn=fake.conn), human_guid=1, guild=None, training=None)
    if attached:
        ground = app.training = TrainingGround(app, fake)
        ground.jobs.lab = runner
    ports = RealmPorts(app)
    bodies = app.bodies = BodyLedger(fake.clock)
    bodies.add_holder_source("training", ports.training_body_holds)
    return ports, bodies


@pytest.mark.parametrize("attached", [False, True])
@pytest.mark.parametrize("current", ["pending", "missing", "malformed", "none_without_finish"])
def test_lab_complete_flag_cannot_hide_pending_or_malformed_attempt(fake, lab, attached, current):
    runner, _, _ = lab
    ports, bodies = lab_source(fake, runner, attached)
    asyncio.run(start(runner))  # Actual producer has dispatched an unresolved DummyTrial.
    if attached:
        row, = ports.training_body_holds()
        assert row.retained
    runner._owned["phase"] = "complete"
    if current == "missing":
        del runner._owned["current"]
    elif current == "malformed":
        runner._owned["current"] = []
    elif current == "none_without_finish":
        runner._owned["current"] = None
    runner._save()
    with pytest.raises(RealmEvidenceUnavailable):
        ports.training_body_holds()
    assert bodies.claim("realm_day", fake.members, rank=4, until=fake.clock.now() + 60) == set()
    assert not bodies.may_move("realm_day", min(fake.members))


async def finish_lab(runner, fake):
    await start(runner)
    count = 0
    while runner.active and count < 30:
        if runner._owned["current"]:
            await complete(fake, runner)
        else:
            await runner.tick()
        count += 1
    assert not runner.active and runner._owned["current"] is None
    assert runner._owned["attempts"] == len(runner._owned["attempt_ids"]) > 0


@pytest.mark.parametrize("attached", [False, True])
@pytest.mark.parametrize("rejected", [False, True])
def test_actual_completed_lab_history_is_empty_for_attached_and_disabled_sources(fake, lab, attached, rejected):
    runner, _, _ = lab
    ports, bodies = lab_source(fake, runner, attached)
    if rejected:
        fake.acks["dummy_trial"].append((False, "busy"))
        asyncio.run(start(runner))
        assert not runner.active and runner.reason == "dummy_command_rejected"
    else:
        asyncio.run(finish_lab(runner, fake))
    assert ports.training_body_holds() == () and runner.body_holds() == ()
    assert bodies.claim("realm_day", fake.members, rank=4, until=fake.clock.now() + 60) == set(fake.members)


@pytest.mark.parametrize("attached", [False, True])
@pytest.mark.parametrize("change", ["summary", "cursor", "attempt_missing", "attempt_pending", "terminal"])
def test_lab_terminal_history_requires_committed_finish_and_attempt_evidence(fake, lab, attached, change):
    runner, _, _ = lab
    ports, _ = lab_source(fake, runner, attached)
    asyncio.run(finish_lab(runner, fake))
    assert ports.training_body_holds() == ()
    key = ATTEMPT_PREFIX + runner._owned["attempt_ids"][0]
    with fake.conn:
        if change == "summary":
            fake.conn.execute("DELETE FROM meta WHERE key=?", (SUMMARY_PREFIX + runner._owned["night"],))
        elif change == "cursor":
            fake.conn.execute("DELETE FROM meta WHERE key='tg:lab:cursor'")
        elif change == "attempt_missing":
            fake.conn.execute("DELETE FROM meta WHERE key=?", (key,))
        else:
            attempt = _get(fake.conn, key)
            if change == "attempt_pending":
                attempt["restoration"] = "pending"
            else:
                attempt["terminal"] = None
            _put(fake.conn, key, attempt)
    with pytest.raises(RealmEvidenceUnavailable):
        ports.training_body_holds()
