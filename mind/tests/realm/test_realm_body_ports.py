"""Source-owned reservations and exact controller/listener run evidence."""

import asyncio
import copy
from dataclasses import asdict, replace
import random
from types import SimpleNamespace as NS

import pytest

from leeroy_mind.bodies import ALL, BodyLedger
from leeroy_mind.guild.config import GuildConfig
from leeroy_mind.guild.loot import _put
from leeroy_mind.guild.offline import OfflineRaids
from leeroy_mind.guild.protocol import BotDo, Job
from leeroy_mind.guild.raid_night import RaidNight, RaidRun, State
from leeroy_mind.guild.session import RaidPlan, RaidSession
from leeroy_mind.realm.ports import LivePorts, RealmEvidenceUnavailable
from leeroy_mind.storage.store import Store
from leeroy_mind.training import protocol as training_protocol, store as ts
from leeroy_mind.training.cards import Directive
from leeroy_mind.training.config import TrainingConfig
from leeroy_mind.training.ports import LivePorts as TrainingPorts
from leeroy_mind.training.protocol import TrainingCleanup
from leeroy_mind.training.service import TrainingGround

from guild.fakes import FakeArmory, FakePorts, member, persona
from training.test_tg_applier import card, vocabulary


@pytest.fixture
def boundary(tmp_path, clock):
    guild_ports = FakePorts(tmp_path, clock)
    # Direct construction bypasses attach_training, which registers this wire
    # family in production. Cleanup includes the real DC clear command too.
    training_protocol.register()
    for guid in (101, 102):
        guild_ports.members[guid] = member(guid, f"Member{guid}", 1 if guid == 101 else 8, 2, map=631)
        guild_ports.personas[guid] = persona(guid, f"Member{guid}")
    cfg, armory = GuildConfig(), FakeArmory({}, {})
    controller = RaidNight(guild_ports, cfg, armory, random.Random(7))
    app = NS(clock=clock, store=Store(guild_ports.conn, clock=clock), human_guid=1,
             guild=NS(cfg=cfg, ports=guild_ports, raid_night=controller,
                      offline=OfflineRaids(guild_ports, cfg, armory, random.Random(8))), training=None)
    ports = LivePorts(app)
    training_ports = TrainingPorts(app, cfg=TrainingConfig(enabled=True, act=True, experiments=False))
    training_ports._generation = 1
    ground = app.training = TrainingGround(app, training_ports)
    ground._epoch = 1
    ground._connected = ground._presence_known = ground._startup_ready = True
    ground.vocab, ground.gate._phase = vocabulary(), "passed"
    ground.ledger.first_pull_ts = lambda: clock.now() - 28 * 86400
    bodies = app.bodies = BodyLedger(clock, run_proof=ports.run_body_proof)
    bodies.add_holder_source("raid", ports.raid_body_holds)
    bodies.add_holder_source("offline", ports.offline_body_holds)
    bodies.add_holder_source("training", ports.training_body_holds)
    controller.add_listener(ground.listener)
    async def wait_job(ident, timeout_s):
        command = next(c for c in reversed(guild_ports.sent) if c.id == ident)
        return Job(int(clock.now() * 1000), ident, "bot_do", "done", "", "",
                   command.guids, (), 0, 0, 0, 0, 0, 0, command.training_run)
    training_ports.wait_job = wait_job
    try:
        yield NS(app=app, ports=ports, ground=ground, bodies=bodies, controller=controller,
                 guild_ports=guild_ports, cfg=cfg, armory=armory)
    finally:
        guild_ports.close()


def session_for(b, *, offline=False, bound=True, guids=(101, 102)):
    for guid in guids:
        if guid not in b.guild_ports.members:
            b.guild_ports.members[guid] = member(guid, f"Member{guid}", 8, 2, map=631)
            b.guild_ports.personas[guid] = persona(guid, f"Member{guid}")
    plan = RaidPlan("icc", 10, False, offline, 101, 101, 102, list(guids), [], not offline)
    session = asyncio.run(RaidSession.open(b.guild_ports, b.cfg, b.armory, plan))
    if bound:
        assert session.bind_instance(77)
    run = RaidRun(session.plan, session, not offline, False)
    controller = b.app.guild.offline if offline else b.controller
    controller.session, controller.run = session, run
    if offline:
        # Match the real accepted/running operation schema, including the
        # dispatch freshness and lifecycle fields inspected by Guild events.
        sent_at = b.app.clock.now() - 1
        controller._owned = dict(id="offline_original", day=controller._day(), phase="accepted",
            plan=asdict(session.plan), sent_at=sent_at,
            deadline=sent_at + b.cfg.offline_timeout_min * 60, cancelled=False, cancel_at=None,
            accepted=list(guids), instance=77, raid_id=session.raid_id, actual_plan=asdict(session.plan),
            started=True, running_seen=True, terminal=None, end_notified=False)
        b.guild_ports.online = controller._online = False
        controller._save()
    else:
        controller.state = State.RUNNING
    b.ground.listener.run = run
    return run


