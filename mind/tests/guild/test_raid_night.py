import asyncio
from dataclasses import replace
from functools import wraps
import random
import threading

import pytest

from leeroy_mind.guild.config import GuildConfig
from leeroy_mind.guild.protocol import ClearLockout, FormRaid, JobSkip, RaidControl, SetLootMaster, Summon
from leeroy_mind.guild.raid_night import RaidNight, State
from leeroy_mind.guild.store import new_raid, record_attendance
from leeroy_mind.protocol import Ack, Boss, Chat, DcStatus, Death, Hello, Login, Logout, Zone
from leeroy_mind.talk.turns import TurnKind
from .fakes import FakeArmory, job, member, persona


def _run_async(test):
    @wraps(test)
    def run(*args, **kwargs):
        return asyncio.run(test(*args, **kwargs))
    return run


@pytest.fixture
def setup(fake):
    tanks = {101, 103, 105, 106, 107}
    healers = {102, *range(111, 118)}
    titles = {101: "Raid Lead", 102: "Healing", 104: "Raid Lead, DKP", 107: "Tank",
              108: "Raid Lead, Historian", 109: "Raid Lead, Theorycraft", 120: "Warlock"}
    names = {101: "Aster", 102: "Belra", 104: "Cobalt", 108: "Dorin", 109: "Elva", 120: "Fable"}
    for guid in range(101, 131):
        cls, spec = (1, 2) if guid in tanks else (5, 0) if guid in healers else (9, 1) if guid == 120 else (8, 1)
        name = names.get(guid, f"M{guid}")
        fake.members[guid] = member(guid, name, cls, spec)
        fake.personas[guid] = persona(guid, name, guild_role="officer" if guid in titles else "raider",
                                        officer_title=titles.get(guid, ""))
    prior = new_raid(fake.conn, started=fake.clock.now() - 86400, raid_key="naxx", size=25,
                     heroic=False, offline=True, lead_guid=101, tank_guid=101, loot_guid=104)
    fake.conn.execute("UPDATE guild_raid SET ended=started+100 WHERE raid_id=?", (prior,))
    record_attendance(fake.conn, prior, [], [130])

    async def wait_job(cmd_id, timeout_s):
        command = next(cmd for cmd in reversed(fake.sent) if cmd.id == cmd_id)
        kind = "form_raid" if isinstance(command, FormRaid) else "summon"
        script = fake.jobs[kind]
        if script:
            result = script.popleft()
            if callable(result):
                result = result(command)
            if result is None:
                return None
            return replace(result, id=cmd_id) if not result.id else result
        guids = command.members if isinstance(command, FormRaid) else command.guids
        return job(kind, "done", id=cmd_id, guids=guids,
                   instance_id=77 if isinstance(command, Summon) and command.where == "inside" else 0)

    fake.wait_job = wait_job
    armory = FakeArmory({}, {})
    return fake, armory, RaidNight(fake, GuildConfig(), armory, random.Random(7))


def chat(ports, text, **kwargs):
    defaults = dict(ts=int(ports.clock.now() * 1000), channel="guild", from_guid=1,
                    from_name="Testmage", to_guid=0, text=text, map=1, zone=1637)
    defaults.update(kwargs)
    return Chat(**defaults)


async def advance(ports, seconds):
    await ports.clock.advance(seconds, step=max(seconds, 1))


async def form(setup, text="icc 25"):
    ports, _, night = setup
    if night.state is State.IDLE:
        night.state = State.CALLED
        night._call_deadline = ports.clock.now() + 600
    await night.on_chat(chat(ports, text))
    await night.tick()
    return night


async def inside(setup):
    ports, _, night = setup
    await night.on_zone(Zone(0, 1, 4812, "Icecrown Citadel"))
    await night.tick()


async def running(setup, text="icc 25"):
    ports, _, night = setup
    await form(setup, text)
    await inside(setup)
    await advance(ports, 30)
    await night.tick()
    await advance(ports, 20)
    await night.tick()
    assert night.state is State.RUNNING
    return night


def controls(ports, action):
    return [c for c in ports.sent if isinstance(c, RaidControl) and c.action == action]


