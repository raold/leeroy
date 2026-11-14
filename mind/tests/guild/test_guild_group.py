"""His own `.playerbots bot addN` group: a raid it takes into a known raid instance counts like a
called raid night, and a group he built himself is never disbanded by the mind."""

import random

import pytest

from leeroy_mind.guild.config import GuildConfig
from leeroy_mind.guild.loot import _get, _put
from leeroy_mind.guild.protocol import (ClearLockout, FormRaid, GuildGroup, GuildGroupAdd, JobSkip, RaidControl,
                                        SetLootMaster, Summon)
from leeroy_mind.guild.raid_night import FORMED_GROUP_KEY, RaidNight, State
from leeroy_mind.protocol import Boss, Group, Login, Logout, Zone

from .fakes import job, member
from .test_raid_night import _run_async, advance, controls, form, running, setup  # noqa: F401


GROUP = tuple(range(101, 125))  # 24 guild members: tanks, healers and damage, officers among them
# mod-leeroy's HandleSummon: a summon with a member still on the way completes 60 s after the server handled the
# command, on the first world tick past that deadline (RaidAssembly.cpp, TickSummons).
NATIVE_SUMMON_DEADLINE_S = 60.0


def group_event(ports, *, reason="entered", map=631, instance_id=77, difficulty=1, members=GROUP, size=25,
                roles=None):
    roles = roles or {}
    return GuildGroup(ts=int(ports.clock.now() * 1000), reason=reason, size=size, leader_guid=1, raid=True,
                      map=map, instance_id=instance_id, difficulty=difficulty,
                      added=tuple(GuildGroupAdd(g, roles.get(g, "damage")) for g in members) if reason == "built" else (),
                      members=tuple(members))


async def logged_in(setup):
    ports, _, night = setup
    await night.on_login(Login(0, 1, True))
    await night.tick()
    return night


async def adopted(setup, **fields):
    ports, _, night = setup
    await logged_in(setup)
    await night.on_zone(Zone(0, 1, 4812, "Icecrown Citadel"))
    await night.on_guild_group(group_event(ports, **fields))
    await night.tick()
    await night.tick()
    return night


def records(ports, raid_key="icc"):
    return ports.conn.execute("SELECT raid_id,size,heroic,offline,lead_guid,ended,outcome FROM guild_raid "
                              "WHERE raid_key=? ORDER BY raid_id", (raid_key,)).fetchall()


def attendance(ports, raid_id):
    return {g: s for g, s in ports.conn.execute("SELECT guid,status FROM guild_attendance WHERE raid_id=?",
                                                (raid_id,))}


def summoned(*, instance_id=77, missing=()):
    """The server's inside summon: everyone arrives in that instance but the missing."""
    return lambda command: job("summon", "done", guids=tuple(g for g in command.guids if g not in missing),
                               skipped=tuple(JobSkip(g, "timeout") for g in command.guids if g in missing),
                               instance_id=instance_id)


def ledger(ports, raid_id, reason):
    return sorted(g for (g,) in ports.conn.execute("SELECT guid FROM dkp_ledger WHERE raid_id=? AND reason=?",
                                                   (raid_id, reason)))


@_run_async
async def test_his_group_in_a_raid_opens_one_record_like_a_called_raid(setup):
    ports, _, night = setup
    await adopted(setup)
    assert night.state is State.RUNNING and night.run.lead_is_human and night.run.human_in_raid
    # Arrival is proved inside his copy first, then the loot master; nothing is formed, cleared or disbanded.
    assert [type(c) for c in ports.sent] == [Summon, SetLootMaster]
    summon, loot = ports.sent
    assert (summon.where, summon.map_id, set(summon.guids), summon.summon_human) == ("inside", 631, set(GROUP), False)
    assert (loot.member_guid, loot.looter_guid) == (1, 104)
    [(raid_id, size, heroic, offline, lead, ended, _)] = records(ports)
    assert (size, heroic, offline, lead, ended) == (25, 0, 0, 1, None)
    assert attendance(ports, raid_id) == {g: "raided" for g in (*GROUP, 1)}
    assert ledger(ports, raid_id, "attendance") == sorted((*GROUP, 1))
    assert night.session.ctx.instance_id == 77 and night.run.plan.bench == []
    assert ports.leads[-1] == 1

    # Entering again, or another fill of the same group, opens nothing new.
    await night.on_guild_group(group_event(ports))
    await night.on_guild_group(group_event(ports, reason="built"))
    await night.tick()
    assert len(records(ports)) == 1 and [type(c) for c in ports.sent] == [Summon, SetLootMaster]

    # Kills pay DKP as a called raid's do.
    await night.on_boss(Boss(int(ports.clock.now() * 1000), 631, 77, 0, "Lord Marrowgar", "killed", (1, *GROUP)))
    await night.tick()
    assert night.session.kills == 1
    assert ledger(ports, raid_id, "kill") == sorted((*GROUP, 1))


