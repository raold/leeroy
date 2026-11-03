import dataclasses
import json
from pathlib import Path

import pytest

from leeroy_mind import protocol as wire
from leeroy_mind.realm import protocol


FIXTURE = Path(__file__).resolve().parents[3] / "modules/mod-leeroy/tests/fixtures/realm_protocol.jsonl"

# The native parser/publisher contract, in table order. Payload discriminators
# use arena_type/event_type so they never replace the envelope's type key.
COMMAND_FIELDS = {
    "realm_actors": "id guids gankers guilds",
    "realm_say": "id guid channel text to_guid",
    "realm_emote": "id guid emote target_guid",
    "realm_move": "id guid place slot",
    "rpg_status": "id guid status quest_id",
    "leave_pvp": "id guids pvp_owner",
    "rearm": "id guids",
    "wg_join": "id guids",
    "arena_team": "id action captain_guid arena_type name team_id guid",
    "arena_queue": "id team_id captain_guid",
    "duel": "id guid target_guid",
    "craft": "id guid spell_id count",
    "item_count": "id guid item_ids",
    "equip": "id guid item_id",
    "set_profession": "id guid skill_id",
    "transmog_set": "id guid slot entry",
    "calendar_post": "id creator_guid title description event_type at dungeon_id guild_event invite_human",
    "calendar_signup": "id event_id guid status",
    "calendar_update": "id event_id title description at",
    "mail_send": "id from_guid subject body items money delay_s",
    "bank_tab_buy": "id officer_guid tab name icon",
    "bank_money": "id amount",
    "bank_deposit": "id guid item_id count tab",
    "bank_withdraw": "id guid item_id count tab",
    "bank_stock": "id officer_guid tab item_id count",
    "grant_achievement": "id guid achievement_id",
    "guild_rename": "id guild_id name",
    "guild_grow": "id guild_id guids",
    "guild_admit": "id guid rank",
    "guild_note": "id guid officer_note",
    "announce": "id map instance_id boss_id boss_name attempts fight_s first",
    "get_realm_pvp": "id pvp_owner origin_id",
}
EVENT_FIELDS = {
    "realm_job": "ts id kind state reason guid value pvp_owner",
    "channel_chat": "ts channel from_guid from_name zone text via_bridge",
    "pvp_kill": "ts killer victim map zone",
    "duel_end": "ts winner loser result",
    "pvp_flag": "ts guid flagged",
    "wg": "ts state defender seconds update_ms",
    "arena_queue": "ts team_id arena_type rated",
    "arena_end": "ts arena_type winner_team loser_team winner_change loser_change members",
    "game_event": "ts event_id state",
    "calendar_rsvp": "ts event_id status",
    "mail_taken": "ts item_guid item_id count",
    "guard": "ts guid action rank reason",
    "realm_pvp": ("ts id pvp_owner origin_id kind origin_epoch revision guild human members stage "
                  "application_fenced reason cleanup_id"),
}


def fixtures():
    return [json.loads(line) for line in FIXTURE.read_text(encoding="utf-8").splitlines() if line]


def owner_event():
    return next(row["msg"] for row in fixtures() if row["name"] == "realm_pvp_event")


def command_from_message(msg):
    fields = {key: value for key, value in msg.items() if key not in {"type", "v", "ts"}}
    for key in {"guids", "gankers", "guilds", "item_ids"} & fields.keys():
        fields[key] = tuple(fields[key])
    if "items" in fields:
        fields["items"] = tuple(protocol.MailItem(item["item_id"], item["count"], item["from"])
                                for item in fields["items"])
    return protocol.COMMANDS[msg["type"]](**fields)