@_run_async
async def test_calls_raid_once_after_login_and_ignores_duplicate_login(setup):
    ports, _, night = setup
    await night.on_login(Login(0, 1, True))
    await night.tick()
    # Nothing the mind formed is standing, so his own group is left alone.
    assert len(controls(ports, "disband")) == 0
    await advance(ports, 59)
    await night.on_login(Login(0, 1, True))
    await night.tick()
    assert ports.proposed == []
    await advance(ports, 16)
    await night.tick()
    assert night.state is State.CALLED
    assert len(ports.proposed) == 1
    assert ports.proposed[0][:2] == (101, "guild")
    assert "Testy" in ports.proposed[0][2]
    await night.tick()
    assert len(ports.proposed) == 1


@_run_async
async def test_pick_runs_formation_in_order_with_clean_owned_roster(setup):
    ports, _, night = setup
    await form(setup)
    assert [type(c) for c in ports.sent] == [ClearLockout, RaidControl, FormRaid, SetLootMaster, Summon]
    clear, clean, formed, loot, summon = ports.sent
    assert clean.action == "disband"
    assert set(clear.guids) == {1, *formed.members}
    assert (formed.leader_guid, formed.main_tank_guid, formed.map_id, formed.size) == (1, 101, 631, 25)
    assert len(formed.members) == len(set(formed.members)) == 24
    assert {101, 104, 120} <= set(formed.members)
    assert 1 not in formed.assistants and {101, 104} <= set(formed.assistants)
    assert (loot.member_guid, loot.looter_guid, loot.threshold) == (1, 104, 4)
    assert (summon.where, summon.summon_human, summon.summoner_guid) == ("entrance", True, 120)
    assert night.state is State.ASSEMBLING and night.active
    assert night.session.ctx.instance_id is None


@pytest.mark.parametrize("error", ["no_group", "not_cast:999"])
@_run_async
async def test_owned_group_cleanup_only_tolerates_no_group(setup, error):
    ports, _, night = setup
    ports.acks["raid_control"].append((False, error))
    await form(setup)
    assert any(isinstance(c, FormRaid) for c in ports.sent) == (error == "no_group")
    assert night.state is (State.ASSEMBLING if error == "no_group" else State.ENDED)


@_run_async
async def test_clear_lockout_in_instance_retries_once_without_human(setup):
    ports, _, night = setup
    ports.acks["clear_lockout"].append((False, "in_instance:1"))
    await form(setup)
    clears = [c for c in ports.sent if isinstance(c, ClearLockout)]
    assert len(clears) == 2 and 1 in clears[0].guids and 1 not in clears[1].guids
    assert night.state is State.ASSEMBLING


@_run_async
async def test_backfills_same_role_once_and_does_not_count_skipped_members(setup):
    ports, _, night = setup
    first = []

    def skip_first(command):
        first.extend(command.members)
        assert 130 in command.members
        return job("form_raid", "done", guids=tuple(g for g in command.members if g != 130),
                   skipped=(JobSkip(130, "in_bg"),))

    def skip_replacement(command):
        replacement = next(g for g in command.members if g not in first)
        return job("form_raid", "done", guids=tuple(g for g in command.members if g != replacement),
                   skipped=(JobSkip(replacement, "offline"),))

    ports.jobs["form_raid"].extend([skip_first, skip_replacement])
    await form(setup)
    forms = [c for c in ports.sent if isinstance(c, FormRaid)]
    assert len(forms) == 2 and len(controls(ports, "disband")) == 1
    replacement = next(g for g in forms[1].members if g not in first)
    assert ports.members[replacement].class_ == 8
    assert 130 not in night.session.plan.members and replacement not in night.session.plan.members
    assert len(night.session.plan.members) == 23
    assert ports.conn.execute("SELECT 1 FROM guild_attendance WHERE raid_id=? AND guid=?",
                              (night.session.raid_id, 130)).fetchone() is None