@_run_async
async def test_only_members_who_arrive_inside_count(setup):
    ports, _, night = setup
    # 104 (the DKP officer) and 120 are still outside when the summon's time runs out.
    ports.jobs["summon"].append(summoned(missing=(104, 120)))
    await adopted(setup)
    arrived = set(GROUP) - {104, 120}
    [(raid_id, *_)] = records(ports)
    assert attendance(ports, raid_id) == {g: "raided" for g in (*arrived, 1)}
    assert ledger(ports, raid_id, "attendance") == sorted((*arrived, 1))
    assert set(night.run.plan.members) == arrived
    assert night.run.plan.loot_guid in arrived and night.run.plan.tank_guid in arrived
    assert ports.sent[1].looter_guid == night.run.plan.loot_guid


def native_timing(ports, *, lag_s):
    """The fixture's summon results, delivered as the server times one that someone never finishes. The mind's
    wait starts at the ack, which takes the bridge's control lane and is resolved in the mind's reader; the
    completion leaves after native's deadline in the event lane, behind queued events, and reaches the waiter
    through the mind's serial dispatcher, lag_s later than the ack's own trip took. A wait that has ended by then
    gets nothing: its mailbox entry is gone, so the late completion is dropped."""
    scripted = ports.wait_job

    async def wait_job(cmd_id, timeout_s):
        result = await scripted(cmd_id, timeout_s)
        late = result is not None and any(skip.reason == "timeout" for skip in result.skipped)
        return None if late and timeout_s < NATIVE_SUMMON_DEADLINE_S + lag_s else result
    ports.wait_job = wait_job


@pytest.mark.parametrize("lag_s", [0.05, 2.0])  # one world tick; a busy dispatcher
@_run_async
async def test_a_member_who_never_arrives_does_not_cost_the_others_their_record(setup, lag_s):
    ports, _, night = setup
    native_timing(ports, lag_s=lag_s)
    ports.jobs["summon"].append(summoned(missing=(120,)))
    await adopted(setup)
    arrived = set(GROUP) - {120}
    [(raid_id, *_)] = records(ports)
    assert attendance(ports, raid_id) == {g: "raided" for g in (*arrived, 1)}
    assert set(night.run.plan.members) == arrived


@pytest.mark.parametrize("result", [
    summoned(instance_id=78),                                     # another copy than his
    summoned(missing=GROUP),                                      # nobody got in
    lambda command: job("summon", "failed", guids=(), skipped=tuple(JobSkip(g, "offline") for g in command.guids),
                        instance_id=77),
    lambda command: None,                                         # no completion
])
@_run_async
async def test_no_record_without_proved_arrival_in_his_instance(setup, result):
    ports, _, night = setup
    ports.jobs["summon"].append(result)
    await adopted(setup)
    assert records(ports) == [] and night.run is None
    assert night.state is State.WAITING_CALL and not night.active
    assert [type(c) for c in ports.sent] == [Summon]


@pytest.mark.parametrize(("difficulty", "size", "heroic"), [(0, 10, 0), (1, 25, 0), (2, 10, 1), (3, 25, 1)])
@_run_async
async def test_size_and_heroic_come_from_the_instance_difficulty(setup, difficulty, size, heroic):
    ports, _, night = setup
    await adopted(setup, difficulty=difficulty, members=GROUP[:size - 1])
    [(_, recorded_size, recorded_heroic, *_)] = records(ports)
    assert (recorded_size, recorded_heroic) == (size, heroic)


@pytest.mark.parametrize("fields", [
    {"map": 574, "difficulty": 0},          # Utgarde Keep: a 5-man, no DKP there
    {"map": 571, "instance_id": 0},         # Northrend itself
    {"map": 603, "difficulty": 2},          # Ulduar has no heroic difficulty
    {"difficulty": 7},                      # not a raid difficulty
    {"instance_id": 0},
    {"members": GROUP, "difficulty": 0},    # 24 members do not fit a 10-man
    {"members": (102, 104, 108, 109, 110)},  # no tank among them
])
@_run_async
async def test_no_record_outside_a_known_raid_or_for_a_group_that_cannot_raid_it(setup, fields):
    ports, _, night = setup
    await logged_in(setup)
    await night.on_guild_group(group_event(ports, **fields))
    await night.tick()
    await night.tick()
    assert records(ports) == [] and records(ports, "ulduar") == []
    assert night.state is State.WAITING_CALL and night.run is None and not night.active
    assert ports.sent == []
    # The raid call still goes out later as on any night.
    await advance(ports, 120)
    await night.tick()
    assert night.state is State.CALLED


FERAL_GROUP = (102, 111, 112, 104, 108, 109, 110, 130)  # healers and damage, no protection warrior; 130 is feral