def test_authoritative_maps_fields_and_immutable_message_shapes():
    assert list(protocol.COMMANDS) == list(COMMAND_FIELDS)
    assert list(protocol.EVENTS) == list(EVENT_FIELDS)
    assert len(protocol.COMMANDS) == 32 and len(protocol.EVENTS) == 13
    for registry, expected in ((protocol.COMMANDS, COMMAND_FIELDS), (protocol.EVENTS, EVENT_FIELDS)):
        for name, cls in registry.items():
            assert [field.name for field in dataclasses.fields(cls)] == expected[name].split()
            assert cls.__dataclass_params__.frozen
            assert "__dict__" not in cls.__slots__
    assert protocol.COMMANDS["arena_queue"] is protocol.ArenaQueue
    assert protocol.EVENTS["arena_queue"] is protocol.ArenaQueueEvent
    assert protocol.NOISY == frozenset({"realm_job", "channel_chat", "game_event"})


def test_commands_round_trip_native_fixtures_with_ordered_wire_keys():
    protocol.register()
    seen = set()
    for row in fixtures():
        if row["kind"] != "command" or not row["valid"]:
            continue
        msg = row["msg"]
        cmd = command_from_message(msg)
        line = wire.encode_command(cmd, msg["ts"])
        encoded = json.loads(line)
        expected_fields = COMMAND_FIELDS[msg["type"]].split()
        if msg["type"] == "arena_team":
            expected_fields = (["id", "action", "captain_guid", "arena_type", "name"]
                               if cmd.action == "create" else ["id", "action", "team_id", "guid"])
        assert list(encoded) == ["v", "type", "ts", *expected_fields], row["name"]
        assert all(encoded[key] == value for key, value in msg.items()), row["name"]
        assert command_from_message(encoded) == cmd
        assert line.endswith(b"\n") and line.count(b"\n") == 1
        seen.add(msg["type"])
    assert seen == protocol.COMMANDS.keys()


@pytest.mark.parametrize("name,defaults", [
    ("realm_actors_defaults", {"gankers": (), "guilds": ()}),
    ("realm_say_defaults", {"to_guid": 0}),
    ("realm_emote_defaults", {"target_guid": 0}),
    ("realm_move_defaults", {"slot": 0}),
    ("rpg_status_defaults", {"quest_id": 0}),
    ("leave_pvp_ok", {"pvp_owner": ""}),
    ("craft_defaults", {"count": 1}),
    ("calendar_post_defaults", {"description": "", "dungeon_id": -1, "guild_event": True,
                                "invite_human": False}),
    ("calendar_update_defaults", {"title": "", "description": "", "at": 0}),
    ("mail_send_defaults", {"body": "", "items": (), "money": 0, "delay_s": 600}),
    ("guild_admit_defaults", {"rank": 4}),
    ("announce_defaults", {"attempts": 0, "fight_s": 0}),
])
def test_native_optional_command_defaults(name, defaults):
    msg = next(row["msg"] for row in fixtures() if row["name"] == name)
    cmd = command_from_message(msg)
    assert all(getattr(cmd, key) == value for key, value in defaults.items())


def test_required_zero_capable_count_is_not_silently_defaulted():
    with pytest.raises(TypeError, match="count"):
        protocol.BankDeposit(id="deposit", guid=11001, item_id=43015, tab=0)


def test_nested_mail_items_preserve_from_key_and_order():
    protocol.register()
    cmd = protocol.MailSend("mail", 11001, "A gift", items=(
        protocol.MailItem(43015, 2, "new"), protocol.MailItem(34753, 1, "bags")))
    encoded = json.loads(wire.encode_command(cmd, 0))
    assert encoded["items"] == [{"item_id": 43015, "count": 2, "from": "new"},
                                {"item_id": 34753, "count": 1, "from": "bags"}]
    assert all(list(item) == ["item_id", "count", "from"] for item in encoded["items"])
    assert command_from_message(encoded) == cmd


@pytest.mark.parametrize("kwargs", [
    {"action": "create", "captain_guid": 11001, "arena_type": 3, "name": "Test", "team_id": 0},
    {"action": "create", "captain_guid": 11001, "arena_type": 3, "name": "Test", "guid": 0},
    {"action": "create", "captain_guid": 11001, "name": "Test"},
    {"action": "create", "captain_guid": True, "arena_type": 3, "name": "Test"},
    {"action": "create", "captain_guid": 11001, "arena_type": 4, "name": "Test"},
    {"action": "add", "team_id": 13001, "guid": 11001, "captain_guid": 0},
    {"action": "drop", "team_id": 13001, "guid": 11001, "arena_type": 0},
    {"action": "add", "team_id": 13001, "guid": 11001, "name": ""},
    {"action": "drop", "team_id": 13001},
    {"action": "remove", "team_id": 13001, "guid": 11001},
])
def test_arena_team_rejects_missing_or_cross_action_fields(kwargs):
    with pytest.raises(ValueError, match="action fields"):
        protocol.ArenaTeam(id="team", **kwargs)