@pytest.mark.parametrize("guid", [101, 104, 102])
@_run_async
async def test_missing_required_lead_tank_or_loot_aborts(setup, guid):
    ports, _, night = setup
    ports.jobs["form_raid"].append(lambda c: job("form_raid", "done",
        guids=tuple(g for g in c.members if g != guid), skipped=(JobSkip(guid, "offline"),)))
    await form(setup, "icc 25, belra leads")
    assert night.state is State.ENDED and night.session is None
    assert not any(isinstance(c, SetLootMaster) for c in ports.sent)
    assert len(controls(ports, "disband")) == 2


@pytest.mark.parametrize("mode", ["timeout", "id", "kind", "state", "missing", "extra", "duplicate"])
@_run_async
async def test_form_jobs_require_matching_terminal_success_and_exact_membership(setup, mode):
    ports, _, night = setup

    def result(command):
        if mode == "timeout":
            return None
        changes = dict(guids=command.members)
        if mode == "id":
            changes["id"] = "other-command"
        if mode == "missing":
            changes["guids"] = command.members[:-1]
        if mode == "extra":
            changes["guids"] = (*command.members, 999)
        if mode == "duplicate":
            changes["guids"] = (*command.members, command.members[0])
        return job("summon" if mode == "kind" else "form_raid", "running" if mode == "state" else "done", **changes)

    ports.jobs["form_raid"].append(result)
    await form(setup)
    assert night.state is State.ENDED and night.session is None
    assert not any(isinstance(c, SetLootMaster) for c in ports.sent)


@_run_async
async def test_ack_id_mismatch_never_advances(setup):
    ports, _, night = setup
    original = ports.send

    async def wrong(command):
        result = await original(command)
        return replace(result, id="wrong") if isinstance(command, ClearLockout) else result

    ports.send = wrong
    await form(setup)
    assert night.state is State.ENDED and len(ports.sent) == 1


@_run_async
async def test_inside_ready_dc_order_and_listener_isolation(setup):
    ports, _, night = setup
    heard = []

    class Broken:
        async def on_start(self, run):
            raise RuntimeError("private exception text")

    class Listener:
        async def on_start(self, run):
            heard.append(("start", run.lead_is_human))
        async def on_ready(self, run):
            assert not controls(ports, "dc_on")
            heard.append(("ready", run.session.ctx.instance_id))
        async def on_boss(self, ev):
            heard.append(("boss", ev.boss_id))
        async def on_end(self, outcome):
            heard.append(("end", outcome))

    night.add_listener(Broken())
    night.add_listener(Listener())
    await form(setup)
    await inside(setup)
    assert night.session.ctx.instance_id == 77 and not controls(ports, "ready_check")
    await advance(ports, 30)
    await night.tick()
    assert controls(ports, "ready_check")[-1].leader_guid == 101
    assert ports.said[-1] == (101, "raid", "ready check")
    assert heard == [("start", False), ("ready", 77)]
    await advance(ports, 20)
    await night.tick()
    assert controls(ports, "dc_on")[-1].leader_guid == 101 and ports.leads[-1] == 101
    await night.on_boss(Boss(0, 631, 77, 123, "Testboss", "killed", (1, 101)))
    await night.tick()
    await night.on_chat(chat(ports, "calling it"))
    assert night.state is State.ENDED
    await night.tick()
    assert heard[-2:] == [("boss", 123), ("end", "called")]


@pytest.mark.parametrize("mode", ["timeout", "failed", "zero", "id", "kind", "skipped"])
@_run_async
async def test_inside_summon_failure_or_unproved_instance_never_starts_dc(setup, mode):
    ports, _, night = setup
    await form(setup)

    def result(command):
        if mode == "timeout":
            return None
        return job("form_raid" if mode == "kind" else "summon", "failed" if mode == "failed" else "done",
                   id="wrong" if mode == "id" else "", instance_id=0 if mode == "zero" else 77,
                   guids=command.guids[:-1] if mode == "skipped" else command.guids,
                   skipped=(JobSkip(command.guids[-1], "offline"),) if mode == "skipped" else ())

    ports.jobs["summon"].append(result)
    await inside(setup)
    await advance(ports, 90)
    await night.tick()
    assert night.state is State.ENDED
    assert not controls(ports, "dc_on") and not controls(ports, "ready_check")