async def feral_group(setup, *fills):
    """His add10 group whose tank slot the server filled with 130, a feral whose persona raid role is melee
    (native counts a feral by bear form or Thick Hide, the mind by the persona): each fill's (members, roles)
    first, then he takes the group into ICC 10."""
    ports, _, night = setup
    ports.members[130] = member(130, "M130", 11, 1)
    await logged_in(setup)
    for members, roles in fills:
        await night.on_guild_group(group_event(ports, reason="built", map=571, instance_id=0, difficulty=0,
                                               members=members, size=10, roles=roles))
    await night.on_zone(Zone(0, 1, 4812, "Icecrown Citadel"))
    await night.on_guild_group(group_event(ports, difficulty=0, members=FERAL_GROUP, size=10))
    await night.tick()
    await night.tick()
    return night


@_run_async
async def test_a_tank_his_add_command_picked_is_his_raids_tank(setup):
    ports, _, night = setup
    await feral_group(setup, (FERAL_GROUP, {130: "tank", 102: "healer", 111: "healer", 112: "healer"}))
    [(raid_id, size, *_)] = records(ports)
    assert size == 10 and night.state is State.RUNNING
    assert night.run.plan.tank_guid == 130
    assert attendance(ports, raid_id) == {g: "raided" for g in (*FERAL_GROUP, 1)}


@pytest.mark.parametrize("fills", [
    [(FERAL_GROUP, {130: "damage"})],                                  # the server picked 130 as damage
    [(FERAL_GROUP, {130: "tank"}), ((102, 111, 112, 104, 108), {})],   # 130 left the group the add command built
])
@_run_async
async def test_only_a_tank_pick_in_his_group_counts_as_a_tank(setup, fills):
    ports, _, night = setup
    await feral_group(setup, *fills)
    assert records(ports) == [] and night.run is None
    assert not any(isinstance(c, Summon) for c in ports.sent)


@_run_async
async def test_a_raid_night_the_mind_forms_keeps_its_own_tank_rule(setup):
    ports, _, night = setup
    ports.members[130] = member(130, "M130", 11, 1)
    for guid in (101, 103, 105, 106, 107):  # every protection warrior is offline
        ports.members[guid] = member(guid, ports.members[guid].name, 1, 2, online=False)
    await logged_in(setup)
    await night.on_guild_group(group_event(ports, reason="built", map=571, instance_id=0, difficulty=0,
                                           members=(130,), size=10, roles={130: "tank"}))
    night.state, night._call_deadline = State.CALLED, ports.clock.now() + 600
    await form(setup)
    assert records(ports) == [] and ports.sent == []
    assert night.state is State.ENDED


@_run_async
async def test_a_called_raid_is_never_doubled(setup):
    ports, _, night = setup
    await running(setup)
    sent = len(ports.sent)
    [(raid_id, *_)] = records(ports)
    await night.on_guild_group(group_event(ports))
    await night.on_guild_group(group_event(ports, instance_id=92))
    await night.tick()
    assert [row[0] for row in records(ports)] == [raid_id] and len(ports.sent) == sent
    assert night.session.raid_id == raid_id


@_run_async
async def test_it_ends_when_his_group_breaks_up_and_his_group_is_never_disbanded(setup):
    ports, _, night = setup
    await adopted(setup)
    [(raid_id, *_)] = records(ports)
    # Members leaving one by one do not end it; the group breaking up does.
    await night.on_group(Group(0, 1, True, (1, *GROUP[:3])))
    assert night.state is State.RUNNING
    await night.on_group(Group(0, 0, False, ()))
    assert night.state is State.ENDED
    await night.tick()
    [(_, _, _, _, _, ended, outcome)] = records(ports)
    assert ended is not None and outcome == "disbanded"
    # Not after the raid, at logout, five minutes on or at his next login.
    await night.on_logout(Logout(0, 1, True))
    await advance(ports, 400)
    await night.tick()
    await night.on_login(Login(0, 1, True))
    await night.tick()
    assert controls(ports, "disband") == []
    # One record per instance: the same copy again is not a new raid; a fresh copy is.
    await night.on_guild_group(group_event(ports))
    await night.tick()
    assert len(records(ports)) == 1 and night.state is State.WAITING_CALL
    ports.jobs["summon"].append(summoned(instance_id=93))
    await night.on_guild_group(group_event(ports, instance_id=93))
    await night.tick()
    assert len(records(ports)) == 2 and night.state is State.ASSEMBLING


@_run_async
async def test_leaving_the_instance_ends_it_as_a_called_raid_ends(setup):
    ports, _, night = setup
    await adopted(setup)
    await night.on_zone(Zone(0, 1, 4395, "Dalaran"))
    await advance(ports, 599)
    await night.tick()
    assert night.state is State.RUNNING
    await advance(ports, 2)
    await night.tick()
    assert night.state is State.ENDED
    [(_, _, _, _, _, ended, outcome)] = records(ports)
    assert ended is not None and outcome == "called"
    assert controls(ports, "disband") == []


