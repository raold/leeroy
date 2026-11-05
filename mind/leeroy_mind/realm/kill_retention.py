"""Atomic private retention of first unqualified native kills.

These records are historical evidence, never a publication or send token.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import re

from leeroy_mind.storage.store import Store
from .kill_capture import (NativeKillCapture, native_kill_capture_from_json,
    native_kill_capture_from_observation, native_kill_capture_sha256, native_kill_capture_to_json)
from .protocol import RealmKill
from .provenance import validate_live_binding_in_unit
from .records import RealmObservation, Rejected
from .source_store import claim_native_kill_source_in_unit, read_native_kill_source_in_unit

_POLICY = 'verified_raid_kill_v1'
_COLUMNS = ('kill_id,source,native_boot,native_epoch,guild,human,map,instance_id,boss_id,'
            'difficulty,size,heroic,killed_ms,verified_ms,published_ms,native_cast_json,'
            'human_present,qualified_cast_json,qualification_sha256,fact,qualification_json')


@dataclass(frozen=True, slots=True)
class PrivateKillRecord:
    event: RealmKill
    capture: NativeKillCapture


class PrivateKillRejected(Exception):
    def __init__(self, rejection: Rejected):
        self.rejection = rejection
        super().__init__(rejection.reason)


def _reject(reason='proof_changed'):
    raise PrivateKillRejected(Rejected(reason))


def _accepted(value):
    if type(value) is Rejected:
        raise PrivateKillRejected(value)
    return value


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _values(event, capture):
    raw = native_kill_capture_to_json(capture)
    return (event.kill_id, capture.source.id, event.kill_id.split(':', 1)[0], str(event.native_epoch),
            event.guild_id, event.human_guid, event.map, event.instance_id, event.boss_id,
            event.difficulty, event.size, int(event.heroic), event.killed_at_ms, event.verified_at_ms,
            event.ts, _json(event.cast_members), int(event.human_present), _json(capture.qualified_cast),
            native_kill_capture_sha256(capture), None, raw)


def _valid_id(value):
    return (type(value) is str and re.fullmatch(r'[0-9a-f]{32}:[1-9][0-9]{0,19}', value) is not None
            and int(value[33:]) <= (1 << 64) - 1)


class PrivateKillStore:
    def __init__(self, store: Store):
        self._store = store

    @property
    def store(self):
        return self._store

    def _require_unit(self):
        if not isinstance(self.store, Store) or not self.store.in_unit:
            raise RuntimeError('PrivateKillStore requires an active Store unit')

    def record(self, observation: RealmObservation, *, human):
        if not isinstance(self.store, Store) or self.store.in_unit:
            raise RuntimeError('record requires an outer Store unit')
        try:
            with self.store.transaction():
                return self.record_in_unit(observation, human=human)
        except PrivateKillRejected as error:
            return error.rejection

    def record_in_unit(self, observation: RealmObservation, *, human):
        self._require_unit()
        if type(observation) is not RealmObservation or type(observation.event) is not RealmKill:
            _reject('invalid_input')
        _accepted(validate_live_binding_in_unit(self.store, observation, human=human, policy=_POLICY))
        event = observation.event
        existing = self.store.conn.execute('SELECT fact FROM realm_kill WHERE kill_id=?', (event.kill_id,)).fetchone()
        if existing is not None:
            if existing['fact'] is not None:
                _reject()
            original = _accepted(self.get_in_unit(event.kill_id))
            if original.event != event:
                _reject()
            key = observation.receipt.wire.envelope_sha256
            if self.store.conn.execute('SELECT id FROM realm_source WHERE source_key=?', (key,)).fetchone() is not None:
                _, created = _accepted(claim_native_kill_source_in_unit(self.store, observation, human=human))
                if created:
                    _reject()
            return original
        source, created = _accepted(claim_native_kill_source_in_unit(self.store, observation, human=human))
        if not created:
            _reject()
        try:
            capture = native_kill_capture_from_observation(observation, source)
            values = _values(event, capture)
        except ValueError:
            _reject('invalid_input')
        self.store.conn.execute(f'INSERT INTO realm_kill({_COLUMNS}) VALUES ({",".join("?" for _ in values)})', values)
        record = PrivateKillRecord(event, capture)
        if _accepted(self.get_in_unit(event.kill_id)) != record:
            _reject()
        return record

    def get_in_unit(self, kill_id: str):
        self._require_unit()
        if not _valid_id(kill_id):
            return Rejected('invalid_input')
        row = self.store.conn.execute(f'SELECT {_COLUMNS} FROM realm_kill WHERE kill_id=?', (kill_id,)).fetchone()
        if row is None:
            return Rejected('missing_row')
        if row['fact'] is not None or row['qualification_json'] is None:
            return Rejected('proof_changed')
        capture = native_kill_capture_from_json(row['qualification_json'])
        if type(capture) is Rejected:
            return Rejected('proof_changed')
        source_event = read_native_kill_source_in_unit(self.store, row['source'])
        if type(source_event) is Rejected:
            return Rejected('proof_changed')
        source, event = source_event
        try:
            if (source != capture.source or event.human_present is not False
                    or event.cast_members != capture.native_present
                    or tuple(row) != _values(event, capture)
                    or self.store.conn.execute('SELECT 1 FROM realm_fact_origin WHERE source=? LIMIT 1',
                                               (source.id,)).fetchone() is not None):
                return Rejected('proof_changed')
        except ValueError:
            return Rejected('proof_changed')
        return PrivateKillRecord(event, capture)

    def revalidate_in_unit(self, record: PrivateKillRecord):
        self._require_unit()
        if type(record) is not PrivateKillRecord or type(record.event) is not RealmKill or type(record.capture) is not NativeKillCapture:
            return Rejected('invalid_input')
        try:
            native_kill_capture_to_json(record.capture)
            if RealmKill(**{f: getattr(record.event, f) for f in record.event.__dataclass_fields__}) != record.event:
                return Rejected('invalid_input')
        except (ValueError, TypeError, AttributeError):
            return Rejected('invalid_input')
        current = self.get_in_unit(record.event.kill_id)
        if type(current) is Rejected or current != record:
            return Rejected('proof_changed')
        return current
