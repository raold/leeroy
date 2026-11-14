import dataclasses
import json
from pathlib import Path

import pytest

from leeroy_mind import protocol as bridge
from leeroy_mind.guild.protocol import (
    COMMANDS, BotDo, ClearLockout, FormRaid, GearUp, GuildGroup, GuildGroupAdd, Job, JobSkip,
    RaidControl, SetLootMaster, SetSpec, StartOfflineRaid, StockItems, Summon, register,
)


FIXTURE = (Path(__file__).resolve().parents[3]
           / "modules/mod-leeroy/tests/fixtures/p1d_protocol.jsonl")


def lines(**where):
    rows = [json.loads(line) for line in FIXTURE.read_text(encoding="utf-8").splitlines()
            if line.strip()]
    return [row for row in rows if all(row.get(key) == value for key, value in where.items())]


def test_full_command_lines_encode_identically():
    register()
    rows = lines(kind="command", full=True)
    assert {row["msg"]["type"] for row in rows} == set(COMMANDS)
    for row in rows:
        msg = row["msg"]
        fields = {key: tuple(value) if isinstance(value, list) else value
                  for key, value in msg.items() if key not in ("v", "type", "ts")}
        encoded = bridge.encode_command(COMMANDS[msg["type"]](**fields), ts_ms=msg["ts"])
        assert encoded.endswith(b"\n") and encoded.count(b"\n") == 1
        assert json.loads(encoded) == msg
        # The fixture is also the field-order contract shared with the parser.
        assert list(json.loads(encoded)) == ["v", "type", "ts", *fields]


def test_job_event_parses():
    register()
    ev = bridge.parse_event(json.dumps(lines(kind="event")[0]["msg"]))
    assert ev == Job(ts=0, id="t1", kind="form_raid", state="done", reason="", stage="",
                     guids=(1101, 1102), skipped=(JobSkip(1103, "in_bg"),), kills=0,
                     wipes=0, instance_id=0, guid=0, level=0, avg_ilvl=0)
    assert bridge.event_type(ev) == "job"


def test_all_job_fixtures_round_trip_typed_nested_fields():
    register()
    for row in lines(kind="event"):
        msg = row["msg"]
        if msg["type"] != "job":
            continue
        ev = bridge.parse_event(json.dumps(msg))
        assert type(ev) is Job
        assert type(ev.guids) is tuple and type(ev.skipped) is tuple
        assert all(type(skip) is JobSkip for skip in ev.skipped)
        expected = dict(members_in_instance=None, last_progress_ts=None, boss_engaged=None, dc_state=None)
        expected.update({key: value for key, value in msg.items() if key not in ("v", "type")})
        assert json.loads(json.dumps(dataclasses.asdict(ev))) == expected


def test_guild_group_fixtures_parse_as_the_server_writes_them():
    register()
    rows = {row["name"]: row["msg"] for row in lines(kind="event") if row["msg"]["type"] == "guild_group"}
    assert set(rows) == {"guild_group_built", "guild_group_entered"}
    built = bridge.parse_event(json.dumps(rows["guild_group_built"]))
    assert built == GuildGroup(ts=0, reason="built", size=10, leader_guid=1, raid=True, map=571,
                               instance_id=0, difficulty=0,
                               added=(GuildGroupAdd(1101, "tank"), GuildGroupAdd(1102, "healer"),
                                      GuildGroupAdd(1103, "damage")),
                               members=(1101, 1102, 1103))
    assert bridge.event_type(built) == "guild_group"
    entered = bridge.parse_event(json.dumps(rows["guild_group_entered"]))
    assert (entered.reason, entered.map, entered.instance_id, entered.difficulty, entered.added) == (
        "entered", 631, 77, 1, ())
    for msg in rows.values():
        ev = bridge.parse_event(json.dumps(msg))
        assert json.loads(json.dumps(dataclasses.asdict(ev))) == {
            key: value for key, value in msg.items() if key not in ("v", "type")}
    missing = dict(rows["guild_group_entered"])
    del missing["difficulty"]
    with pytest.raises(bridge.ProtocolError):
        bridge.parse_event(json.dumps(missing))


def test_register_is_idempotent_and_preserves_existing_types():
    original_events = bridge._events.copy()
    original_commands = bridge._commands.copy()
    register()
    register()
    assert all(bridge._events[name] is cls for name, cls in original_events.items())
    assert all(bridge._commands[name] is cls for name, cls in original_commands.items())
    assert bridge._events["job"] is Job
    assert bridge._events["guild_group"] is GuildGroup
    assert all(bridge._commands[name] is cls for name, cls in COMMANDS.items())
    assert json.loads(bridge.encode_command(bridge.Ping("still-core"), 5))["type"] == "ping"


