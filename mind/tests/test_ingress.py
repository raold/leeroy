"""Complete native-envelope identity and immutable stored-row projections."""

import dataclasses
import hashlib
import json

import pytest

from leeroy_mind.ingress import RowReceipt, WireOrigin, chat_row_binding, game_row_binding
from leeroy_mind.protocol import ProtocolError, parse_event, parse_event_with_origin

MS = 1793304000123
RECEIVED = MS / 1000


def envelope(**extra):
    return {"v": 1, "type": "chat", "ts": MS, "channel": "guild", "from_guid": 7,
            "from_name": "Rook", "to_guid": 0, "text": "ready", "map": 0, "zone": 1519,
            **extra}


def parse(value, *, epoch=1, received=RECEIVED):
    line = json.dumps(value) if isinstance(value, dict) else value
    return parse_event_with_origin(line, received_at=received, transport_epoch=epoch)


def sha(prefix, value):
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False, allow_nan=False).encode("utf-8")
    return hashlib.sha256(prefix + b"\0" + canonical).hexdigest()


def test_full_envelope_hash_includes_unknown_fields_defaults_and_unicode_without_normalizing():
    raw = envelope(extension={"labels": ["é", "é"], "n": None})
    ev, wire = parse(raw)
    assert ev == parse_event(json.dumps(raw))
    assert wire.envelope_sha256 == sha(b"realm-envelope-v1", raw)
    assert wire.top_keys == tuple(sorted(raw))
    assert wire.protocol_version == 1 and wire.event_type == "chat" and wire.server_ms == MS
    assert wire.received_at == RECEIVED and wire.transport_epoch == 1 and wire.origin == "bridge"
    assert parse(envelope())[1].envelope_sha256 != wire.envelope_sha256
    assert parse(envelope(via_bridge=False))[1].envelope_sha256 != parse(envelope())[1].envelope_sha256
    assert parse(envelope(text="é"))[1].envelope_sha256 != parse(envelope(text="é"))[1].envelope_sha256
    reversed_keys = dict(reversed(list(raw.items())))
    assert parse(reversed_keys, epoch=2, received=RECEIVED + 60)[1].envelope_sha256 == wire.envelope_sha256
    assert parse(envelope(extension=[1, 2]))[1].envelope_sha256 != parse(envelope(extension=[2, 1]))[1].envelope_sha256


@pytest.mark.parametrize("suffix", [
    '"extra":1,"extra":2', '"extra":{"a":1,"a":2}',
    '"extra":NaN', '"extra":Infinity', '"extra":-Infinity', '"extra":1e999',
    '"extra":"\\ud800"', '"\\udfff":1',
])
def test_strictly_unbound_envelopes_keep_ordinary_parser_semantics(suffix):
    line = json.dumps(envelope())[:-1] + "," + suffix + "}"
    ordinary = parse_event(line)
    ev, wire = parse(line)
    assert ev == ordinary and wire is None


def test_replaced_utf8_is_ordinary_only():
    line = json.dumps(envelope()).encode()[:-1] + b',"extra":"\xff"}'
    ev, wire = parse(line)
    assert ev == parse_event(line) and wire is None


def test_depth_key_and_canonical_byte_limits_are_whole_envelope_limits():
    # The root object and 31 nested arrays are 32 containers.
    nested = 0
    for _ in range(31):
        nested = [nested]
    assert parse(envelope(extra=nested))[1] is not None
    assert parse(envelope(extra=[nested]))[1] is None
    raw = envelope()
    raw.update({str(i): None for i in range(64 - len(raw))})
    assert parse(raw)[1] is not None
    raw["one_more"] = None
    assert parse(raw)[1] is None
    # Valid compact input under the transport limit expands past the canonical
    # limit when JSON floats acquire an explicit exponent sign.
    line = json.dumps(envelope())[:-1] + ',"extra":[' + ','.join(['1e20'] * 12000) + ']}'
    assert len(line.encode()) <= 65536
    assert parse_event(line).text == "ready"
    assert parse(line)[1] is None


