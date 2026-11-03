import asyncio
from dataclasses import replace
import random

import pytest

from leeroy_mind.guild import play
from leeroy_mind.guild.play import Play, drunk, skips_flask, spacing_actions, summon_waves
from leeroy_mind.guild.protocol import BotDo, JobSkip
from leeroy_mind.guild.raid_night import RaidRun
from leeroy_mind.guild.session import RaidSession
from leeroy_mind.storage.rows import PersonaStateRow
from .fakes import job, persona
from .test_session import _run_async, advance, setup as session_setup


@pytest.fixture
def setup(session_setup):
    ports, cfg, armory, plan = session_setup
    for guid, cls, spec, role in ((101, 1, 2, "tank"), (102, 5, 0, "healer"), (103, 5, 1, "healer"),
                                 (104, 1, 0, "melee"), (105, 8, 1, "ranged"), (106, 4, 0, "melee"),
                                 (107, 3, 1, "ranged")):
        ports.members[guid] = replace(ports.members[guid], class_=cls, spec_tab=spec)
        ports.personas[guid].play.raid_role = role

    async def wait_job(cmd_id, timeout_s):
        command = next(c for c in reversed(ports.sent) if c.id == cmd_id)
        script = ports.jobs["bot_do"]
        if script:
            result = script.popleft()
            if callable(result):
                result = result(command)
            if result is None:
                return None
            return replace(result, id=cmd_id) if not result.id else result
        return job("bot_do", "done", id=cmd_id, guids=command.guids)

    ports.wait_job = wait_job
    return ports, cfg, armory, plan, Play(ports, random.Random(3))


async def boot(setup, drunk_guids=()):
    ports, cfg, armory, plan, listener = setup
    for guid in drunk_guids:
        ports.states[guid] = PersonaStateRow(guid, altered_state="drunk")
    session = await RaidSession.open(ports, cfg, armory, plan)
    assert session.bind_instance(77)
    run = RaidRun(session.plan, session, plan.human_in_raid, plan.lead_guid == ports.human_guid)
    await listener.on_start(run)
    await listener.tick()
    return run


async def end(setup, run):
    await run.session.close("called")
    await setup[-1].on_end("called")
    await setup[-1].tick()


def commands(ports, param):
    return [c for c in ports.sent if isinstance(c, BotDo) and c.param == param]


def test_spacing_only_melee_behind_actual_tanks_face_and_optional_ranged(monkeypatch):
    roles = {104: "damage", 105: "damage", 101: "tank", 102: "healer", 106: "damage"}
    raid_roles = {104: "melee", 105: "ranged", 101: "tank", 102: "healer", 106: "tank"}
    monkeypatch.setattr(play, "RANGED_FORMATION", None)
    assert spacing_actions(roles, raid_roles) == [("co", "+behind", [104]), ("co", "+tank face", [101])]
    monkeypatch.setattr(play, "RANGED_FORMATION", "circle")
    assert spacing_actions(roles, raid_roles)[-1] == ("formation", "circle", [102, 105])
    monkeypatch.setattr(play, "RANGED_FORMATION", "chaos")
    assert spacing_actions(roles, raid_roles)[-1][1] == "chaos"
    monkeypatch.setattr(play, "RANGED_FORMATION", "stack")
    with pytest.raises(ValueError):
        spacing_actions(roles, raid_roles)


def test_late_members_by_reliability_are_seeded_and_order_independent():
    personas = {g: persona(g, f"M{g}", conscientiousness=score) for g, score in ((101, 2), (102, 3), (103, 4), (104, 9))}
    on_time, late = summon_waves([104, 102, 101, 103, 105, 101], personas, random.Random(8), (60, 180))
    assert on_time == [103, 104, 105]
    assert [g for g, _ in late] == [101, 102]
    assert all(60 <= delay <= 180 for _, delay in late)
    assert (on_time, late) == summon_waves([101, 102, 103, 104, 105], personas, random.Random(8), (60, 180))
    with pytest.raises(ValueError):
        summon_waves([101], personas, random.Random(8), (180, 60))


def test_drink_and_flask_rules_preserve_clutch_and_theorycrafter():
    sober = PersonaStateRow(101)
    drunk_state = replace(sober, altered_state="drunk")
    assert drunk(drunk_state) and not drunk(sober) and not drunk(replace(sober, altered_state="hungover"))
    for skill, thinks in (("clutch", "social"), ("sloppy", "theorycrafter")):
        p = persona(101, "Aster", skill=skill, thinks_as=thinks)
        assert all(not skips_flask(p, drunk_state, random.Random(seed)) for seed in range(20))
    assert not skips_flask(persona(101, "Aster"), sober, random.Random(1))
    for p, state in ((persona(101, "Aster", skill="sloppy"), sober), (persona(101, "Aster"), drunk_state)):
        assert 70 < sum(skips_flask(p, state, random.Random(seed)) for seed in range(200)) < 130


