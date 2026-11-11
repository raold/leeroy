"""A start refused for one named member is replanned without that member, never resent that day."""

from collections import deque
from datetime import datetime, timezone
import random
from types import SimpleNamespace

import pytest

from leeroy_mind.guild.offline import (BUSY_RETRY_S, INSTANCE_MAPS, MAX_ATTEMPTS, RETRY_S, OfflineRaids,
                                       refused_member)
from leeroy_mind.guild.ports import LivePorts
from leeroy_mind.guild.protocol import StartOfflineRaid
from leeroy_mind.guild.session import RaidPlan
from .fakes import member
from .test_offline import event, setup  # noqa: F401  (fixture)
from .test_raid_night import _run_async, advance, setup as raid_setup  # noqa: F401  (fixture)


def roster(command):
    return {command.leader_guid, *command.members}


def a_member(command):
    """A sent member who is neither the leader nor the loot officer."""
    return next(g for g in command.members if g not in {command.leader_guid, command.loot_master_guid})


def refuse(ports, *errors):
    """Refuse the next starts in turn; an error may be a function of the command it refuses."""
    pending, send = deque(errors), ports.send

    async def refusing(cmd):
        if isinstance(cmd, StartOfflineRaid) and pending:
            error = pending.popleft()
            ports.acks["start_offline_raid"].appendleft((False, error(cmd) if callable(error) else error))
        return await send(cmd)

    ports.send = refusing


def in_instance(cmd):
    return f"in_instance:{a_member(cmd)}"


PLAN = RaidPlan("icc", 25, False, True, 101, 101, 104, [101, 104, 113, 120], [], False)


@pytest.mark.parametrize("error, guid", [
    ("in_instance:113", 113), ("busy:realm_owner:120", 120), ("busy:realm_group:113", 113),
    ("busy:104", 104), ("not_cast:113", 113), ("leader_unavailable:in_combat", 101),
    ("leader_unavailable:dead", 101), ("setup:grouping:in_instance:113", 113),
    ("setup:teleporting:busy:realm_owner:120", 120),
    ("in_instance:999", None), ("busy", None), ("busy:realm_owner", None), ("leader_not_tank", None),
    ("driver_pending", None), ("in_instance:", None), ("in_instance:-1", None), ("in_instance:1x", None),
    ("in_instance:99999999999", None), ("setup:grouping:too_few", None), ("training_commands_pending", None),
])
def test_refused_member_reads_only_a_roster_member(error, guid):
    assert refused_member(error, PLAN) == guid


@_run_async
async def test_a_refused_member_is_replaced_from_the_bench(setup):
    ports, _, controller = setup
    refuse(ports, in_instance)
    await controller.tick()
    refused = ports.sent[-1]
    gone = a_member(refused)
    day = controller._day_state()
    assert day["excluded"] == [gone] and not day["blocked"] and day["refused"] == f"in_instance:{gone}"
    await advance(ports, BUSY_RETRY_S - 60)
    await controller.tick()
    assert len(ports.sent) == 1  # The other bodies keep the busy pace.
    await advance(ports, 60)
    await controller.tick()
    second = ports.sent[-1]
    assert len(ports.sent) == 2 and gone not in roster(second)
    assert len(roster(second)) == len(roster(refused)) == 25  # A bench member took the seat.
    assert (second.leader_guid, second.loot_master_guid) == (refused.leader_guid, refused.loot_master_guid)
    assert ports.gpu == ["acquire:offline_raid", "release:offline_raid", "acquire:offline_raid"]


@pytest.mark.parametrize("error", ["in_instance:101", "leader_unavailable:in_combat"])
@_run_async
async def test_a_refused_leader_is_replaced_by_another_tank(setup, error):
    ports, _, controller = setup
    ports.acks["start_offline_raid"].append((False, error))
    await controller.tick()
    assert ports.sent[-1].leader_guid == 101
    await advance(ports, BUSY_RETRY_S)
    await controller.tick()
    second = ports.sent[-1]
    assert len(ports.sent) == 2 and 101 not in roster(second)
    assert second.leader_guid in {103, 105, 106, 107}


@_run_async
async def test_a_refused_member_is_never_resent_that_day_and_may_return_the_next(setup):
    ports, _, controller = setup
    refuse(ports, "in_instance:101", in_instance, in_instance, in_instance)
    await controller.tick()
    for _ in range(3):
        await advance(ports, BUSY_RETRY_S)
        await controller.tick()
    assert len(ports.sent) == 4
    named = [101, *(a_member(command) for command in ports.sent[1:])]
    assert controller._day_state()["excluded"] == sorted(named)
    for index, guid in enumerate(named):
        assert guid in roster(ports.sent[index])
        assert not any(guid in roster(later) for later in ports.sent[index + 1:])
    # The next day starts with nobody left out: the standing raid lead leads again.
    tomorrow = datetime(2026, 10, 30, 14, tzinfo=timezone.utc)
    await advance(ports, tomorrow.timestamp() - ports.clock.now())
    await controller.tick()
    assert len(ports.sent) == 5 and "excluded" not in controller._day_state()
    assert ports.sent[-1].leader_guid == 101


@_run_async
async def test_member_refusals_are_still_bounded_by_the_attempts(setup):
    ports, _, controller = setup
    refuse(ports, *[in_instance] * (MAX_ATTEMPTS + 2))
    for _ in range(MAX_ATTEMPTS + 2):
        await controller.tick()
        await advance(ports, BUSY_RETRY_S)
    assert len(ports.sent) == MAX_ATTEMPTS and controller._day_state()["blocked"]
    assert len(controller._day_state()["excluded"]) == MAX_ATTEMPTS