def test_exact_current_and_closing_scope_restores_after_ended_before_handback(boundary):
    b = boundary
    run = session_for(b)
    before = b.ports.raid_body_holds()
    finite = next(h for h in before if h.guids is not ALL)
    assert before[0].guids is ALL and not before[0].retained
    assert finite.guids == frozenset({101, 102}) and finite.retained
    assert b.bodies.owner_for_run(run) == "raid"
    assert not b.bodies.may_move("raid", 101)

    async def scenario():
        members = b.ground.members_for(run)
        assert await b.ground.applier.apply(b.ground.listener.run_key(run), "boss", "631:0:0",
            card(Directive("threat", "+")), {102: members[102]}, tank_guid=101, lead_is_human=False)
        assert ts.open_applied(b.ports.conn, 0)
        b.controller._end("ended")
        assert b.controller.state is State.ENDED and b.controller.run is None
        assert b.ground.listener.run is None
        assert b.ports.raid_body_holds() == (finite,)
        assert b.bodies.owner_for_run(run) == "raid"
        assert b.ground.state_for(b.ground.listener.run_key(run)).body_owner == "raid"
        original = b.ground.ports.send
        restored = []
        async def send(command):
            if isinstance(command, BotDo) and command.action == "restore":
                assert b.ports.raid_body_holds() == (finite,)
                assert b.ports.run_body_proof(run) is not None
                restored.append(command)
            return await original(command)
        b.ground.ports.send = send
        await b.controller._close_pending()
        assert restored and not ts.open_applied(b.ports.conn, 0)
        assert any(isinstance(c, training_protocol.DcSetting) and c.op == "clear"
                   for c in b.guild_ports.sent)
        assert not b.controller._closing and b.ports.raid_body_holds() == ()
        assert b.bodies.owner_for_run(run) is None
        assert b.bodies.holder(101) is None
    asyncio.run(scenario())


@pytest.mark.parametrize("state", list(State))
def test_all_is_only_active_scheduling_intent(boundary, state):
    b = boundary
    b.controller.state = state
    holds = b.ports.raid_body_holds()
    assert bool(holds) == (state.name in {"WAITING_CALL", "CALLED", "FORMING", "ASSEMBLING", "RUNNING"})
    assert all(h.guids is ALL and not h.retained for h in holds)
    assert b.ports.run_body_proof(object()) is None


@pytest.mark.parametrize("change", ["replacement", "wrong_session", "listener_replacement", "closed",
    "closing", "unbound", "bound_mismatch", "plan", "started", "disconnected", "presence",
    "epoch_zero", "epoch_bool", "epoch_mismatch", "stopping", "human", "ended_without_closing"])
def test_proof_rejects_replacement_closed_unbound_and_connection_mismatches(boundary, change):
    b = boundary
    run = session_for(b)
    assert b.ports.run_body_proof(run).members == frozenset({101, 102})
    if change == "replacement":
        b.controller.run = replace(run)
    elif change == "wrong_session":
        b.controller.session = NS(raid_id=run.session.raid_id)
    elif change == "listener_replacement":
        b.ground.listener.run = replace(run)
    elif change == "closed":
        with b.ports.conn:
            b.ports.conn.execute("UPDATE guild_raid SET ended=started+1")
    elif change == "closing":
        run.session._closing = True
    elif change == "unbound":
        run.session.ctx.instance_id = None
    elif change == "bound_mismatch":
        with b.ports.conn:
            _put(b.ports.conn, f"guild:raid:{run.session.raid_id}:instance", 88)
    elif change == "plan":
        run.plan.members.append(103)
    elif change == "started":
        run.session.started += 1
    elif change == "disconnected":
        b.ground._connected = False
    elif change == "presence":
        b.ground._presence_known = False
    elif change == "epoch_zero":
        b.ground._epoch = b.ground.ports._generation = 0
    elif change == "epoch_bool":
        b.ground._epoch = b.ground.ports._generation = True
    elif change == "epoch_mismatch":
        b.ground.ports._generation += 1
    elif change == "stopping":
        b.ground._stopping = True
    elif change == "human":
        b.app.human_guid = 102
    elif change == "ended_without_closing":
        b.controller.state = State.ENDED
    assert b.ports.run_body_proof(run) is None