def test_events_parse_from_native_fixture():
    protocol.register()
    seen = set()
    for row in fixtures():
        if row["kind"] != "event":
            continue
        msg = row["msg"]
        parsed = wire.parse_event(json.dumps(msg))
        assert type(parsed) is protocol.EVENTS[msg["type"]]
        assert wire.event_type(parsed) == msg["type"]
        expected = {key: tuple(value) if key == "members" else value for key, value in msg.items()
                    if key not in {"type", "v"}}
        if msg["type"] == "realm_job":
            expected.setdefault("pvp_owner", "")
            assert type(parsed.value) is int
        assert dataclasses.asdict(parsed) == expected
        seen.add(msg["type"])
    assert seen == protocol.EVENTS.keys()


def test_event_fields_remain_required_unless_native_contract_has_a_default():
    protocol.register()
    for row in fixtures():
        if row["kind"] != "event":
            continue
        msg = row["msg"]
        for field in dataclasses.fields(protocol.EVENTS[msg["type"]]):
            if field.default is not dataclasses.MISSING:
                continue
            without_field = {key: value for key, value in msg.items() if key != field.name}
            with pytest.raises(wire.ProtocolError, match=field.name):
                wire.parse_event(json.dumps(without_field))


@pytest.mark.parametrize("value", [3.5, 3])
def test_wintergrasp_update_ms_preserves_fractional_and_whole_numbers(value):
    protocol.register()
    msg = next(row["msg"] for row in fixtures() if row["name"] == "wg_event")
    msg["update_ms"] = value
    parsed = wire.parse_event(json.dumps(msg))
    assert parsed.update_ms == value and type(parsed.update_ms) is float


@pytest.mark.parametrize("value", [True, "3.5", None, float("nan"), float("inf"), float("-inf"), 10**400])
def test_wintergrasp_update_ms_rejects_non_numeric_and_non_finite_values(value):
    protocol.register()
    msg = next(row["msg"] for row in fixtures() if row["name"] == "wg_event")
    msg["update_ms"] = value
    with pytest.raises(wire.ProtocolError, match="update_ms"):
        wire.parse_event(json.dumps(msg))


def test_register_is_idempotent_for_all_realm_types():
    protocol.register()
    protocol.register()
    assert json.loads(wire.encode_command(protocol.ArenaQueue("queue", 13001, 11001), 0))["type"] == "arena_queue"
    assert isinstance(wire.parse_event('{"v":1,"type":"arena_queue","ts":0,"team_id":13001,'
                                       '"arena_type":3,"rated":true}'), protocol.ArenaQueueEvent)


def test_native_owner_fixture_and_command_contracts():
    protocol.register()
    protocol.register()
    for row in fixtures():
        msg = row["msg"]
        if msg["type"] in {"realm_pvp", "realm_job"} and row["kind"] == "event":
            parsed = wire.parse_event(json.dumps(msg))
            assert dataclasses.asdict(parsed) == {
                key: tuple(value) if key == "members" else value for key, value in msg.items()
                if key not in {"type", "v"}
            } | ({"pvp_owner": ""} if msg["type"] == "realm_job" else {})
        elif msg["type"] in {"leave_pvp", "get_realm_pvp"} and row["kind"] == "command":
            cls = protocol.LeavePvp if msg["type"] == "leave_pvp" else protocol.GetRealmPvp
            fields = {key: tuple(value) if key == "guids" else value for key, value in msg.items()
                      if key not in {"type", "v", "ts"}}
            if not row["valid"]:
                with pytest.raises(ValueError):
                    cls(**fields)
            else:
                encoded = json.loads(wire.encode_command(cls(**fields), 0))
                assert all(encoded[key] == value for key, value in msg.items())