@_run_async
async def test_a_roster_below_the_minimum_waits_instead_of_sending(setup):
    ports, _, controller = setup
    ports.away_set = set(range(121, 131))  # Exactly the 25-man minimum of 20 is left.
    refuse(ports, in_instance)
    await controller.tick()
    first = ports.sent[-1]
    assert len(roster(first)) == 20
    gone = a_member(first)
    for _ in range(3):
        await advance(ports, BUSY_RETRY_S)
        await controller.tick()
    assert len(ports.sent) == 1  # Nineteen are never sent.
    day = controller._day_state()
    assert not day["blocked"] and day["attempts"] == 1 and day["excluded"] == [gone]
    assert ports.gpu == ["acquire:offline_raid", "release:offline_raid"]
    # Someone back from away makes twenty again, without the refused member.
    ports.away_set = set(range(122, 131))
    await advance(ports, BUSY_RETRY_S)
    await controller.tick()
    assert len(ports.sent) == 2 and gone not in roster(ports.sent[-1]) and 121 in roster(ports.sent[-1])


@_run_async
async def test_a_roster_that_fails_without_the_left_out_members_still_ends_the_day(setup):
    ports, _, controller = setup
    ports.dungeon_set = {125}
    ports.away_set = set(range(106, 131)) - {125}
    await controller.tick()
    assert not ports.sent and controller._day_state()["blocked"]


@_run_async
async def test_exclusions_survive_a_restart(setup):
    ports, armory, controller = setup
    refuse(ports, lambda cmd: f"busy:{a_member(cmd)}")
    await controller.tick()
    gone = a_member(ports.sent[-1])
    recreated = OfflineRaids(ports, controller.cfg, armory, random.Random(11))
    await advance(ports, BUSY_RETRY_S)
    await recreated.tick()
    assert len(ports.sent) == 2 and gone not in roster(ports.sent[-1])


@_run_async
async def test_a_not_cast_member_is_left_out_and_retried_within_a_minute(setup):
    ports, _, controller = setup
    refuse(ports, lambda cmd: f"not_cast:{a_member(cmd)}")
    await controller.tick()
    gone = a_member(ports.sent[-1])
    assert not controller._day_state()["blocked"]
    await advance(ports, RETRY_S)
    await controller.tick()
    assert len(ports.sent) == 2 and gone not in roster(ports.sent[-1])


@_run_async
async def test_a_refusal_naming_someone_outside_the_roster_keeps_the_old_retry(setup):
    ports, _, controller = setup
    ports.acks["start_offline_raid"].append((False, "in_instance:999"))
    await controller.tick()
    day = controller._day_state()
    assert "excluded" not in day and not day["blocked"]
    await advance(ports, BUSY_RETRY_S)
    await controller.tick()
    assert len(ports.sent) == 2


@pytest.mark.parametrize("stage", ["grouping", "teleporting"])
@_run_async
async def test_a_setup_failure_naming_a_member_leaves_it_out_of_the_next_try(setup, stage):
    ports, _, controller = setup
    await controller.tick()
    gone = a_member(ports.sent[-1])
    if stage == "teleporting":
        await controller.on_job(event(controller, stage="teleporting"))
        await controller.tick()
    await controller.on_job(event(controller, state="failed", stage="", reason=f"setup:{stage}:in_instance:{gone}",
                                  instance=77 if stage == "teleporting" else 0))
    await controller.tick()
    assert controller._day_state()["excluded"] == [gone]
    await advance(ports, BUSY_RETRY_S)
    await controller.tick()
    assert len(ports.sent) == 2 and gone not in roster(ports.sent[-1])


@_run_async
async def test_members_known_in_a_dungeon_are_left_out_when_planning(setup):
    ports, _, controller = setup
    ports.dungeon_set = {101, 110, 125}
    await controller.tick()
    command = ports.sent[-1]
    assert not {101, 110, 125} & roster(command) and len(roster(command)) == 25
    assert command.leader_guid != 101
    assert "excluded" not in controller._day_state()  # Not refused: free again once out.


@_run_async
async def test_an_unreadable_dungeon_view_plans_as_before(setup):
    ports, _, controller = setup

    def broken():
        raise RuntimeError("world not ready")

    ports.in_dungeon = broken
    await controller.tick()
    assert len(ports.sent) == 1 and ports.sent[-1].leader_guid == 101


def test_live_ports_trust_the_snapshot_map_only_while_the_zone_is_unchanged():
    snapshot = {201: member(201, "A", 1, 2, map=574, zone=206),     # still in Utgarde Keep
                202: member(202, "B", 1, 2, map=574, zone=206),     # left it since
                203: member(203, "C", 1, 2, map=571, zone=65),      # outdoors in Northrend
                204: member(204, "D", 1, 2, map=631, zone=4812),    # inside a raid, not live yet
                205: member(205, "E", 1, 2, map=658, zone=4813)}    # the Pit of Saron
    live = {201: snapshot[201], 202: member(202, "B", 1, 2, map=574, zone=65),
            203: snapshot[203], 205: snapshot[205]}
    app = SimpleNamespace(clock=None, store=SimpleNamespace(conn=None, cast=lambda: snapshot),
                          world=SimpleNamespace(members=live))
    assert LivePorts(app).in_dungeon() == {201, 205}
    assert {574, 575, 578, 631, 649, 658, 668} <= INSTANCE_MAPS and not {0, 1, 571, 530} & INSTANCE_MAPS