@_run_async
async def test_zone_events_alone_never_bind_an_instance(setup):
    ports, _, night = setup
    await form(setup)
    for guid in night.session.plan.members:
        await night.on_zone(Zone(0, guid, 4812, "ICC"))
    assert night.session.ctx.instance_id is None
    await inside(setup)
    assert isinstance(ports.sent[-1], Summon) and ports.sent[-1].where == "inside"
    assert night.session.ctx.instance_id == 77


@_run_async
async def test_human_leads_without_human_proposals_or_dc(setup):
    ports, _, night = setup
    await form(setup, "icc 10, i'll lead")
    await inside(setup)
    assert night.state is State.RUNNING and ports.leads[-1] == 1
    assert night.run.lead_is_human
    await advance(ports, 90)
    await night.tick()
    assert not controls(ports, "dc_on") and not controls(ports, "ready_check")
    assert all(g != 1 for g, _, _, _ in ports.proposed)


@_run_async
async def test_delegated_lead_is_mandatory_and_keeps_actual_tank(setup):
    ports, _, night = setup
    await running(setup, "icc 10, belra leads")
    assert night.session.plan.lead_guid == 102 and night.session.plan.tank_guid == 101
    assert {101, 102, 104} <= set(night.session.plan.members)
    assert ports.leads[-1] == 102


@_run_async
async def test_rotates_to_never_led_officer_and_announces_first(setup):
    ports, _, night = setup
    for guid in (104, 108):
        raid_id = new_raid(ports.conn, started=ports.clock.now() - 1000, raid_key="icc", size=25,
                           heroic=False, offline=False, lead_guid=guid, tank_guid=101, loot_guid=104)
        ports.conn.execute("UPDATE guild_raid SET ended=started+100 WHERE raid_id=?", (raid_id,))
    await form(setup, "icc 25, someone else lead")
    assert night.session.plan.lead_guid == 109
    taking = next(i for i, p in enumerate(ports.proposed) if p[0] == 109 and p[1] == "guild")
    forming = next(i for i, p in enumerate(ports.proposed) if p[0] == 109 and p[1] == "raid")
    assert taking < forming


@_run_async
async def test_hangout_stays_quiet_but_explicit_pick_still_works(setup):
    ports, _, night = setup
    await night.on_login(Login(0, 1, True))
    await night.on_chat(chat(ports, "no raid, let's just chill"))
    await advance(ports, 7200)
    await night.tick()
    assert night.state is State.ENDED and len(ports.proposed) == 1
    await night.on_chat(chat(ports, "icc was rough yesterday"))
    assert night.state is State.ENDED
    await night.on_chat(chat(ports, "can we do icc 10"))
    await night.tick()
    assert night.state is State.ASSEMBLING


@_run_async
async def test_break_never_benches_a_priority_member_for_owner_raid(setup):
    ports, _, night = setup
    ports.away_set = {130}
    await form(setup)
    assert 130 in night.session.plan.members


@_run_async
async def test_late_member_has_one_delayed_summon_and_own_aside(setup):
    ports, _, night = setup
    night.summon_plan = lambda members: ([g for g in members if g != 130], [(130, 90)])
    await form(setup)
    first = next(c for c in ports.sent if isinstance(c, Summon))
    assert 130 not in first.guids
    await advance(ports, 89)
    await night.tick()
    assert not any(isinstance(c, Summon) and c.guids == (130,) for c in ports.sent)
    await advance(ports, 1)
    await night.tick()
    assert ports.sent[-1].guids == (130,) and ports.sent[-1].where == "entrance"
    assert ports.proposed[-1][0:2] == (130, "raid") and ports.proposed[-1][3] is TurnKind.ASIDE
    await night.tick()
    assert len([c for c in ports.sent if isinstance(c, Summon) and c.guids == (130,)]) == 1


@_run_async
async def test_tier_gate_keeps_the_call_open(setup):
    ports, _, night = setup
    night.state = State.CALLED
    await night.on_chat(chat(ports, "rs 25"))
    await night.tick()
    assert night.state is State.CALLED and ports.sent == []
    assert "17" in ports.proposed[-1][2]