@pytest.mark.parametrize("ts", [0, -1, 253402300800000])
def test_source_timestamp_bounds_do_not_reject_ordinary_events(ts):
    ev, wire = parse(envelope(ts=ts))
    assert ev.ts == ts and wire is None


@pytest.mark.parametrize("ts", [1, 253402300799999])
def test_source_timestamp_endpoints_are_accepted(ts):
    assert parse(envelope(ts=ts))[1].server_ms == ts


@pytest.mark.parametrize("received", [0, 253402300799.999])
def test_receive_time_endpoints_are_accepted(received):
    assert parse(envelope(), received=received)[1].received_at == received


@pytest.mark.parametrize("received", [True, -1, float("nan"), float("inf"), 253402300800])
def test_invalid_receive_time_cannot_create_origin(received):
    assert parse(envelope(), received=received)[1] is None


@pytest.mark.parametrize("epoch", [None, True, 0, -1, "1", 0x10000000000000000])
def test_origin_requires_actual_positive_uint64_epoch(epoch):
    assert parse(envelope(), epoch=epoch)[1] is None


@pytest.mark.parametrize("line", ['[]', '{}', '{"v":1,"type":"missing","ts":1}', 'bad json'])
def test_metadata_parser_preserves_ordinary_rejections(line):
    with pytest.raises(ProtocolError) as ordinary:
        parse_event(line)
    with pytest.raises(type(ordinary.value)) as decorated:
        parse(line)
    assert str(decorated.value) == str(ordinary.value)


def test_row_bindings_have_exact_id_kind_types_nulls_and_complete_payload():
    payload = {"ts": MS, "guid": 7, "extra": {"maybe": None, "values": [1, 2]}}
    expected = {"row_kind": "game_event", "row_id": 17, "type": "death", "payload": payload}
    binding = game_row_binding(17, "death", payload)
    assert binding == sha(b"realm-row-binding-v1", expected)
    assert binding == game_row_binding(17, "death", dict(reversed(list(payload.items()))))
    assert binding != game_row_binding(18, "death", payload)
    assert binding != game_row_binding(17, "resurrect", payload)
    assert binding != game_row_binding(17, "death", {**payload, "guid": 7.0})
    assert binding != game_row_binding(17, "death", {**payload, "extra": {}})
    fields = dict(channel="guild", from_guid=7, from_name="Rook", to_guid=0,
                  text="ready", map=0, zone=1519, origin="human")
    chat = chat_row_binding(17, **fields)
    assert chat == sha(b"realm-row-binding-v1", {"row_kind": "chat_line", "row_id": 17, **fields})
    for key, value in dict(channel="party", from_guid=8, from_name="Wren", to_guid=9,
                           text="changed", map=1, zone=12, origin="game").items():
        assert chat != chat_row_binding(17, **{**fields, key: value})
    with pytest.raises(ValueError):
        chat_row_binding(17, **{**fields, "from_guid": True})
    with pytest.raises(ValueError):
        game_row_binding(True, "death", payload)


def test_dtos_copy_keys_and_remain_frozen_snapshots():
    wire = parse(envelope())[1]
    keys = list(wire.top_keys)
    copied = dataclasses.replace(wire, top_keys=keys)
    keys.append("later")
    assert type(copied.top_keys) is tuple and copied.top_keys == wire.top_keys
    receipt = RowReceipt("chat_line", 1, "a" * 64, copied, False)
    assert not hasattr(wire, "__dict__") and not hasattr(receipt, "__dict__")
    with pytest.raises(dataclasses.FrozenInstanceError):
        receipt.row_sha256 = "b" * 64
    with pytest.raises(ValueError):
        dataclasses.replace(wire, server_ms=True)
    with pytest.raises(ValueError):
        dataclasses.replace(receipt, backlog=1)