@_run_async
async def test_hooks_queue_work_and_spacing_is_sent_once(setup):
    ports, cfg, armory, plan, listener = setup
    session = await RaidSession.open(ports, cfg, armory, plan)
    session.bind_instance(77)
    run = RaidRun(plan, session, True, False)
    await listener.on_start(run)
    assert not ports.sent
    await listener.tick()
    assert commands(ports, "+behind")[0].guids == (104, 106)
    assert commands(ports, "+tank face")[0].guids == (101,)
    before = len(ports.sent)
    await listener.on_start(run)
    await listener.tick()
    assert len(ports.sent) == before


@_run_async
async def test_drunk_boost_removed_then_restored_only_at_check_or_end(setup):
    ports, _, _, _, listener = setup
    run = await boot(setup, (104, 105))
    assert commands(ports, "-boost")[0].guids == (104, 105)
    ports.states[104] = PersonaStateRow(104, altered_state="hungover")
    await advance(ports, 299)
    await listener.tick()
    assert not commands(ports, "+boost")
    await advance(ports, 1)
    await listener.tick()
    assert commands(ports, "+boost")[-1].guids == (104,)
    await end(setup, run)
    assert commands(ports, "+boost")[-1].guids == (105,)
    assert listener._owned == {}


@_run_async
async def test_new_drunk_state_applies_after_300_seconds(setup):
    ports, _, _, _, listener = setup
    await boot(setup)
    ports.states[106] = PersonaStateRow(106, altered_state="drunk")
    await advance(ports, 299)
    await listener.tick()
    assert not commands(ports, "-boost")
    await advance(ports, 1)
    await listener.tick()
    assert commands(ports, "-boost")[-1].guids == (106,)


@pytest.mark.parametrize("bad", ["id", "kind", "state", "timeout", "extra", "missing"])
@_run_async
async def test_ack_alone_never_confirms_removal_and_unknown_work_is_restored(setup, bad):
    ports, _, _, _, listener = setup
    original = ports.wait_job

    async def result(cmd_id, timeout_s):
        command = next(c for c in reversed(ports.sent) if c.id == cmd_id)
        if command.param != "-boost":
            return await original(cmd_id, timeout_s)
        if bad == "timeout":
            return None
        return job("form_raid" if bad == "kind" else "bot_do", "progress" if bad == "state" else "done",
                   id="foreign" if bad == "id" else cmd_id,
                   guids=(999,) if bad == "extra" else () if bad == "missing" else command.guids)

    ports.wait_job = result
    run = await boot(setup, (104,))
    assert 104 in listener._owned
    await end(setup, run)
    assert commands(ports, "+boost")[-1].guids == (104,)
    assert not listener._owned


@_run_async
async def test_skipped_or_rejected_removal_has_no_unowned_restore(setup):
    ports, _, _, _, listener = setup
    original = ports.wait_job

    async def result(cmd_id, timeout_s):
        command = next(c for c in reversed(ports.sent) if c.id == cmd_id)
        if command.param == "-boost":
            return job("bot_do", "done", id=cmd_id, guids=(104,), skipped=(JobSkip(105, "offline"),))
        return await original(cmd_id, timeout_s)

    ports.wait_job = result
    run = await boot(setup, (104, 105))
    assert set(listener._owned) == {104}
    await end(setup, run)
    assert commands(ports, "+boost")[-1].guids == (104,)


@_run_async
async def test_unknown_restore_retries_are_bounded_and_require_valid_job(setup):
    ports, _, _, _, listener = setup
    run = await boot(setup, (104,))
    original = ports.wait_job

    async def result(cmd_id, timeout_s):
        command = next(c for c in reversed(ports.sent) if c.id == cmd_id)
        return None if command.param == "+boost" else await original(cmd_id, timeout_s)

    ports.wait_job = result
    await end(setup, run)
    assert 104 in listener._owned
    for _ in range(4):
        await advance(ports, 60)
        await listener.tick()
    assert len(commands(ports, "+boost")) == 3 and 104 in listener._owned


