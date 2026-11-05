"""Bounded private native-kill capture codec; decoding grants no authority."""
from __future__ import annotations

from dataclasses import asdict, dataclass, fields
import hashlib
import json
import math
import re

from leeroy_mind.ingress import RowReceipt, WireOrigin, game_row_binding
from .actions import ActionContext
from .protocol import RealmKill
from .records import ActorQualification, QualificationSnapshot, RealmObservation, Rejected, StoredSource

_SCHEMA = 'realm_native_kill_retention_v1'
_MAX = 8192
_U64 = (1 << 64) - 1
_KEYS = {'v', 'type', *(field.name for field in fields(RealmKill))}


@dataclass(frozen=True, slots=True)
class NativeKillCapture:
    source: StoredSource
    snapshot: QualificationSnapshot
    context: ActionContext
    participants: tuple[int, ...]
    native_present: tuple[int, ...]
    qualified_cast: tuple[int, ...]

    def __post_init__(self):
        try:
            _validate(self)
            if len(_json(_body(self)).encode('utf-8')) > _MAX:
                raise ValueError('invalid private capture')
        except (TypeError, ValueError, AttributeError, UnicodeError, RecursionError, OverflowError):
            raise ValueError('invalid private capture') from None


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _copy(value, cls):
    if type(value) is not cls:
        raise ValueError('invalid private capture')
    if cls(**{f.name: getattr(value, f.name) for f in fields(cls)}) != value:
        raise ValueError('invalid private capture')


def _validate(capture):
    if type(capture) is not NativeKillCapture:
        raise ValueError('invalid private capture')
    source, snapshot, context = capture.source, capture.snapshot, capture.context
    _copy(source, StoredSource)
    _copy(snapshot, QualificationSnapshot)
    _copy(context, ActionContext)
    if (type(source.received_at) is not float or not math.isfinite(source.received_at)
            or type(snapshot.captured_at) is not float or not math.isfinite(snapshot.captured_at)
            or source.event_type != 'realm_kill' or source.row_kind != 'game_event'
            or type(source.top_keys) is not tuple or source.top_keys != tuple(sorted(_KEYS))
            or snapshot.human_confirmed is not True or snapshot.human in snapshot.cast
            or snapshot.guild in snapshot.rival_guilds
            or (source.guild, source.human) != (snapshot.guild, snapshot.human)
            or (context.guild, context.human) != (snapshot.guild, snapshot.human)
            or any(getattr(context, x) is not True for x in ('enabled', 'connected', 'presence_known'))
            or type(context.generation) is not int or not 0 <= context.generation <= (1 << 63) - 1
            or type(context.connection_epoch) is not int or not 1 <= context.connection_epoch <= _U64
            or snapshot.speaker_kind != 'none' or snapshot.speaker_guid != 0 or snapshot.speaker_guild != 0
            or snapshot.speaker_name != '' or snapshot.speaker_name_source != 'none'
            or snapshot.speaker_name_sha256 is not None):
        raise ValueError('invalid private capture')
    for name in ('cast', 'actors', 'rival_guilds'):
        if type(getattr(snapshot, name)) is not tuple:
            raise ValueError('invalid private capture')
    for actor in snapshot.actors:
        _copy(actor, ActorQualification)
        projection = {key: getattr(actor, key) for key in ('guid', 'guild', 'role', 'status')}
        digest = hashlib.sha256(b'realm-actor-qualification-v1\0' + _json(projection).encode('utf-8')).hexdigest()
        if (actor.status != 'active' or actor.guid == snapshot.human or actor.guid in snapshot.cast
                or actor.guild not in (*snapshot.rival_guilds, snapshot.guild) or actor.row_sha256 != digest):
            raise ValueError('invalid private capture')
    for name in ('participants', 'native_present', 'qualified_cast'):
        value = getattr(capture, name)
        if (type(value) is not tuple or len(value) > 128
                or any(type(x) is not int or not 1 <= x <= 0xffffffff for x in value)
                or value != tuple(sorted(set(value)))):
            raise ValueError('invalid private capture')
    if (not capture.native_present or capture.participants != capture.native_present
            or snapshot.human in capture.native_present
            or capture.qualified_cast != tuple(sorted(set(capture.native_present) & set(snapshot.cast)))
            or len(capture.qualified_cast) >= 5):
        raise ValueError('invalid private capture')


def _body(capture):
    _validate(capture)
    source = asdict(capture.source)
    snapshot = asdict(capture.snapshot)
    context = asdict(capture.context)
    snapshot['transport_epoch'] = str(snapshot['transport_epoch'])
    context['connection_epoch'] = str(context['connection_epoch'])
    return dict(schema=_SCHEMA, source=source, snapshot=snapshot, context=context,
                participants=capture.participants, native_present=capture.native_present,
                qualified_cast=capture.qualified_cast)