def test_all_alone_and_lost_session_slot_never_prove_a_run(boundary):
    b = boundary
    run = session_for(b)
    b.controller.run = b.controller.session = None
    with pytest.raises(RealmEvidenceUnavailable):
        b.ports.raid_body_holds()
    assert b.ports.run_body_proof(run) is None
    assert b.bodies.owner_for_run(run) is None


def test_offline_retains_exact_command_scope_and_terminal_meaning(boundary):
    b = boundary
    run = session_for(b, offline=True)
    owner = b.app.guild.offline._owned
    row, = b.ports.offline_body_holds()
    assert (row.scope_id, row.owner, row.rank, row.retained) == ("offline_original", "offline_raid", 1, True)
    assert b.ports.run_body_proof(run).scope_id == row.scope_id
    assert b.bodies.owner_for_run(run) == "offline_raid"
    assert b.ground.state_for(b.ground.listener.run_key(run)).native_run is None
    # A Python reservation cannot substitute for NativeCleanup's exact token.
    assert b.ground.applier._state(b.ground.listener.run_key(run)) is None
    owner.update(cancelled=True, phase="uncertain", accepted=[])
    assert b.ports.offline_body_holds() == (row,)
    owner["phase"] = "complete"
    assert b.ports.offline_body_holds() == ()
    assert b.ports.run_body_proof(run) is None


@pytest.mark.parametrize("value", ["unreadable", {"phase": "accepted"},
    {"id": "op", "phase": "accepted", "plan": {"members": [True]}},
    {"id": "", "phase": "complete", "plan": {"members": [101]}}])
def test_malformed_offline_source_is_unavailable(boundary, value):
    b = boundary
    b.app.guild.offline._owned = value
    with pytest.raises(RealmEvidenceUnavailable):
        b.ports.offline_body_holds()


@pytest.mark.parametrize("key,payload", [("tg:lab:owned", {"id": "lab", "phase": "blocked", "guids": [101]}),
    ("tg:benchmark:active", {"command_id": "benchmark", "members": [101]}),
    ("tg:lab:owned", "corrupt")])
def test_disabled_unattached_training_cannot_hide_durable_jobs(boundary, key, payload):
    b = boundary
    b.app.training = None
    assert b.ports.training_body_holds() == ()
    with b.ports.conn:
        _put(b.ports.conn, key, payload)
    with pytest.raises(RealmEvidenceUnavailable):
        b.ports.training_body_holds()


def test_disabled_training_cannot_hide_an_applier_journal(boundary):
    b = boundary
    b.app.training = None
    ts.log_applied(b.ports.conn, "orphan", "boss", "631:0:0", {}, None, b.app.clock.now())
    with pytest.raises(RealmEvidenceUnavailable):
        b.ports.training_body_holds()


def test_attached_ledger_preserves_unsupported_idle_training(boundary):
    b = boundary
    b.guild_ports.online = False
    b.controller.state = State.IDLE
    assert b.ground.state_for(None) is None
    assert asyncio.run(b.ground.applier.apply_standing((101,))) == 0
    assert not b.guild_ports.sent
    del b.app.bodies
    assert b.ground.state_for(None).run_key is None


