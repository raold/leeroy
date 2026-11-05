import dataclasses
import json
from pathlib import Path

import pytest

from leeroy_mind.guild import protocol as guild
from leeroy_mind.protocol import ProtocolError, encode_command, event_type, parse_event
from leeroy_mind.training import protocol as tg


FIXTURE = Path(__file__).resolve().parents[3] / "modules/mod-leeroy/tests/fixtures/training_protocol.jsonl"


def lines():
    return [json.loads(line) for line in FIXTURE.read_text(encoding="utf-8").splitlines() if line]


def message(name):
    return next(row["msg"] for row in lines() if row["name"] == name)


def test_full_command_lines_encode_identically():
    tg.register()
    for row in lines():
        if row["kind"] != "command" or not row.get("full") or not row["valid"]:
            continue
        msg = row["msg"]
        if msg["type"] not in tg.COMMANDS:
            continue
        fields = {({"class": "class_", "max": "max_"}.get(k, k)):
                  tuple(v) if isinstance(v, list) else v
                  for k, v in msg.items() if k not in ("v", "type", "ts")}
        assert json.loads(encode_command(tg.COMMANDS[msg["type"]](**fields), 0)) == msg


def test_every_event_line_parses_with_nested_types_and_signed_fields():
    guild.register()
    tg.register()
    for row in lines():
        if row["kind"] == "event" and row["valid"]:
            assert event_type(parse_event(json.dumps(row["msg"]))) == row["msg"]["type"]
    member = parse_event(json.dumps(message("pull_member")))
    assert member.class_ == 6 and member.tank is False
    assert member.taken_by == (tg.TakenTotal(69146, 36672, 40000, 4),)
    track = parse_event(json.dumps(message("pull_track_boss")))
    assert track.casts == (tg.TrackCast(69076, 45),)
    dc = parse_event(json.dumps(message("vocab_server"))).dc[0]
    assert (dc.min_, dc.max_, dc.heroic) == (1000, 12000, -1)


def test_register_is_idempotent_and_never_shadows_guild():
    guild.register()
    tg.register()
    tg.register()
    assert "job" not in tg.EVENTS
    assert tg.TG_EVENT_TYPES == frozenset(tg.EVENTS)
    assert type(parse_event(json.dumps(message("job_benchmark_done")))) is guild.Job


def test_command_defaults_and_explicit_invalid_minimum_remain_distinct():
    tg.register()
    assert tg.Telemetry("a", False).watch == ()
    assert tg.GetVocab("a").probes == ()
    assert tg.DcSetting("a", 101, "clear").key == ""
    assert json.loads(encode_command(tg.GetRivals("a"), 0))["max"] == 200
    cmd = tg.StartBenchmark("a", 631, 10, 5001, (5002, 5003), (0,))
    assert cmd.min_members == 3
    assert dataclasses.replace(cmd, min_members=0).min_members == 0
    assert cmd.heroic is False and cmd.max_wipes == 6 and cmd.timeout_min == 180
    with pytest.raises(dataclasses.FrozenInstanceError):
        cmd.size = 25


@pytest.mark.parametrize("name,field,value", [
    ("pull_member", "tank", 1), ("pull_member", "damage", True),
    ("pull_death", "x", -3.5), ("pull", "assisted", "false"),
    ("vocab_class", "spells_checked", 1), ("pull_track_boss", "casts", {}),
])
def test_malformed_event_types_fail_closed(name, field, value):
    tg.register()
    msg = message(name)
    msg[field] = value
    with pytest.raises(ProtocolError):
        parse_event(json.dumps(msg))


def test_nested_payload_mandatory_and_strict():
    tg.register()
    msg = message("pull_member")
    del msg["taken_by"][0]["source"]
    with pytest.raises(ProtocolError, match="source"):
        parse_event(json.dumps(msg))
    msg = message("pull_track_boss")
    msg["casts"][0]["at_s"] = True
    with pytest.raises(ProtocolError, match="at_s"):
        parse_event(json.dumps(msg))


@pytest.mark.parametrize("name,fields", [
    ("pull", {"damage_basis": "requested_pre_ai", "healing_basis": "effective_gain_with_pre_absorb_raw",
              "spell_basis": "best_effort_thread_pair", "interrupt_basis": "successful_interrupt_spell_cast"}),
    ("dummy_result", {"damage_basis": "requested_pre_ai", "spell_basis": "best_effort_thread_pair"}),
])
def test_metric_provenance_is_explicit_and_legacy_defaults_unknown(name, fields):
    tg.register()
    msg = message(name)
    msg.update(fields)
    ev = parse_event(json.dumps(msg))
    for field, basis in fields.items():
        assert getattr(ev, field) == basis
        del msg[field]
    old = parse_event(json.dumps(msg))
    assert all(getattr(old, field) == "unknown" for field in fields)
    for field in fields:
        bad = msg | {field: True}
        with pytest.raises(ProtocolError):
            parse_event(json.dumps(bad))