@pytest.mark.parametrize("decline", [True, False])
@_run_async
async def test_decline_and_lapse_emit_once(setup, decline):
    ports, _, night = setup
    await night.on_login(Login(0, 1, True))
    await advance(ports, 75)
    await night.tick()
    if decline:
        await night.on_chat(chat(ports, "not tonight"))
    else:
        await advance(ports, 600)
    await night.tick()
    assert night.state is State.ENDED and len(ports.proposed) == 2
    await night.tick()
    assert len(ports.proposed) == 2


@pytest.mark.parametrize("return_after", [None, 120])
@_run_async
async def test_logout_disband_timer_and_return_cancellation(setup, return_after):
    ports, _, night = setup
    await form(setup)
    before = len(controls(ports, "disband"))
    await night.on_logout(Logout(0, 1, True))
    await night.tick()
    if return_after:
        await advance(ports, return_after)
        await night.on_login(Login(0, 1, True))
        await advance(ports, 181)
    else:
        await advance(ports, 299)
        await night.tick()
        assert len(controls(ports, "disband")) == before
        await advance(ports, 2)
    await night.tick()
    assert len(controls(ports, "disband")) == before + (return_after is None)


@_run_async
async def test_hello_restart_does_not_call(setup):
    ports, _, night = setup
    await night.on_hello(Hello(0, "test", "1", 1, 1))
    await advance(ports, 120)
    await night.tick()
    assert night.state is State.IDLE and not ports.sent and not ports.proposed


@_run_async
async def test_logout_during_form_job_cannot_resume_stale_formation(setup):
    ports, _, night = setup
    reached, release = asyncio.Event(), asyncio.Event()
    original = ports.wait_job

    async def blocked(cmd_id, timeout_s):
        if isinstance(ports.sent[-1], FormRaid):
            reached.set()
            await release.wait()
        return await original(cmd_id, timeout_s)

    ports.wait_job = blocked
    night.state = State.CALLED
    await night.on_chat(chat(ports, "icc 25"))
    work = asyncio.create_task(night.tick())
    await reached.wait()
    await night.on_logout(Logout(0, 1, True))
    await night.on_login(Login(0, 1, True))
    release.set()
    await work
    assert night.state is State.WAITING_CALL and night.session is None
    assert not any(isinstance(c, SetLootMaster) for c in ports.sent)


@_run_async
async def test_cancelled_tick_cannot_be_retried_into_running(setup):
    ports, _, night = setup
    reached = asyncio.Event()

    async def blocked(cmd_id, timeout_s):
        reached.set()
        await asyncio.Event().wait()

    ports.wait_job = blocked
    night.state = State.CALLED
    await night.on_chat(chat(ports, "icc 25"))
    work = asyncio.create_task(night.tick())
    await reached.wait()
    work.cancel()
    with pytest.raises(asyncio.CancelledError):
        await work
    assert night.state is State.ENDED and night.session is None
    await night.tick()
    assert not any(isinstance(c, SetLootMaster) for c in ports.sent)


@_run_async
async def test_end_handler_queues_close_without_blocking_dispatcher(setup):
    ports, _, night = setup
    await form(setup)
    session = night.session
    entered, release = asyncio.Event(), asyncio.Event()
    original = session.close

    async def blocked(outcome):
        entered.set()
        await release.wait()
        return await original(outcome)

    session.close = blocked
    await night.on_logout(Logout(0, 1, True))
    assert not entered.is_set()
    work = asyncio.create_task(night.tick())
    await entered.wait()
    await night.on_login(Login(0, 1, True))
    release.set()
    await work
    assert night.state is State.WAITING_CALL