@pytest.mark.parametrize("change", ["missing", "detached", "replacement", "terminal", "active_replacement"])
def test_all_sources_deny_detached_offline_journal_until_authoritative_resolution(boundary, change):
    b = boundary
    guids = tuple(range(101, 111))  # Synthetic ten-player operation satisfies native admission minimum.
    run = session_for(b, offline=True, guids=guids)
    controller = b.app.guild.offline
    key = b.ground.listener.run_key(run)
    native_state = "open"
    async def query(command, *args, **kwargs):
        return [TrainingCleanup(int(b.app.clock.now() * 1000), command.id, 1, "tr_original",
            "offline_original", "7", 101, 10, 1, 631, 77, "9", guids, native_state)]
    b.ground.ports.request = query
    with b.ports.conn:
        _put(b.ports.conn, "guild_id", 10)
    async def scenario():
        nonlocal native_state
        progress = Job(int(b.app.clock.now() * 1000), "offline_original", "offline_raid",
            "progress", "", "running", guids, (), 0, 0, 77, 0, 0, 0, "tr_original")
        assert controller._valid_job(progress)
        b.ground.native.feed(progress)
        assert await b.ground.native.prepare(run)
        members = b.ground.members_for(run)
        assert await b.ground.applier.apply(key, "boss", "631:0:0", card(Directive("threat", "+")),
                                           {102: members[102]}, tank_guid=101, lead_is_human=False)
        assert ts.open_applied(b.ports.conn, 0)
        b.ground.listener.invalidate(run)
        assert b.ground.listener.retained_run(key) is run
        assert b.ports.training_body_holds() == ()  # Valid journal is covered by the exact offline row.
        assert b.bodies.owner_for_run(run) == "offline_raid"
        assert b.bodies.claim("realm_day", guids, rank=4, until=b.app.clock.now() + 600) == set()
        assert not b.bodies.may_move("offline_raid", 101)
        original = copy.deepcopy(controller._owned)
        if change in {"missing", "detached"}:
            controller._owned = None
        elif change in {"replacement", "active_replacement"}:
            controller._owned = original | {"id": "replacement_operation",
                "phase": "accepted" if change == "active_replacement" else "complete"}
        else:
            controller._owned["phase"] = "complete"
        controller._save()
        if change == "detached":
            b.app.guild.offline = None
        assert b.ports.raid_body_holds() == ()
        if change != "active_replacement":
            assert b.ports.offline_body_holds() == ()
        # Training's retained run and valid journal alone cannot certify that
        # the Guild-side operation still reserves these bodies.
        assert b.ground.body_holds() == ()
        with pytest.raises(RealmEvidenceUnavailable):
            b.ports.training_body_holds()
        assert b.bodies.claim("realm_day", guids, rank=4, until=b.app.clock.now() + 600) == set()
        assert not b.bodies.may_move("realm_day", 101)
        assert b.bodies.owner_for_run(run) is None
        sent = len(b.guild_ports.sent)
        # An open native status cannot settle the journal or resume allocation.
        await b.ground.native.tick()
        assert ts.open_applied(b.ports.conn, 0)
        with pytest.raises(RealmEvidenceUnavailable):
            b.ports.training_body_holds()
        native_state = "complete"
        for _ in range(2):
            await b.ground.native.tick()  # Bounded cursor wraps, then consumes exact completion.
        assert not ts.open_applied(b.ports.conn, 0)
        assert len(b.guild_ports.sent) == sent  # Reconciliation never replays a mutation.
        assert b.ports.training_body_holds() == ()
        granted = b.bodies.claim("realm_day", guids, rank=4, until=b.app.clock.now() + 600)
        if change == "active_replacement":
            assert granted == set()  # Its separate retained operation still owns the bodies.
        else:
            assert granted == set(guids) and b.bodies.may_move("realm_day", 101)
    asyncio.run(scenario())


# -- the raid's hold, narrowed to his raid (owner ruling 2026-11-04, the middle option) ---------

HIS_GROUP = (1, 101, 900)  # Him, a pick and a non-cast addclass fill-in.
RIVAL = 950  # A rival officer, nowhere near his group.


def narrowed(b, *, group=HIS_GROUP, roster=(1, 101, 102, 103)):
    """Enable the Realm's ports with his group and the guild's roster as the app reads them."""
    from conftest import mem
    from leeroy_mind.protocol import Group
    b.app.world = NS(group=None if group is None else Group(0, 1, True, tuple(group)))
    if roster:
        b.app.store.upsert_cast([mem(guid) for guid in roster], 1)
    b.ports.set_enabled(True)


def scheduling(b):
    return next(h for h in b.ports.raid_body_holds() if h.scope_id == "raid:scheduling")