@_run_async
async def test_only_a_group_the_mind_formed_is_disbanded_at_login(setup):
    ports, armory, night = setup
    await form(setup)
    assert _get(ports.conn, FORMED_GROUP_KEY) is True
    before = len(controls(ports, "disband"))
    # A restarted mind still knows its raid group stood and clears it at his login.
    restarted = RaidNight(ports, GuildConfig(), armory, random.Random(7))
    await restarted.on_login(Login(0, 1, True))
    await restarted.tick()
    assert len(controls(ports, "disband")) == before + 1 and _get(ports.conn, FORMED_GROUP_KEY) is None
    again = RaidNight(ports, GuildConfig(), armory, random.Random(7))
    await again.on_login(Login(0, 1, True))
    await again.tick()
    assert len(controls(ports, "disband")) == before + 1


@_run_async
async def test_an_offline_no_group_keeps_the_formed_group_for_his_next_login(setup):
    ports, _, night = setup
    await form(setup)
    await night.on_logout(Logout(0, 1, True))
    ports.online = False
    ports.acks["raid_control"].append((False, "no_group"))
    await advance(ports, 301)
    await night.tick()
    assert _get(ports.conn, FORMED_GROUP_KEY) is True
    ports.online = True
    await night.on_login(Login(0, 1, True))
    await night.tick()
    assert controls(ports, "disband")[-1].action == "disband" and _get(ports.conn, FORMED_GROUP_KEY) is None


@_run_async
async def test_building_his_own_group_takes_it_over_from_the_mind(setup):
    ports, _, night = setup
    await logged_in(setup)
    with ports.conn:
        _put(ports.conn, FORMED_GROUP_KEY, True)
    await night.on_guild_group(group_event(ports, reason="built", map=571, instance_id=0, difficulty=0))
    assert _get(ports.conn, FORMED_GROUP_KEY) is None
    await night.on_logout(Logout(0, 1, True))
    await advance(ports, 400)
    await night.tick()
    await night.on_login(Login(0, 1, True))
    await night.tick()
    assert controls(ports, "disband") == []
    assert not any(isinstance(c, (FormRaid, ClearLockout, Summon, RaidControl)) for c in ports.sent)
    assert night.state is State.WAITING_CALL


@_run_async
async def test_someone_elses_or_an_offline_owners_group_is_ignored(setup):
    ports, _, night = setup
    await logged_in(setup)
    other = group_event(ports)
    await night.on_guild_group(GuildGroup(other.ts, other.reason, other.size, 2, other.raid, other.map,
                                          other.instance_id, other.difficulty, other.added, other.members))
    await night.on_logout(Logout(0, 1, True))
    await night.on_guild_group(group_event(ports))
    await night.tick()
    assert records(ports) == [] and ports.sent == []


@_run_async
async def test_a_member_he_named_calls_his_groups_raid_while_he_drives_it(setup):
    """raidlead: the member he named to lead his group is the raid's lead (its record, the talk's
    lead), while he still drives it: no ready check, no countdown, no dungeon-clear."""
    ports, _, night = setup
    ports.designated_lead = lambda: 103
    await adopted(setup)
    run = night.run
    assert night.state is State.RUNNING and run.lead_is_human and run.plan.lead_guid == 103
    [(raid_id, size, heroic, offline, lead, ended, _)] = records(ports)
    assert lead == 103 and ended is None
    assert ports.leads[-1] == 103
    assert [type(c) for c in ports.sent] == [Summon, SetLootMaster]
    for _ in range(3):
        await advance(ports, 60)
        await night.tick()
    assert not any(isinstance(c, RaidControl) for c in ports.sent)
    assert night.console_refusal("start") == "you lead tonight: dungeon-clear runs only under a bot lead"
    # her raid because he named her is no raid night the mind led: the rotation never sees it (P3-9)
    from leeroy_mind.guild.store import NAMED_LEAD_KEY, last_led
    assert _get(ports.conn, NAMED_LEAD_KEY.format(raid_id=raid_id)) == 103
    assert 103 not in last_led(ports.conn)


@_run_async
async def test_a_named_lead_outside_the_raid_leaves_him_its_lead(setup):
    ports, _, night = setup
    ports.designated_lead = lambda: 999
    await adopted(setup)
    assert night.run.lead_is_human and night.run.plan.lead_guid == 1
    [(raid_id, *_)] = records(ports)
    from leeroy_mind.guild.store import NAMED_LEAD_KEY
    assert _get(ports.conn, NAMED_LEAD_KEY.format(raid_id=raid_id)) is None
