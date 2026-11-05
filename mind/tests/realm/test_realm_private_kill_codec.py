"""Canonical private capture shape, bounds and immutable binding."""
from dataclasses import replace
import hashlib
import json

import pytest

from leeroy_mind.realm.kill_capture import (NativeKillCapture, native_kill_capture_from_json,
    native_kill_capture_from_observation, native_kill_capture_sha256, native_kill_capture_to_json)
from leeroy_mind.realm.records import Rejected
from leeroy_mind.realm.source_store import claim_native_kill_source_in_unit
from test_realm_live_proof import HUMAN, observe, roster
from test_realm_private_kill import POLICY, PRIVATE


def capture(roster):
    with roster.transaction():
        obs = observe(roster, POLICY, payload=PRIVATE)
        source, created = claim_native_kill_source_in_unit(roster, obs, human=HUMAN)
        assert created
        return native_kill_capture_from_observation(obs, source)


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def test_roundtrip_preserves_all_snapshot_and_source_fields(roster):
    value = capture(roster)
    raw = native_kill_capture_to_json(value)
    body = json.loads(raw)
    assert len(body['snapshot']) == 14 and len(body['source']) == 12
    assert body['snapshot']['transport_epoch'] == '7' and body['context']['connection_epoch'] == '11'
    assert native_kill_capture_from_json(raw) == value
    assert len(native_kill_capture_sha256(value)) == 64
    assert 'Private Nickname' not in raw and 'Synthetic Private' not in raw


@pytest.mark.parametrize('mutation', [
    lambda b: b.update(known_by=[]),
    lambda b: b['source'].pop('row_sha256'),
    lambda b: b['source'].update(received_at=1),
    lambda b: b['source'].update(id=True),
    lambda b: b['source'].update(source_key='A' * 64),
    lambda b: b['source'].update(top_keys=b['source']['top_keys'][:-1]),
    lambda b: b['context'].update(connection_epoch='01'),
    lambda b: b['context'].update(generation=True),
    lambda b: b['context'].update(enabled=False),
    lambda b: b['context'].update(proof='hidden'),
    lambda b: b['snapshot'].update(captured_at=1),
    lambda b: b['snapshot'].update(transport_epoch='18446744073709551616'),
    lambda b: b['snapshot'].update(human_confirmed=1),
    lambda b: b['snapshot'].update(cast=[101, 101]),
    lambda b: b['snapshot'].update(rival_guilds=[50]),
    lambda b: b['snapshot']['actors'][0].update(row_sha256='0' * 64),
    lambda b: b['snapshot']['actors'][0].update(status='retired'),
    lambda b: b['snapshot']['actors'][0].update(guid=101),
    lambda b: b.update(native_present=[104, 103, 102, 101]),
    lambda b: b.update(participants=[10]),
    lambda b: b.update(qualified_cast=[101]),
])
def test_wrong_or_hidden_fields_and_derivations_are_rejected(roster, mutation):
    body = json.loads(native_kill_capture_to_json(capture(roster)))
    mutation(body)
    assert type(native_kill_capture_from_json(canonical(body))) is Rejected


def test_noncanonical_duplicate_nonfinite_surrogate_and_oversize_rejected(roster):
    raw = native_kill_capture_to_json(capture(roster))
    assert type(native_kill_capture_from_json(raw.replace('"schema":', '"schema":"duplicate","schema":'))) is Rejected
    assert type(native_kill_capture_from_json(' ' + raw)) is Rejected
    assert type(native_kill_capture_from_json(raw.replace('1010.0', '1e999', 1))) is Rejected
    assert type(native_kill_capture_from_json(raw.replace('proof-test-process', '\\ud800'))) is Rejected
    assert native_kill_capture_from_json(' ' * 8193) == Rejected('graph_limit')
    assert type(native_kill_capture_from_json('[' * 34 + '0' + ']' * 34)) is Rejected


def test_forged_capture_and_source_cannot_be_serialized(roster):
    value = capture(roster)
    with pytest.raises(ValueError, match='invalid private capture'):
        native_kill_capture_to_json(replace(value, qualified_cast=(101,)))
    with pytest.raises(ValueError, match='invalid private capture'):
        native_kill_capture_to_json(replace(value, source=replace(value.source, event_type='other')))


def test_direct_constructor_rejects_invalid_shape_and_mutable_aliases(roster):
    value = capture(roster)
    with pytest.raises(ValueError, match='^invalid private capture$'):
        NativeKillCapture(None, None, None, [], [], [])
    for name in ('participants', 'native_present', 'qualified_cast'):
        members = list(getattr(value, name))
        with pytest.raises(ValueError, match='^invalid private capture$'):
            replace(value, **{name: members})
    for name in ('source', 'snapshot', 'context'):
        with pytest.raises(ValueError, match='^invalid private capture$'):
            replace(value, **{name: None})
    with pytest.raises(ValueError, match='^invalid private capture$'):
        replace(value, qualified_cast=(101,))
    actor = replace(value.snapshot.actors[0], row_sha256='0' * 64)
    snapshot = replace(value.snapshot, actors=(actor,))
    with pytest.raises(ValueError, match='^invalid private capture$'):
        replace(value, snapshot=snapshot)
    valid = NativeKillCapture(value.source, value.snapshot, value.context, value.participants,
                              value.native_present, value.qualified_cast)
    assert native_kill_capture_from_json(native_kill_capture_to_json(valid)) == value


def _bad_actor_guild(body):
    actor = body['snapshot']['actors'][0]
    actor['guild'] = 999
    projection = {key: actor[key] for key in ('guid', 'guild', 'role', 'status')}
    actor['row_sha256'] = hashlib.sha256(b'realm-actor-qualification-v1\0' + canonical(projection).encode()).hexdigest()


def _five_qualified(body):
    members = [101, 102, 103, 104, 105]
    body['snapshot']['cast'] = members
    body['participants'] = members
    body['native_present'] = members
    body['qualified_cast'] = members


@pytest.mark.parametrize('mutation', [
    lambda b: b['source'].pop('id'),
    lambda b: b['source'].update(proof='hidden'),
    lambda b: b['context'].pop('guild'),
    lambda b: b['context'].update(output_fields=[]),
    lambda b: b['source'].update(id=1.0),
    lambda b: b['context'].update(connection_epoch='+11'),
    lambda b: b['context'].update(connection_epoch='0'),
    lambda b: b['snapshot'].update(transport_epoch='07'),
    lambda b: b['snapshot'].update(transport_epoch='1.0'),
    _bad_actor_guild,
    _five_qualified,
])
def test_distinct_nested_codec_invariants(roster, mutation):
    body = json.loads(native_kill_capture_to_json(capture(roster)))
    mutation(body)
    assert type(native_kill_capture_from_json(canonical(body))) is Rejected


@pytest.mark.parametrize('marker,duplicate', [
    ('"source":{', '"source":{"id":1,'),
    ('"context":{', '"context":{"enabled":true,'),
    ('"actors":[{', '"actors":[{"guid":201,'),
])
def test_duplicate_nested_keys_rejected(roster, marker, duplicate):
    raw = native_kill_capture_to_json(capture(roster))
    assert marker in raw
    assert type(native_kill_capture_from_json(raw.replace(marker, duplicate, 1))) is Rejected