def native_kill_capture_to_json(capture):
    try:
        raw = _json(_body(capture))
        if len(raw.encode('utf-8')) > _MAX:
            raise ValueError('invalid private capture')
        return raw
    except (TypeError, ValueError, AttributeError, UnicodeError, RecursionError, OverflowError):
        raise ValueError('invalid private capture') from None


def native_kill_capture_sha256(capture):
    return hashlib.sha256(b'realm-qualification-v1\0' + native_kill_capture_to_json(capture).encode('utf-8')).hexdigest()


def native_kill_capture_from_observation(observation, source):
    try:
        _copy(observation, RealmObservation)
        _copy(observation.event, RealmKill)
        _copy(observation.receipt, RowReceipt)
        _copy(observation.receipt.wire, WireOrigin)
        event, receipt = observation.event, observation.receipt
        wire = receipt.wire
        payload = asdict(event)
        payload['cast_members'] = list(event.cast_members)
        if (receipt.backlog or event.human_present or source.source_key != wire.envelope_sha256
                or source.protocol_version != wire.protocol_version or source.event_type != wire.event_type
                or source.row_kind != receipt.row_kind or source.row_id != receipt.row_id
                or source.row_sha256 != receipt.row_sha256 or source.server_ms != wire.server_ms
                or source.received_at != wire.received_at or source.top_keys != wire.top_keys
                or source.server_ms != event.ts or observation.qualification.transport_epoch != wire.transport_epoch
                or (source.guild, source.human) != (event.guild_id, event.human_guid)
                or source.row_sha256 != game_row_binding(source.row_id, source.event_type, payload)):
            raise ValueError('invalid private capture')
        native = event.cast_members
        capture = NativeKillCapture(source, observation.qualification, observation.context, native, native,
            tuple(sorted(set(native) & set(observation.qualification.cast))))
        native_kill_capture_to_json(capture)
        return capture
    except (TypeError, ValueError, AttributeError, UnicodeError, RecursionError, OverflowError):
        raise ValueError('invalid private capture') from None


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError('duplicate')
        result[key] = value
    return result


def _epoch(value):
    if type(value) is not str or re.fullmatch(r'[1-9][0-9]{0,19}', value) is None or int(value) > _U64:
        raise ValueError('invalid epoch')
    return int(value)


def _depth(value, level=0):
    if level > 32:
        raise ValueError('depth')
    if type(value) is dict:
        for item in value.values():
            _depth(item, level + 1)
    elif type(value) is list:
        for item in value:
            _depth(item, level + 1)


def native_kill_capture_from_json(raw):
    try:
        if type(raw) is not str:
            raise ValueError('type')
        if len(raw.encode('utf-8')) > _MAX:
            return Rejected('graph_limit')
        def constant(_):
            raise ValueError('constant')
        body = json.loads(raw, object_pairs_hook=_pairs, parse_constant=constant)
        _depth(body)
        if type(body) is not dict or set(body) != {'schema', 'source', 'snapshot', 'context', 'participants', 'native_present', 'qualified_cast'} or body['schema'] != _SCHEMA:
            raise ValueError('schema')
        source, snap, ctx = body['source'], body['snapshot'], body['context']
        if (type(source) is not dict or set(source) != {f.name for f in fields(StoredSource)}
                or type(snap) is not dict or set(snap) != {f.name for f in fields(QualificationSnapshot)}
                or type(ctx) is not dict or set(ctx) != {f.name for f in fields(ActionContext)}
                or type(source['top_keys']) is not list or type(snap['cast']) is not list
                or type(snap['actors']) is not list or type(snap['rival_guilds']) is not list
                or any(type(body[k]) is not list for k in ('participants', 'native_present', 'qualified_cast'))):
            raise ValueError('shape')
        actors = []
        for actor in snap['actors']:
            if type(actor) is not dict or set(actor) != {f.name for f in fields(ActorQualification)}:
                raise ValueError('actor')
            actors.append(ActorQualification(**actor))
        source = StoredSource(**{**source, 'top_keys': tuple(source['top_keys'])})
        snapshot = QualificationSnapshot(**{**snap, 'cast': tuple(snap['cast']), 'actors': tuple(actors),
            'rival_guilds': tuple(snap['rival_guilds']), 'transport_epoch': _epoch(snap['transport_epoch'])})
        context = ActionContext(**{**ctx, 'connection_epoch': _epoch(ctx['connection_epoch'])})
        capture = NativeKillCapture(source, snapshot, context, tuple(body['participants']),
                                    tuple(body['native_present']), tuple(body['qualified_cast']))
        if native_kill_capture_to_json(capture) != raw:
            raise ValueError('canonical')
        return capture
    except (TypeError, ValueError, KeyError, AttributeError, UnicodeError, RecursionError, OverflowError):
        return Rejected('invalid_input')