@_run_async
async def test_foreign_events_never_reach_session_or_listeners(setup):
    ports, _, night = setup
    heard = []

    class Listener:
        async def on_boss(self, ev): heard.append("boss")
        async def on_death(self, ev): heard.append("death")
        async def on_dc_status(self, ev): heard.append("dc")

    night.add_listener(Listener())
    await running(setup)
    for ev in (Boss(0, 631, 88, 1, "Foreign", "killed", (101,)),
               Boss(0, 631, 77, 1, "Foreign", "killed", (999,))):
        await night.on_boss(ev)
    await night.on_death(Death(0, 999, 631, 0))
    await night.on_death(Death(0, 101, 1, 0))
    await night.on_dc_status(DcStatus(0, 999, True, False, ""))
    await night.tick()
    assert heard == [] and night.session.kills == 0
    await night.on_death(Death(0, 101, 631, 0))
    await night.on_dc_status(DcStatus(0, 101, True, False, ""))
    await night.tick()
    assert heard == ["death", "dc"]


@_run_async
async def test_assembly_timeout_leaves_group_and_outside_timeout_closes(setup):
    ports, _, night = setup
    await form(setup)
    before = len(controls(ports, "disband"))
    await advance(ports, 1200)
    await night.tick()
    assert night.state is State.ENDED and len(controls(ports, "disband")) == before
    await night.on_chat(chat(ports, "can we do icc 10"))
    await night.tick()
    await inside(setup)
    await advance(ports, 30)
    await night.tick()
    await advance(ports, 20)
    await night.tick()
    await night.on_zone(Zone(0, 1, 1637, "Orgrimmar"))
    await advance(ports, 599)
    await night.tick()
    assert night.state is State.RUNNING
    await advance(ports, 1)
    await night.tick()
    assert night.state is State.ENDED


@_run_async
async def test_mvp_remains_ticked_after_session_closes(setup):
    ports, _, night = setup
    await running(setup)
    await night.on_boss(Boss(0, 631, 77, 1, "Testboss", "killed", (1, 101)))
    await night.tick()
    await night.on_chat(chat(ports, "calling it"))
    await night.tick()
    assert night.state is State.ENDED and night._votes
    vote = night._votes[0]
    voter = vote.voters[0]
    target = next(g for g in vote.members if g != voter)
    ports.conn.execute("INSERT INTO utterance(ts,guid,channel,to_guid,kind,text,why,memory_ids) VALUES (?,?,?,?,?,?,?,?)",
                        (ports.clock.now(), voter, "guild", 0, "aside", ports.members[target].name, "test", "[]"))
    await advance(ports, 90)
    await night.tick()
    assert night._votes == []
    assert any("mvp tonight" in text for _, _, text in ports.said)


@pytest.mark.parametrize("phase", ["send", "job"])
@_run_async
async def test_transport_exception_never_retries_uncertain_formation(setup, phase):
    ports, _, night = setup
    original_send = ports.send

    async def fail_send(command):
        ack = await original_send(command)
        if isinstance(command, FormRaid):
            raise TimeoutError("private transport contents")
        return ack

    async def fail_job(cmd_id, timeout_s):
        raise ConnectionError("private transport contents")

    if phase == "send":
        ports.send = fail_send
    else:
        ports.wait_job = fail_job
    await form(setup)
    assert night.state is State.ENDED and night.session is None
    await night.tick()
    assert len([c for c in ports.sent if isinstance(c, FormRaid)]) == 1
    assert all("private transport" not in text for _, _, text, _ in ports.proposed)


@pytest.mark.parametrize("cancel", [True, False])
@_run_async
async def test_interrupted_session_open_closes_exact_committed_attendance(setup, cancel):
    ports, _, night = setup
    entered = asyncio.Event()

    async def remember(candidate):
        entered.set()
        if cancel:
            await asyncio.Event().wait()
        raise RuntimeError("test memory writer failed")

    ports.remember = remember
    night.state = State.CALLED
    await night.on_chat(chat(ports, "icc 25"))
    work = asyncio.create_task(night.tick())
    await entered.wait()
    if cancel:
        work.cancel()
        with pytest.raises(asyncio.CancelledError):
            await work
    else:
        await work
    assert night.state is State.ENDED
    assert ports.conn.execute("SELECT COUNT(*) FROM guild_raid WHERE ended IS NULL").fetchone()[0] == 0
    assert ports.conn.execute("SELECT COUNT(*) FROM guild_raid WHERE offline=0").fetchone()[0] == 1
    await night.tick()
    assert len([c for c in ports.sent if isinstance(c, FormRaid)]) == 1


