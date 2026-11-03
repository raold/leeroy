import dataclasses
import json
from pathlib import Path

import pytest

from leeroy_mind import protocol as wire
from leeroy_mind.realm import protocol


FIXTURE = Path(__file__).resolve().parents[3] / "modules/mod-leeroy/tests/fixtures/realm_protocol.jsonl"


def fixtures():
    return [json.loads(line) for line in FIXTURE.read_text(encoding="utf-8").splitlines() if line]


def owner_event():
    return next(row["msg"] for row in fixtures() if row["name"] == "realm_pvp_event")


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