@pytest.mark.parametrize("field,value", [
    ("pvp_owner", "queue-one"), ("pvp_owner", "a" * 50),
    ("members", [101, 101]), ("members", [102, 101]), ("members", list(range(1, 152))),
    ("revision", True), ("origin_epoch", 0), ("revision", 2**64), ("application_fenced", False),
    ("stage", "empty"), ("cleanup_id", ""), ("reason", "x" * 129),
])
def test_owner_receipt_rejects_malformed_evidence(field, value):
    protocol.register()
    msg = owner_event()
    msg[field] = value
    with pytest.raises(wire.ProtocolError):
        wire.parse_event(json.dumps(msg))


def test_owner_query_requires_exactly_one_selector():
    token = owner_event()["pvp_owner"]
    with pytest.raises(ValueError):
        protocol.GetRealmPvp("query")
    with pytest.raises(ValueError):
        protocol.GetRealmPvp("query", token, "queue-one")
    assert protocol.GetRealmPvp("query", origin_id="queue-one").origin_id == "queue-one"


def test_queue_success_retains_native_owner_token_without_changing_job_state():
    protocol.register()
    event = {"v": 1, "type": "realm_job", "ts": 0, "id": "queue-one", "kind": "arena_queue",
             "state": "done", "reason": "", "guid": 0, "value": 2, "pvp_owner": owner_event()["pvp_owner"]}
    parsed = wire.parse_event(json.dumps(event))
    assert parsed.pvp_owner == event["pvp_owner"]
    assert parsed.state == "done"


@pytest.mark.parametrize("value", [True, -1, 2**64, "2", 2.0])
def test_job_value_requires_a_bounded_integer_not_a_boolean(value):
    protocol.register()
    msg = next(row["msg"] for row in fixtures() if row["name"] == "realm_job_event")
    msg["value"] = value
    with pytest.raises(wire.ProtocolError):
        wire.parse_event(json.dumps(msg))


@pytest.mark.parametrize("guids", [(), (101, 101), (0,), (True,), (2**32,), tuple(range(1, 152)), [101]])
def test_leave_pvp_preserves_native_manifest_validation(guids):
    with pytest.raises(ValueError, match="cleanup request"):
        protocol.LeavePvp("cleanup", guids)


def test_clean_native_receipt_does_not_relax_cleanup_proof():
    protocol.register()
    msg = owner_event()
    msg["stage"] = "clean"
    assert wire.parse_event(json.dumps(msg)).cleanup_id == msg["cleanup_id"]
    for field, value in (("cleanup_id", ""), ("application_fenced", False), ("pvp_owner", "")):
        with pytest.raises(wire.ProtocolError):
            wire.parse_event(json.dumps({**msg, field: value}))


def test_unknown_native_receipt_before_cleanup_accepts_empty_cleanup_id():
    protocol.register()
    msg = {**owner_event(), "stage": "unknown", "application_fenced": True,
           "cleanup_id": "", "reason": "body_identity_changed"}
    parsed = wire.parse_event(json.dumps(msg))
    assert parsed.stage == "unknown" and parsed.application_fenced is True
    assert parsed.cleanup_id == "" and parsed.reason == "body_identity_changed"
    assert parsed.pvp_owner == msg["pvp_owner"]
    assert parsed.members == tuple(msg["members"])


@pytest.mark.parametrize("field,value", [
    ("application_fenced", False), ("pvp_owner", ""), ("pvp_owner", "queue-one"),
    ("members", []), ("members", [101, 101]), ("members", [102, 101]),
    ("origin_epoch", 0), ("revision", 0),
])
def test_unknown_native_receipt_still_requires_fence_token_and_full_manifest(field, value):
    protocol.register()
    msg = {**owner_event(), "stage": "unknown", "application_fenced": True,
           "cleanup_id": "", "reason": "body_identity_changed", field: value}
    with pytest.raises(wire.ProtocolError):
        wire.parse_event(json.dumps(msg))