@_run_async
async def test_human_and_changed_roster_are_never_targeted(setup):
    ports, _, _, _, listener = setup
    run = await boot(setup, (104,))
    ports.members[1] = replace(ports.members[104], guid=1)
    ports.personas[1] = persona(1, "Testy")
    ports.states[1] = PersonaStateRow(1, altered_state="drunk")
    ports.members[104] = replace(ports.members[104], spec_tab=1)
    await end(setup, run)
    assert not commands(ports, "+boost")
    assert all(1 not in c.guids for c in ports.sent)


@_run_async
async def test_owner_leading_still_gets_members_own_mild_habits(setup):
    ports, cfg, armory, plan, listener = setup
    plan = replace(plan, lead_guid=ports.human_guid)
    run = await boot((ports, cfg, armory, plan, listener), (104,))
    assert commands(ports, "-boost") and commands(ports, "+behind")
    await end(setup, run)
    assert commands(ports, "+boost")


@_run_async
async def test_effects_use_only_approved_mild_whitelist(setup, monkeypatch):
    ports, _, _, _, listener = setup
    monkeypatch.setattr(play, "RANGED_FORMATION", "circle")
    run = await boot(setup, (104,))
    await end(setup, run)
    allowed = {("co", "+behind"), ("co", "+tank face"), ("co", "-boost"), ("co", "+boost"), ("formation", "circle")}
    assert {(c.action, c.param) for c in ports.sent} <= allowed
    assert all(isinstance(c, BotDo) and c.target_guid == 0 for c in ports.sent)


@_run_async
async def test_cancelled_removal_keeps_compensation_and_never_repeats_removal(setup):
    ports, cfg, armory, plan, listener = setup
    entered = asyncio.Event()
    original = ports.wait_job

    async def result(cmd_id, timeout_s):
        command = next(c for c in reversed(ports.sent) if c.id == cmd_id)
        if command.param == "-boost":
            entered.set()
            await asyncio.Event().wait()
        return await original(cmd_id, timeout_s)

    ports.wait_job = result
    task = asyncio.create_task(boot(setup, (104,)))
    await entered.wait()
    run = listener._run
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    ports.wait_job = original
    await end(setup, run)
    assert len(commands(ports, "-boost")) == len(commands(ports, "+boost")) == 1


@_run_async
async def test_late_old_end_cannot_clear_new_active_run(setup):
    ports, cfg, armory, plan, listener = setup
    old = await boot(setup, (104,))
    await old.session.close("called")
    session = await RaidSession.open(ports, cfg, armory, plan)
    session.bind_instance(78)
    new = RaidRun(session.plan, session, True, False)
    await listener.on_start(new)
    await listener.on_end("called")
    assert listener._run is new
    await listener.tick()
    assert not commands(ports, "+boost")


@_run_async
async def test_old_restore_ack_cannot_clear_new_run_compensation(setup):
    ports, cfg, armory, plan, listener = setup
    old = await boot(setup, (104,))
    await old.session.close("called")
    await listener.on_end("called")
    entered, release = asyncio.Event(), asyncio.Event()
    original = ports.wait_job

    async def result(cmd_id, timeout_s):
        command = next(c for c in reversed(ports.sent) if c.id == cmd_id)
        if command.param == "+boost":
            entered.set()
            await release.wait()
        return await original(cmd_id, timeout_s)

    ports.wait_job = result
    task = asyncio.create_task(listener.tick())
    await entered.wait()
    session = await RaidSession.open(ports, cfg, armory, plan)
    session.bind_instance(78)
    new = RaidRun(session.plan, session, True, False)
    await listener.on_start(new)
    release.set()
    await task
    await listener.tick()
    assert listener._owned[104].raid_id == session.raid_id
    assert ports.sent[-1].param == "-boost"
    assert len(commands(ports, "-boost")) == 2


@_run_async
async def test_unbound_session_cannot_apply_play_effects(setup):
    ports, cfg, armory, plan, listener = setup
    ports.states[104] = PersonaStateRow(104, altered_state="drunk")
    session = await RaidSession.open(ports, cfg, armory, plan)
    await listener.on_start(RaidRun(session.plan, session, True, False))
    await listener.tick()
    assert not ports.sent and not listener._owned
    # An in-memory value is not a validated persisted binding either.
    session.ctx.instance_id = 77
    await listener.on_start(RaidRun(session.plan, session, True, False))
    await listener.tick()
    assert not ports.sent


