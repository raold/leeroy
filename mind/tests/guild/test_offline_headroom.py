"""Admission reserves stay bounded, eligible and accountable; no live bridge/model calls."""
from dataclasses import replace
import json
import random

import pytest

from leeroy_mind.guild.offline import OfflineRaids
from leeroy_mind.guild.protocol import JobSkip
from leeroy_mind.protocol import encode_command
from .test_offline import event, setup, start
from .test_raid_night import _run_async, setup as raid_setup


def enable(controller, *, size=10, count=2, supported=True):
    controller.cfg = replace(controller.cfg, offline_reserves=count, offline_rotation=(f"icc{size}",))
    controller._features = frozenset({"offline_raid_reserves"} if supported else ())


@pytest.mark.parametrize(("count", "supported", "expected"), [(0, True, 0), (2, False, 0), (1, True, 1), (2, True, 2)])
@_run_async
async def test_reserve_wire_is_opt_in_and_capability_negotiated(setup, count, supported, expected):
    ports, _, controller = setup
    enable(controller, count=count, supported=supported)
    await start(setup)
    command = ports.sent[-1]
    wire = json.loads(encode_command(command, 17))
    assert len(command.reserves) == expected
    assert ("reserves" in wire) == bool(expected)
    assert set(command.reserves).isdisjoint({command.leader_guid, *command.members})
    assert command.min_members == 8 and len(command.members) == 9
    assert command.reserves == tuple(controller._owned["plan"]["bench"][:expected])


@_run_async
async def test_reserve_candidates_keep_owner_saves_absence_and_busy_exclusions(setup):
    ports, armory, controller = setup
    enable(controller)
    armory.binds = {1: {(631, 14)}, 103: {(631, 14)}}
    ports.away_set = {105}
    ports.dungeon_set = {106}
    controller._update_day(excluded=[107])
    await start(setup)
    command = ports.sent[-1]
    assert len(command.reserves) == 2
    assert not {1, 103, 105, 106, 107} & {command.leader_guid, *command.members, *command.reserves}


@pytest.mark.parametrize(("size", "losses"), [(10, 2), (25, 5)])
@_run_async
async def test_accepted_reserves_replace_admission_losses_and_are_not_also_benched(setup, size, losses):
    ports, _, controller = setup
    enable(controller, size=size)
    await start(setup)
    plan, reserves = controller._owned["plan"], ports.sent[-1].reserves
    dropped = [g for g in plan["members"] if g not in {plan["tank_guid"], plan["loot_guid"]}][:losses]
    accepted = tuple(g for g in plan["members"] if g not in dropped) + reserves
    await controller.on_job(event(controller, guids=accepted, skipped=tuple(JobSkip(g, "in_flight") for g in dropped)))
    await controller.tick()
    assert controller.session is not None
    actual = controller._owned["actual_plan"]
    assert set(actual["members"]) == set(accepted)
    assert len(actual["members"]) == size * 4 // 5 + 2
    assert not set(reserves) & set(actual["bench"])
    assert not set(dropped) & set(actual["members"])
    assert {plan["tank_guid"], plan["loot_guid"]} <= set(actual["members"])
    # Durable attendance includes replacements as present, never standby too.
    rows = ports.conn.execute("SELECT guid,status FROM guild_attendance WHERE raid_id=?",
                              (controller._owned["raid_id"],)).fetchall()
    assert {g for g, status in rows if status == "raided"} == set(accepted)


@_run_async
async def test_unused_reserves_remain_benched_and_manifest_survives_restart(setup):
    ports, armory, controller = setup
    enable(controller)
    await start(setup)
    reserves = ports.sent[-1].reserves
    sample = event(controller, skipped=tuple(JobSkip(g, "reserve_unused") for g in reserves))
    await controller.on_job(sample)
    await controller.tick()
    assert controller.session is not None
    recreated = OfflineRaids(ports, controller.cfg, armory, random.Random(8))
    assert recreated._owned["reserves"] == list(reserves)
    assert recreated._valid_job(sample)
    await recreated.on_job(sample)
    await recreated.tick()
    assert recreated.session is not None
    assert set(reserves) <= set(recreated._owned["actual_plan"]["bench"])
    assert not set(reserves) & set(recreated._owned["accepted"])


@pytest.mark.parametrize("bad", ["unknown", "over_capacity", "missing_reserve", "missing_lead", "duplicate"])
@_run_async
async def test_reserve_manifest_does_not_weaken_native_receipt_validation(setup, bad):
    ports, _, controller = setup
    enable(controller)
    await start(setup)
    plan, reserves = controller._owned["plan"], ports.sent[-1].reserves
    members = list(plan["members"])
    skipped = [JobSkip(g, "reserve_unused") for g in reserves]
    if bad == "unknown":
        members[-1] = 999
    elif bad == "over_capacity":
        members.append(reserves[0])
        skipped.pop(0)
    elif bad == "missing_reserve":
        skipped.pop(0)
    elif bad == "missing_lead":
        members.remove(plan["tank_guid"])
        skipped.append(JobSkip(plan["tank_guid"], "offline"))
    else:
        members[-1] = members[0]
    sample = event(controller, guids=tuple(members), skipped=tuple(skipped))
    assert not controller._valid_job(sample)
    await controller.on_job(sample)
    await controller.tick()
    assert controller.session is None