@_run_async
async def test_armory_read_does_not_block_dispatch_and_logout_discards_result(setup):
    ports, armory, night = setup
    loop = asyncio.get_running_loop()
    entered, release = asyncio.Event(), threading.Event()

    def blocked_tier(guid):
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(5), "test did not release synthetic armory read"
        return 16

    armory.human_tier = blocked_tier
    night.state = State.CALLED
    await night.on_chat(chat(ports, "icc 25"))
    assert not entered.is_set() and not ports.sent
    work = asyncio.create_task(night.tick())
    try:
        await asyncio.wait_for(entered.wait(), 3)
        await night.on_logout(Logout(0, 1, True))
        assert night.state is State.ENDED
    finally:
        release.set()
        await work
    assert not ports.sent and night.session is None


@_run_async
async def test_owner_exit_during_inside_job_requires_new_inside_validation(setup):
    ports, _, night = setup
    await form(setup)
    original = ports.wait_job
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocked(cmd_id, timeout_s):
        entered.set()
        await release.wait()
        return await original(cmd_id, timeout_s)

    ports.wait_job = blocked
    await night.on_zone(Zone(0, 1, 4812, "ICC"))
    work = asyncio.create_task(night.tick())
    await entered.wait()
    await night.on_zone(Zone(0, 1, 1637, "Orgrimmar"))
    await night.on_zone(Zone(0, 1, 4812, "ICC"))
    release.set()
    await work
    assert not controls(ports, "ready_check") and not night._inside
    ports.wait_job = original
    await night.tick()
    assert len([c for c in ports.sent if isinstance(c, Summon) and c.where == "inside"]) == 2
    assert night.state is State.ASSEMBLING


@pytest.mark.parametrize("ending", ["called", "logout", "outside"])
@_run_async
async def test_owned_dc_run_is_stopped_once_before_raid_end(setup, ending):
    ports, _, night = setup
    await running(setup)
    observed = []

    class Listener:
        async def on_end(self, outcome):
            observed.append((outcome, len(controls(ports, "dc_off"))))

    night.add_listener(Listener())
    if ending == "logout":
        await night.on_logout(Logout(0, 1, True))
    elif ending == "outside":
        await night.on_zone(Zone(0, 1, 1637, "Orgrimmar"))
        await advance(ports, 600)
    else:
        await night.on_chat(chat(ports, "calling it"))
    await night.tick()
    assert night.state is State.ENDED
    stop = controls(ports, "dc_off")
    assert len(stop) == 1 and stop[0].member_guid == stop[0].leader_guid == 101
    assert observed == [("logout" if ending == "logout" else "called", 1)]
    await night.tick()
    assert len(controls(ports, "dc_off")) == 1


@_run_async
async def test_logout_during_dc_start_ack_still_stops_attempted_old_run(setup):
    ports, _, night = setup
    await form(setup)
    await inside(setup)
    await advance(ports, 30)
    await night.tick()
    await advance(ports, 20)
    reached, release = asyncio.Event(), asyncio.Event()
    original = ports.send

    async def blocked(command):
        result = await original(command)
        if isinstance(command, RaidControl) and command.action == "dc_on":
            reached.set()
            await release.wait()
        return result

    ports.send = blocked
    work = asyncio.create_task(night.tick())
    await reached.wait()
    await night.on_logout(Logout(0, 1, True))
    await night.on_login(Login(0, 1, True))
    release.set()
    await work
    await night.tick()
    assert night.state is State.WAITING_CALL
    assert len(controls(ports, "dc_off")) == 1 and night.session is None


@pytest.mark.parametrize("human_leads", [True, False])
@_run_async
async def test_never_stops_manual_or_not_yet_started_dc(setup, human_leads):
    ports, _, night = setup
    await form(setup, "icc 10, i'll lead" if human_leads else "icc 10")
    if human_leads:
        await inside(setup)
    await night.on_chat(chat(ports, "calling it"))
    await night.tick()
    assert not controls(ports, "dc_off")