@pytest.mark.parametrize(("cmd", "expected"), [
    (FormRaid(id="t", leader_guid=1, members=(1101,), size=10, map_id=631,
              main_tank_guid=1101), {"heroic": False, "assistants": []}),
    (Summon(id="t", map_id=631, guids=(1101,)),
     {"where": "entrance", "summon_human": False, "summoner_guid": 0}),
    (ClearLockout(id="t", map_id=631, guids=(1101,)), {"difficulties": [0, 1, 2, 3]}),
    (SetLootMaster(id="t", member_guid=1, looter_guid=1101), {"threshold": 4}),
    (StartOfflineRaid(id="t", map_id=631, size=10, leader_guid=1101, members=(1102,),
                      loot_master_guid=1102), {"heroic": False, "assistants": [],
                      "max_wipes": 6, "max_wipes_per_boss": 3, "timeout_min": 180,
                      "min_members": 2}),
    (GearUp(id="t", guids=(1101,)), {"ilvl": 264, "level_to": 80}),
    (RaidControl(id="t", member_guid=1, action="disband"), {"leader_guid": 0, "param": ""}),
    (SetSpec(id="t", guid=1101, spec_no=2), {"glyphs": True}),
    (BotDo(id="t", guids=(1101,), action="cast", param="rebirth"), {"target_guid": 0}),
    (StockItems(id="t", guid=1101, items=(43015,)), {"count": 5}),
])
def test_command_defaults_are_encoded(cmd, expected):
    register()
    msg = json.loads(bridge.encode_command(cmd, 17))
    assert {key: msg[key] for key in expected} == expected


@pytest.mark.parametrize(("size", "member_count", "expected"), [
    (10, 1, 2), (10, 9, 8), (25, 1, 2), (25, 24, 20),
])
def test_offline_minimum_defaults_to_available_or_four_fifths(size, member_count, expected):
    cmd = StartOfflineRaid(id="t", map_id=631, size=size, leader_guid=1101,
                           members=tuple(range(1102, 1102 + member_count)),
                           loot_master_guid=1101)
    assert type(cmd.min_members) is int and cmd.min_members == expected


@pytest.mark.parametrize("minimum", [0, 1, 3])
def test_explicit_offline_minimum_is_never_replaced(minimum):
    cmd = StartOfflineRaid(id="t", map_id=631, size=10, leader_guid=1101,
                           members=(1102,), loot_master_guid=1101, min_members=minimum)
    assert cmd.min_members == minimum


@pytest.mark.parametrize(("field", "value"), [
    ("guids", [True]), ("guids", [1101.0]), ("guids", "1101"),
    ("skipped", [{"guid": True, "reason": "offline"}]),
    ("skipped", [{"guid": 1101.0, "reason": "offline"}]),
    ("skipped", [{"guid": 1101, "reason": 3}]),
    ("skipped", [{"guid": 1101}]), ("skipped", [1101]),
    ("kills", False), ("avg_ilvl", 264.5),
])
def test_job_rejects_malformed_typed_fields(field, value):
    register()
    msg = lines(kind="event")[0]["msg"]
    msg[field] = value
    with pytest.raises(bridge.ProtocolError, match=field):
        bridge.parse_event(json.dumps(msg))


@pytest.mark.parametrize("field", [f.name for f in dataclasses.fields(Job)
                                  if f.default is dataclasses.MISSING])
def test_every_nondefault_job_field_is_required(field):
    register()
    msg = lines(kind="event")[0]["msg"]
    del msg[field]
    with pytest.raises(bridge.ProtocolError, match=field):
        bridge.parse_event(json.dumps(msg))


def test_older_job_producer_has_no_native_training_scope():
    register()
    msg = lines(kind="event")[0]["msg"]
    msg.pop("training_run")
    assert bridge.parse_event(json.dumps(msg)).training_run == ""


def test_job_progress_optional_fields_round_trip_and_old_native_defaults():
    register()
    msg = lines(kind="event")[0]["msg"]
    old = bridge.parse_event(json.dumps(msg))
    assert old.members_in_instance is old.last_progress_ts is old.boss_engaged is old.dc_state is None
    msg.update(members_in_instance=0, last_progress_ts=123, boss_engaged=False)
    ev = bridge.parse_event(json.dumps(msg))
    assert (ev.members_in_instance, ev.last_progress_ts, ev.boss_engaged) == (0, 123, False)


@pytest.mark.parametrize(('field', 'value'), [('members_in_instance', True),
    ('last_progress_ts', 1.5), ('boss_engaged', 0), ('dc_state', 1)])
def test_job_progress_fields_are_strictly_typed(field, value):
    register()
    msg = lines(kind="event")[0]["msg"]
    msg[field] = value
    with pytest.raises(bridge.ProtocolError, match=field):
        bridge.parse_event(json.dumps(msg))


def test_messages_are_frozen_and_slotted():
    cmd = Summon(id="t", map_id=631, guids=(1101,))
    with pytest.raises(dataclasses.FrozenInstanceError):
        cmd.map_id = 249
    assert not hasattr(cmd, "__dict__")
    skip = JobSkip(1101, "offline")
    with pytest.raises(dataclasses.FrozenInstanceError):
        skip.reason = "dead"
    assert not hasattr(skip, "__dict__")


def test_offline_raid_heartbeat_fixture_parses_with_progress_fields():
    register()
    row = next(r for r in lines(kind="event") if r["name"] == "job_offline_raid_progress")
    ev = bridge.parse_event(json.dumps(row["msg"]))
    assert (ev.kind, ev.state, ev.stage, ev.kills, ev.instance_id) == ("offline_raid", "progress", "running", 1, 77)
    assert (ev.members_in_instance, ev.last_progress_ts, ev.boss_engaged) == (2, 1700000000000, False)
    assert ev.dc_state == "door"