@_run_async
async def test_mutated_plan_cannot_target_a_benched_member(setup):
    ports, cfg, armory, plan, listener = setup
    session = await RaidSession.open(ports, cfg, armory, plan)
    session.bind_instance(77)
    session.plan.members.append(108)
    session.plan.bench.remove(108)
    ports.states[108] = PersonaStateRow(108, altered_state="drunk")
    await listener.on_start(RaidRun(session.plan, session, True, False))
    await listener.tick()
    assert not ports.sent and not listener._owned


@_run_async
async def test_blocked_send_is_bounded_and_retains_boost_compensation(setup, monkeypatch):
    ports, _, _, _, listener = setup
    original = ports.send
    monkeypatch.setattr(play, "JOB_TIMEOUT_S", 0.02)

    async def blocked(command):
        ack = await original(command)
        if command.param == "-boost":
            await asyncio.Event().wait()
        return ack

    ports.send = blocked
    run = await asyncio.wait_for(boot(setup, (104,)), timeout=1)
    assert 104 in listener._owned and not listener._lock.locked()
    ports.send = original
    await end(setup, run)
    assert len(commands(ports, "-boost")) == len(commands(ports, "+boost")) == 1
    assert not listener._owned


@pytest.mark.parametrize("unknown", [False, True])
@_run_async
async def test_recreated_listener_restores_ended_raid_owned_boost(setup, unknown):
    ports, _, _, _, listener = setup
    original = ports.wait_job

    async def result(cmd_id, timeout_s):
        command = next(c for c in reversed(ports.sent) if c.id == cmd_id)
        return None if unknown and command.param == "-boost" else await original(cmd_id, timeout_s)

    ports.wait_job = result
    run = await boot(setup, (104,))
    assert 104 in listener._owned
    await run.session.close("called")
    recreated = Play(ports, random.Random(9))
    assert recreated._owned[104].fingerprint == listener._owned[104].fingerprint
    await recreated.tick()
    assert commands(ports, "+boost")[-1].guids == (104,) and not recreated._owned
    again = Play(ports, random.Random(10))
    await again.tick()
    assert len(commands(ports, "+boost")) == 1 and len(commands(ports, "-boost")) == 1


@_run_async
async def test_recreated_listener_keeps_restore_attempt_bound(setup):
    ports, _, _, _, listener = setup
    run = await boot(setup, (104,))
    await run.session.close("called")
    original = ports.wait_job

    async def result(cmd_id, timeout_s):
        command = next(c for c in reversed(ports.sent) if c.id == cmd_id)
        return None if command.param == "+boost" else await original(cmd_id, timeout_s)

    ports.wait_job = result
    for _ in range(5):
        recreated = Play(ports, random.Random(2))
        await recreated.tick()
        await advance(ports, 60)
    assert len(commands(ports, "+boost")) == 3


@pytest.mark.parametrize("offline", [False, True])
@_run_async
async def test_presence_flip_stops_new_play_effects(setup, offline):
    ports, cfg, armory, plan, listener = setup
    ports.online = not offline
    plan = replace(plan, offline=offline, human_in_raid=not offline)
    await boot((ports, cfg, armory, plan, listener))
    before = len(ports.sent)
    ports.online = offline
    ports.states[104] = PersonaStateRow(104, altered_state="drunk")
    await advance(ports, 300)
    await listener.tick()
    assert len(ports.sent) == before and not commands(ports, "-boost")


@_run_async
async def test_recovery_never_restores_into_a_newer_active_raid(setup):
    ports, cfg, armory, plan, listener = setup
    old = await boot(setup, (104,))
    await old.session.close("called")
    new = await RaidSession.open(ports, cfg, armory, plan)
    new.bind_instance(78)
    recreated = Play(ports, random.Random(2))
    await recreated.tick()
    assert not commands(ports, "+boost") and 104 in recreated._owned


@_run_async
async def test_changed_human_identity_cannot_apply_old_compensation(setup):
    ports, _, _, _, listener = setup
    run = await boot(setup, (104,))
    await run.session.close("called")
    ports.human_guid = 2
    await listener.tick()
    assert not commands(ports, "+boost")
    recreated = Play(ports, random.Random(2))
    assert not recreated._owned


@_run_async
async def test_recovery_waits_for_original_run_to_end_without_fresh_attachment(setup):
    ports, _, _, _, listener = setup
    run = await boot(setup, (104,))
    recreated = Play(ports, random.Random(2))
    await recreated.tick()
    assert not commands(ports, "+boost")
    await run.session.close("called")
    await recreated.tick()
    assert commands(ports, "+boost")[-1].guids == (104,)