def test_raid_hold_covers_his_group_and_the_raids_picks(boundary):
    b = boundary
    run = session_for(b, guids=(101, 102, 103))  # 103 is a late arrival, not grouped yet.
    narrowed(b)
    hold = scheduling(b)
    assert hold.guids == frozenset({101, 102, 103, 900}) and not hold.retained
    assert b.bodies.owner_for_run(run) == "raid"
    for guid in (101, 102, 103, 900):
        assert not b.bodies.may_move("realm_day", guid)
    assert b.bodies.may_move("realm_day", RIVAL)  # Rival officers can be moved into capitals.
    assert b.bodies.may_move("raid", 900)


def test_rival_officer_outside_his_group_is_not_held(boundary):
    b = boundary
    session_for(b)
    narrowed(b, group=(1, 101, 102))
    assert b.bodies.may_move("realm_day", RIVAL) and not b.bodies.may_move("realm_day", 101)
    assert b.bodies.claim("realm_day", {RIVAL}, rank=4, until=b.app.clock.now() + 600) == {RIVAL}


@pytest.mark.parametrize("state", [State.WAITING_CALL, State.CALLED, State.FORMING])
def test_waiting_call_holds_the_cast_and_his_group(boundary, state):
    b = boundary
    b.controller.state = state
    narrowed(b, group=(1, 900))
    assert b.ports.raid_body_holds() == (scheduling(b),)
    assert scheduling(b).guids == frozenset({101, 102, 103, 900})  # Never him.
    assert not b.bodies.may_move("realm_day", 103) and b.bodies.may_move("realm_day", RIVAL)
    narrowed(b, group=None)  # Out of any group: the guild alone.
    assert scheduling(b).guids == frozenset({101, 102, 103})


@pytest.mark.parametrize("unreadable", ["no_session", "group", "roster"])
def test_unreadable_member_list_falls_back_to_all(boundary, caplog, unreadable):
    b = boundary
    if unreadable == "no_session":
        b.controller.state = State.RUNNING  # Formed, but its member list is not there.
        narrowed(b)
    elif unreadable == "group":
        session_for(b)
        narrowed(b)
        b.app.world.group = NS(leader_guid=1, members=[101, "900"])
    else:
        b.controller.state = State.WAITING_CALL
        narrowed(b, roster=())
    for _ in range(3):
        assert scheduling(b).guids is ALL
    assert not b.bodies.may_move("realm_day", RIVAL)
    assert caplog.text.count("realm_raid_hold_fallback") == 1  # Once, not at every poll.


def test_fallback_logs_again_after_a_good_read(boundary, caplog):
    b = boundary
    b.controller.state = State.WAITING_CALL
    narrowed(b)
    world = b.app.world
    b.app.world = NS()  # No group evidence at all.
    assert scheduling(b).guids is ALL and scheduling(b).guids is ALL
    b.app.world = world
    assert scheduling(b).guids is not ALL
    b.app.world = NS()
    assert scheduling(b).guids is ALL
    assert caplog.text.count("realm_raid_hold_fallback") == 2


def test_hold_released_when_the_raid_night_ends(boundary):
    b = boundary
    run = session_for(b)
    narrowed(b)
    assert scheduling(b).guids == frozenset({101, 102, 900})
    b.controller._end("ended")
    holds = b.ports.raid_body_holds()
    # Only the closing session's own exact, retained reservation is left; his fill-in is free.
    assert [h.scope_id for h in holds if h.scope_id == "raid:scheduling"] == []
    assert [h.guids for h in holds] == [frozenset({101, 102})] and holds[0].retained
    assert b.bodies.may_move("realm_day", 900) and b.bodies.owner_for_run(run) == "raid"
    asyncio.run(b.controller._close_pending())
    assert b.ports.raid_body_holds() == () and b.bodies.may_move("realm_day", 101)


@pytest.mark.parametrize("state", [State.WAITING_CALL, State.RUNNING])
def test_nothing_changes_while_realm_is_disabled(boundary, state):
    b = boundary
    if state is State.RUNNING:
        session_for(b)
    else:
        b.controller.state = state
    narrowed(b)
    b.ports.set_enabled(False)
    assert scheduling(b).guids is ALL and not scheduling(b).retained
    assert not b.bodies.may_move("realm_day", RIVAL) and not b.bodies.may_move("training", RIVAL)
