"""Actual Store-created immutable source claims; no domain/publication writes.

The caller owns the transaction. A later rejection must raise inside that unit
and be mapped outside it, so a source cannot accidentally commit on its own.
"""
from __future__ import annotations

import sqlite3

from leeroy_mind.wow import BOSS_NAMES, is_raid_fight
from .protocol import RealmKill
from .records import RealmObservation

from .provenance import (_Evidence, _canonical, _event_payload, _fail,
                         _read_source, _require_unit, validate_live_binding_in_unit,
                         validate_live_observation_in_unit)
from .records import Rejected


def _retained(store, observation, policy):
    source, _, row = _read_source(store, key=observation.receipt.wire.envelope_sha256, policy=policy)
    wire, snapshot = observation.receipt.wire, observation.qualification
    if ((source.protocol_version, source.event_type, source.server_ms, source.top_keys,
         source.guild, source.human) != (wire.protocol_version, wire.event_type, wire.server_ms,
                                       wire.top_keys, snapshot.guild, snapshot.human)
            or _canonical(row['payload']) != _canonical(_event_payload(observation.event, policy))):
        _fail('proof_changed')
    return source


def claim_source_in_unit(store, observation, *, human, policy):
    """Return (first immutable source, actual INSERT outcome), or Rejected.

    Replay always checks its new receipt and the retained first binding. This
    helper never promotes origins, overwrites source time, commits, or accepts
    an unqualified first-time backlog event. Fresh participant qualification
    applies only to unseen keys; retained origins keep their original evidence.
    """
    _require_unit(store)
    result = validate_live_binding_in_unit(store, observation, human=human, policy=policy)
    if type(result) is Rejected:
        return result
    try:
        wire = observation.receipt.wire
        exists = store.conn.execute('SELECT id FROM realm_source WHERE source_key=?',
                                    (wire.envelope_sha256,)).fetchone()
        if exists is not None:
            return _retained(store, observation, policy), False
        if observation.receipt.backlog:
            _fail('invalid_input')
        result = validate_live_observation_in_unit(store, observation, human=human, policy=policy)
        if type(result) is Rejected:
            return result
        return _insert_source(store, observation, policy)
    except _Evidence as error:
        return Rejected(error.reason)


def _insert_source(store, observation, policy):
    wire = observation.receipt.wire
    snapshot = observation.qualification
    values = (wire.envelope_sha256, wire.protocol_version, wire.event_type,
        observation.receipt.row_id, observation.receipt.row_sha256, wire.server_ms,
        wire.received_at, snapshot.guild, snapshot.human,
        _canonical(list(wire.top_keys)).decode('utf-8'))
    try:
        cursor = store.conn.execute('INSERT INTO realm_source(source_key,protocol_version,event_type,'
            'game_event_id,chat_line_id,row_sha256,server_ms,received_at,guild,human,top_keys_json) '
            'VALUES (?,?,?,?,NULL,?,?,?,?,?,?)', values)
    except sqlite3.IntegrityError as error:
        # Only real source-key uniqueness contention is replay. Foreign
        # key/check/programming failures remain failures of the outer unit.
        if getattr(error, 'sqlite_errorcode', None) != sqlite3.SQLITE_CONSTRAINT_UNIQUE:
            raise
        if store.conn.execute('SELECT id FROM realm_source WHERE source_key=?',
                              (wire.envelope_sha256,)).fetchone() is None:
            raise
        return _retained(store, observation, policy), False
    source = _retained(store, observation, policy)
    if (cursor.rowcount != 1 or source.id != cursor.lastrowid or source.row_id != observation.receipt.row_id
            or source.row_sha256 != observation.receipt.row_sha256
            or source.received_at != wire.received_at):
        _fail('proof_changed')
    return source, True


def claim_native_kill_source_in_unit(store, observation, *, human):
    """Claim only a first unqualified native kill, or validate an existing key."""
    _require_unit(store)
    if type(observation) is not RealmObservation or type(observation.event) is not RealmKill:
        return Rejected('invalid_input')
    policy = 'verified_raid_kill_v1'
    result = validate_live_binding_in_unit(store, observation, human=human, policy=policy)
    if type(result) is Rejected:
        return result
    try:
        key = observation.receipt.wire.envelope_sha256
        if store.conn.execute('SELECT id FROM realm_source WHERE source_key=?', (key,)).fetchone() is not None:
            return _retained(store, observation, policy), False
        if (observation.receipt.backlog or observation.event.human_present
                or len(set(observation.event.cast_members) & set(observation.qualification.cast)) >= 5):
            _fail('invalid_input')
        return _insert_source(store, observation, policy)
    except _Evidence as error:
        return Rejected(error.reason)


def read_native_kill_source_in_unit(store, source_id):
    """Read native binding integrity only; this is not public authorization."""
    _require_unit(store)
    if type(source_id) is not int or not 1 <= source_id <= (1 << 63) - 1:
        return Rejected('invalid_input')
    try:
        source, _, row = _read_source(store, identifier=source_id, policy='verified_raid_kill_v1')
        payload = row['payload']
        if type(payload['cast_members']) is not list:
            _fail('invalid_row')
        try:
            event = RealmKill(**{**payload, 'cast_members': tuple(payload['cast_members'])})
        except (ValueError, TypeError, KeyError):
            _fail('invalid_row')
        if (_canonical(payload) != _canonical(_event_payload(event, 'verified_raid_kill_v1'))
                or (event.guild_id, event.human_guid) != (source.guild, source.human)
                or (event.map, event.boss_id) not in BOSS_NAMES
                or not is_raid_fight(event.map, event.boss_id)):
            _fail('proof_changed')
        return source, event
    except _Evidence as error:
        return Rejected(error.reason)