@pytest.mark.parametrize("failure", ["offline", "rejected", "wrong_id", "exception"])
@_run_async
async def test_dc_stop_failure_does_not_retry_or_block_close(setup, failure):
    ports, _, night = setup
    await running(setup)
    if failure == "offline":
        ports.members[101] = replace(ports.members[101], online=False)
    original = ports.send

    async def reject(command):
        result = await original(command)
        if isinstance(command, RaidControl) and command.action == "dc_off":
            if failure == "exception":
                raise ConnectionError("private transport contents")
            return replace(result, id="wrong" if failure == "wrong_id" else result.id,
                           ok=failure != "rejected")
        return result

    ports.send = reject
    await night.on_logout(Logout(0, 1, True))
    await night.tick()
    assert night.state is State.ENDED and not night._closing
    assert ports.conn.execute("SELECT COUNT(*) FROM guild_raid WHERE ended IS NULL").fetchone()[0] == 0
    await night.tick()
    assert len(controls(ports, "dc_off")) == (failure != "offline")


@_run_async
async def test_old_cleanup_never_stops_a_newer_persisted_raid(setup):
    ports, _, night = setup
    await running(setup)
    await night.on_logout(Logout(0, 1, True))
    new_raid(ports.conn, started=ports.clock.now(), raid_key="naxx", size=10,
             heroic=False, offline=False, lead_guid=101, tank_guid=101, loot_guid=104)
    await night.tick()
    assert not controls(ports, "dc_off")


@pytest.mark.parametrize("text,forms", [("icc 10", False), ("can we do icc 10", True)])
@_run_async
async def test_expired_call_requires_explicit_intent_even_before_next_tick(setup, text, forms):
    ports, _, night = setup
    await night.on_login(Login(0, 1, True))
    await advance(ports, 75)
    await night.tick()
    assert night.state is State.CALLED
    await advance(ports, 601)
    await night.on_chat(chat(ports, text))
    assert night.state is (State.FORMING if forms else State.ENDED)
    assert sum("has lapsed" in text for _, _, text, _ in ports.proposed) == 1
    await night.tick()
    assert night.state is (State.ASSEMBLING if forms else State.ENDED)
    assert sum("has lapsed" in text for _, _, text, _ in ports.proposed) == 1


@_run_async
async def test_hangout_receipt_survives_relogin_and_controller_restart_but_expires(setup):
    ports, armory, night = setup
    await night.on_chat(chat(ports, "no raid, let's just chill"))
    assert night.state is State.ENDED
    await night.on_logout(Logout(0, 1, True))
    await advance(ports, 120)
    await night.on_login(Login(0, 1, True))
    await advance(ports, 600)
    await night.tick()
    assert night.state is State.ENDED
    assert not any("Ask Testy about a raid" in text for _, _, text, _ in ports.proposed)
    restarted = RaidNight(ports, GuildConfig(), armory, random.Random(3))
    await restarted.on_login(Login(0, 1, True))
    await advance(ports, 75)
    await restarted.tick()
    assert restarted.state is State.ENDED
    # The receipt suppresses only automatic calls; the owner's next choice works.
    await restarted.on_chat(chat(ports, "can we do icc 10"))
    await restarted.tick()
    assert restarted.state is State.ASSEMBLING
    await restarted.on_logout(Logout(0, 1, True))
    await restarted.tick()
    await advance(ports, 86400)
    await restarted.on_login(Login(0, 1, True))
    await advance(ports, 75)
    await restarted.tick()
    assert restarted.state is State.CALLED


@_run_async
async def test_engagement_reaches_scoped_listeners_without_kill_or_dkp(setup):
    ports, _, night = setup
    heard = []

    class Listener:
        async def on_boss(self, ev):
            heard.append(ev.state)

    night.add_listener(Listener())
    await running(setup)
    await night.on_boss(Boss(0, 631, 77, 1, "Testboss", "engaged", (1, 101)))
    await night.on_boss(Boss(0, 631, 88, 1, "Other instance", "engaged", (1, 101)))
    await night.tick()
    assert heard == ["engaged"] and night.session.kills == 0
    assert ports.conn.execute("SELECT COUNT(*) FROM dkp_ledger WHERE reason='kill'").fetchone()[0] == 0
