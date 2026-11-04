"""Private kill retention through real captured receipts and Store transactions."""
from dataclasses import asdict, replace
import json
import sqlite3

import pytest

from leeroy_mind.realm import kill_retention as module
from leeroy_mind.realm.fact_store import FactStore
from leeroy_mind.realm.kill_capture import (NativeKillCapture, native_kill_capture_from_observation,
    native_kill_capture_sha256, native_kill_capture_to_json)
from leeroy_mind.realm.kill_retention import PrivateKillRecord, PrivateKillRejected, PrivateKillStore
from leeroy_mind.realm.records import FactRow, Rejected
from leeroy_mind.realm.source_store import claim_native_kill_source_in_unit
from leeroy_mind.storage.store import Store
from test_realm_live_proof import HUMAN, changed_observation, insert, observe, roster

POLICY = 'verified_raid_kill_v1'
PRIVATE = dict(human_present=False, fight_started_at_ms=0, fight_ms=-1, cast_members=[101, 102, 103, 104])
TABLES = ('realm_source', 'realm_fact', 'realm_fact_origin', 'realm_knows', 'realm_kill')


def observation(store, **kwargs):
    with store.transaction():
        return observe(store, POLICY, **kwargs)


def counts(store):
    return tuple(store.conn.execute(f'SELECT count(*) FROM {table}').fetchone()[0] for table in TABLES)


@pytest.mark.parametrize('cast', [[201], [101], [101, 102], [101, 102, 103], [101, 102, 103, 104]])
def test_first_capture_and_exact_replay_are_private(roster, cast):
    obs = observation(roster, payload={**PRIVATE, 'cast_members': cast})
    reducer = PrivateKillStore(roster)
    first = reducer.record(obs, human=HUMAN)
    assert type(first) is PrivateKillRecord
    assert first.event == obs.event and first.capture.native_present == tuple(cast)
    assert first.capture.qualified_cast == tuple(x for x in cast if x in obs.qualification.cast)
    assert counts(roster) == (1, 0, 0, 0, 1)
    replay = observation(roster, payload={**PRIVATE, 'cast_members': cast}, backlog=True, received=1011.0, captured=1011.0)
    assert reducer.record(replay, human=HUMAN) == first
    with roster.transaction():
        assert reducer.get_in_unit(obs.event.kill_id) == first
        assert reducer.revalidate_in_unit(first) == first
        row = roster.conn.execute('SELECT * FROM realm_kill').fetchone()
        assert row['fact'] is None and row['qualification_json'] is not None
        assert row['source'] == first.capture.source.id
    assert type(FactStore(roster).record(replay, human=HUMAN)) is Rejected
    assert counts(roster) == (1, 0, 0, 0, 1)


def test_first_public_and_fresh_qualifying_inputs_cannot_be_private(roster):
    obs = observation(roster)
    assert type(PrivateKillStore(roster).record(obs, human=HUMAN)) is Rejected
    assert counts(roster) == (0, 0, 0, 0, 0)
    assert type(FactStore(roster).record(obs, human=HUMAN)) is FactRow
    assert type(PrivateKillStore(roster).record(obs, human=HUMAN)) is Rejected
    assert counts(roster) == (1, 1, 1, 2, 1)


def test_five_qualified_and_first_backlog_are_refused(roster):
    obs = observation(roster, payload={**PRIVATE, 'cast_members': [101, 102, 103, 104, 105]})
    assert type(PrivateKillStore(roster).record(obs, human=HUMAN)) is Rejected
    backlog = observation(roster, payload=PRIVATE, backlog=True)
    assert type(PrivateKillStore(roster).record(backlog, human=HUMAN)) is Rejected
    assert counts(roster) == (0, 0, 0, 0, 0)


def test_later_qualifying_roster_does_not_change_original_capture(roster):
    obs = observation(roster, payload={**PRIVATE, 'cast_members': [101, 102, 103, 104, 105]})
    # The captured original roster omits 105, so its first decision is private.
    with roster.transaction():
        roster.conn.execute('DELETE FROM cast_member WHERE guid=105')
        first_obs = observe(roster, POLICY, payload={**PRIVATE, 'cast_members': [101, 102, 103, 104, 105]})
    reducer = PrivateKillStore(roster)
    first = reducer.record(first_obs, human=HUMAN)
    assert type(first) is PrivateKillRecord
    assert reducer.record(obs, human=HUMAN) == first
    assert type(FactStore(roster).record(obs, human=HUMAN)) is Rejected
    assert counts(roster) == (1, 0, 0, 0, 1)


def test_same_id_native_conflict_and_source_tombstone_refuse_repair(roster):
    obs = observation(roster, payload=PRIVATE)
    reducer = PrivateKillStore(roster)
    first = reducer.record(obs, human=HUMAN)
    with roster.transaction():
        changed = changed_observation(roster, obs, POLICY, killed_at_ms=999989)
    assert type(reducer.record(changed, human=HUMAN)) is Rejected
    with roster.transaction():
        roster.conn.execute('DELETE FROM realm_kill')
    assert type(reducer.record(obs, human=HUMAN)) is Rejected
    assert counts(roster) == (1, 0, 0, 0, 0)
    assert first.capture.source.id > 0


def test_source_claim_in_same_unit_is_tombstone(roster):
    obs = observation(roster, payload=PRIVATE)
    with roster.transaction():
        source, created = claim_native_kill_source_in_unit(roster, obs, human=HUMAN)
        assert created
        with pytest.raises(PrivateKillRejected):
            PrivateKillStore(roster).record_in_unit(obs, human=HUMAN)
        assert source.id > 0
    assert counts(roster) == (1, 0, 0, 0, 0)


@pytest.mark.parametrize('table', ['realm_source', 'realm_kill'])
def test_insert_failure_rolls_back_source_and_preserves_raw(roster, table):
    obs = observation(roster, payload=PRIVATE)
    with roster.transaction():
        roster.conn.execute(f"CREATE TEMP TRIGGER fail_private BEFORE INSERT ON {table} BEGIN SELECT RAISE(ABORT,'injected'); END")
    with pytest.raises(sqlite3.IntegrityError):
        PrivateKillStore(roster).record(obs, human=HUMAN)
    assert counts(roster) == (0, 0, 0, 0, 0)
    assert roster.conn.execute('SELECT count(*) FROM game_event').fetchone()[0] == 1


def test_late_semantic_failure_and_outer_rollback(roster, monkeypatch):
    obs = observation(roster, payload=PRIVATE)
    reducer = PrivateKillStore(roster)
    with monkeypatch.context() as patch:
        patch.setattr(module, 'native_kill_capture_from_observation', lambda *a: (_ for _ in ()).throw(ValueError()))
        assert type(reducer.record(obs, human=HUMAN)) is Rejected
    assert counts(roster) == (0, 0, 0, 0, 0)
    with pytest.raises(RuntimeError):
        with roster.transaction():
            reducer.record_in_unit(obs, human=HUMAN)
            raise RuntimeError('outer')
    assert counts(roster) == (0, 0, 0, 0, 0)


def test_unit_ownership_and_corrupt_source_or_row_fail_closed(roster):
    obs = observation(roster, payload=PRIVATE)
    reducer = PrivateKillStore(roster)
    with pytest.raises(RuntimeError):
        reducer.get_in_unit(obs.event.kill_id)
    with roster.transaction():
        with pytest.raises(RuntimeError):
            PrivateKillStore(Store(roster.conn, clock=roster.clock)).record_in_unit(obs, human=HUMAN)
        with pytest.raises(RuntimeError):
            reducer.record(obs, human=HUMAN)
    record = reducer.record(obs, human=HUMAN)
    with roster.transaction():
        roster.conn.execute('UPDATE realm_source SET received_at=received_at+1')
        assert type(reducer.get_in_unit(obs.event.kill_id)) is Rejected
        assert type(reducer.revalidate_in_unit(record)) is Rejected
    assert type(reducer.record(obs, human=HUMAN)) is Rejected


@pytest.mark.parametrize('column,value', [
    ('qualification_sha256', '0' * 64), ('native_cast_json', '[101,102,103]'),
    ('qualified_cast_json', '[101]'), ('native_epoch', '30'), ('published_ms', 1000001),
    ('qualification_json', None),
])
def test_sql_projection_corruption_is_never_repaired(roster, column, value):
    obs = observation(roster, payload=PRIVATE)
    reducer = PrivateKillStore(roster)
    first = reducer.record(obs, human=HUMAN)
    with roster.transaction():
        roster.conn.execute(f'UPDATE realm_kill SET {column}=?', (value,))
        assert type(reducer.get_in_unit(obs.event.kill_id)) is Rejected
        assert type(reducer.revalidate_in_unit(first)) is Rejected
    assert type(reducer.record(obs, human=HUMAN)) is Rejected
    assert counts(roster) == (1, 0, 0, 0, 1)


def test_roster_retirement_does_not_change_historical_read(roster):
    obs = observation(roster, payload=PRIVATE)
    reducer = PrivateKillStore(roster)
    first = reducer.record(obs, human=HUMAN)
    with roster.transaction():
        roster.conn.execute("UPDATE realm_actor SET status='retired'")
        roster.conn.execute('DELETE FROM persona_core WHERE guid=101')
        assert reducer.get_in_unit(obs.event.kill_id) == first
        assert reducer.revalidate_in_unit(first) == first


def test_raw_source_payload_change_fails_retained_read(roster):
    obs = observation(roster, payload=PRIVATE)
    reducer = PrivateKillStore(roster)
    first = reducer.record(obs, human=HUMAN)
    with roster.transaction():
        roster.conn.execute("UPDATE game_event SET payload='{}' WHERE id=?", (first.capture.source.row_id,))
        assert type(reducer.get_in_unit(obs.event.kill_id)) is Rejected
    assert type(reducer.record(obs, human=HUMAN)) is Rejected


def test_late_read_failure_escapes_unit_and_rolls_back(roster, monkeypatch):
    obs = observation(roster, payload=PRIVATE)
    reducer = PrivateKillStore(roster)
    monkeypatch.setattr(reducer, 'get_in_unit', lambda kill_id: Rejected('proof_changed'))
    assert reducer.record(obs, human=HUMAN) == Rejected('proof_changed')
    assert counts(roster) == (0, 0, 0, 0, 0)


def test_savepoint_rolls_back_whole_reduction_only(roster):
    obs = observation(roster, payload=PRIVATE)
    with roster.transaction():
        roster.conn.execute("INSERT INTO realm_guild(id,role,faction,name,added) VALUES (80,'gankers','horde','X',1)")
        with pytest.raises(RuntimeError):
            with roster.transaction():
                PrivateKillStore(roster).record_in_unit(obs, human=HUMAN)
                raise RuntimeError('savepoint')
    assert counts(roster) == (0, 0, 0, 0, 0)
    assert roster.conn.execute('SELECT count(*) FROM realm_guild WHERE id=80').fetchone()[0] == 1

@pytest.mark.parametrize('changes', [
    {'native_epoch': 30}, {'guild_id': 51}, {'human_guid': 11}, {'map': 724},
    {'instance_id': 10}, {'boss_id': 1}, {'difficulty': 1, 'size': 25},
    {'difficulty': 2, 'heroic': True}, {'killed_at_ms': 999989},
    {'verified_at_ms': 999998}, {'ts': 1000001}, {'cast_members': [101, 102, 103]},
    {'human_present': True},
])
def test_immutable_native_projection_conflicts_never_change_private_first(roster, changes):
    obs = observation(roster, payload=PRIVATE)
    reducer = PrivateKillStore(roster)
    first = reducer.record(obs, human=HUMAN)
    with roster.transaction():
        changed = changed_observation(roster, obs, POLICY, **changes)
    assert type(reducer.record(changed, human=HUMAN)) is Rejected
    with roster.transaction():
        assert reducer.get_in_unit(obs.event.kill_id) == first
    assert counts(roster) == (1, 0, 0, 0, 1)


def test_imports_do_not_register_or_attach(tmp_path):
    import os
    from pathlib import Path
    import subprocess
    import sys
    root = Path(__file__).resolve().parents[2]
    probe = tmp_path / 'private_import_probe.py'
    probe.write_text('''\
import sys
sys.path.insert(0, sys.argv[1])
import leeroy_mind.protocol as protocol
before = dict(protocol._events), dict(protocol._commands)
import leeroy_mind.realm.kill_capture
import leeroy_mind.realm.kill_retention
import leeroy_mind.realm.source_store
assert before == (dict(protocol._events), dict(protocol._commands))
for name in ("leeroy_mind.realm.service", "leeroy_mind.realm.ports", "leeroy_mind.config"):
    assert name not in sys.modules, name
''', encoding='utf-8')
    data = tmp_path / 'data'
    data.mkdir()
    subprocess.run([sys.executable, '-I', str(probe), str(root)], cwd=tmp_path,
        env={**os.environ, 'LEEROY_MIND_DATA': str(data)}, timeout=10,
        check=True, capture_output=True, text=True)

def test_deferred_commit_failure_rolls_back_complete_outer_unit(roster):
    obs = observation(roster, payload=PRIVATE)
    with roster.transaction():
        roster.conn.execute('CREATE TABLE private_deferred (fact INTEGER REFERENCES realm_fact(id) '
                            'DEFERRABLE INITIALLY DEFERRED)')
    raw = tuple(roster.conn.execute('SELECT * FROM game_event WHERE id=?', (obs.receipt.row_id,)).fetchone())
    with pytest.raises(sqlite3.IntegrityError):
        with roster.transaction():
            roster.conn.execute("INSERT INTO realm_guild(id,role,faction,name,added) VALUES (80,'gankers','horde','X',1)")
            roster.conn.execute('INSERT INTO private_deferred(fact) VALUES (999999)')
            result = PrivateKillStore(roster).record_in_unit(obs, human=HUMAN)
            assert type(result) is PrivateKillRecord and counts(roster) == (1, 0, 0, 0, 1)
    assert counts(roster) == (0, 0, 0, 0, 0)
    assert roster.conn.execute('SELECT count(*) FROM private_deferred').fetchone()[0] == 0
    assert roster.conn.execute('SELECT count(*) FROM realm_guild WHERE id=80').fetchone()[0] == 0
    assert tuple(roster.conn.execute('SELECT * FROM game_event WHERE id=?', (obs.receipt.row_id,)).fetchone()) == raw


def test_real_source_bound_capture_overflow_rolls_back_claim(roster):
    class UndoProbe(Exception):
        pass

    overflow = False
    # Each actor is a real, active, captured row. The public capture still fits,
    # while the added complete source binding eventually crosses the private cap.
    for guid in range(300, 390):
        with roster.transaction():
            insert(roster, 'realm_actor', guid=guid, guild=60, name='Rivalname',
                   role='officer', added=1000.0, status='active')
            obs = observe(roster, POLICY, payload=PRIVATE)
            try:
                with roster.transaction():
                    source, created = claim_native_kill_source_in_unit(roster, obs, human=HUMAN)
                    assert created and roster.conn.execute('SELECT count(*) FROM realm_source').fetchone()[0] == 1
                    native = obs.event.cast_members
                    qualified = tuple(sorted(set(native) & set(obs.qualification.cast)))
                    snapshot, context = asdict(obs.qualification), asdict(obs.context)
                    snapshot['transport_epoch'] = str(snapshot['transport_epoch'])
                    context['connection_epoch'] = str(context['connection_epoch'])
                    body = dict(schema='realm_native_kill_retention_v1', source=asdict(source),
                                snapshot=snapshot, context=context, participants=native,
                                native_present=native, qualified_cast=qualified)
                    raw = json.dumps(body, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':'), allow_nan=False).encode('utf-8')
                    overflow = len(raw) > 8192
                    if overflow:
                        with pytest.raises(ValueError, match='^invalid private capture$'):
                            NativeKillCapture(source, obs.qualification, obs.context, native, native, qualified)
                        with pytest.raises(ValueError, match='^invalid private capture$'):
                            native_kill_capture_from_observation(obs, source)
                    else:
                        assert native_kill_capture_from_observation(obs, source).source == source
                    raise UndoProbe()
            except UndoProbe:
                pass
        if overflow:
            break
    assert overflow, 'expected a valid public snapshot whose source-bound capture exceeds 8192 bytes'
    raw = tuple(roster.conn.execute('SELECT * FROM game_event WHERE id=?', (obs.receipt.row_id,)).fetchone())
    assert PrivateKillStore(roster).record(obs, human=HUMAN) == Rejected('invalid_input')
    assert counts(roster) == (0, 0, 0, 0, 0)
    assert tuple(roster.conn.execute('SELECT * FROM game_event WHERE id=?', (obs.receipt.row_id,)).fetchone()) == raw


def test_stray_origin_rejects_without_repairing_original_private_rows(roster):
    obs = observation(roster, payload=PRIVATE)
    reducer = PrivateKillStore(roster)
    original = reducer.record(obs, human=HUMAN)
    with roster.transaction():
        fact = insert(roster, 'realm_fact', kind='kill', text='Synthetic.', source='event', ts=1000.0)
        insert(roster, 'realm_fact_origin', fact=fact,
            producer_key=f'event:{original.capture.source.source_key}:{POLICY}',
            source=original.capture.source.id, policy=POLICY, assurance='verified_event',
            proof_json='{}', qualification_json='{}', text_sha256='0' * 64, revision=1, state='active')
        before = tuple(tuple(row) for row in roster.conn.execute('SELECT * FROM realm_kill'))
        source = tuple(roster.conn.execute('SELECT * FROM realm_source').fetchone())
        assert type(reducer.get_in_unit(obs.event.kill_id)) is Rejected
        assert type(reducer.revalidate_in_unit(original)) is Rejected
    assert type(reducer.record(obs, human=HUMAN)) is Rejected
    with roster.transaction():
        assert tuple(tuple(row) for row in roster.conn.execute('SELECT * FROM realm_kill')) == before
        assert tuple(roster.conn.execute('SELECT * FROM realm_source').fetchone()) == source
        assert roster.conn.execute('SELECT count(*) FROM realm_fact_origin').fetchone()[0] == 1


def test_coherent_rewrite_is_readable_but_cannot_refresh_original_record(roster):
    obs = observation(roster, payload=PRIVATE)
    reducer = PrivateKillStore(roster)
    original = reducer.record(obs, human=HUMAN)
    new_context = replace(original.capture.context, process_id='different-valid-process')
    rewritten = replace(original.capture, context=new_context)
    with roster.transaction():
        roster.conn.execute('UPDATE realm_kill SET qualification_json=?,qualification_sha256=? WHERE kill_id=?',
            (native_kill_capture_to_json(rewritten), native_kill_capture_sha256(rewritten), obs.event.kill_id))
        current = reducer.get_in_unit(obs.event.kill_id)
        assert current == PrivateKillRecord(original.event, rewritten) and current != original
        assert reducer.revalidate_in_unit(original) == Rejected('proof_changed')
        assert reducer.revalidate_in_unit(current) == current


@pytest.mark.parametrize('mutation', ['missing_receipt', 'wrong_binding', 'disabled', 'mismatched_context'])
def test_private_entry_point_rejects_inadmissible_receipts_and_context(roster, mutation):
    obs = observation(roster, payload=PRIVATE)
    if mutation == 'missing_receipt':
        bad = replace(obs, receipt=None)
    elif mutation == 'wrong_binding':
        bad = replace(obs, receipt=replace(obs.receipt, row_sha256='0' * 64))
    elif mutation == 'disabled':
        bad = replace(obs, context=replace(obs.context, enabled=False))
    else:
        bad = replace(obs, context=replace(obs.context, guild=60))
    assert type(PrivateKillStore(roster).record(bad, human=HUMAN)) is Rejected
    assert counts(roster) == (0, 0, 0, 0, 0)


@pytest.mark.parametrize('changes', [{'fight_started_at_ms': 999000}, {'fight_ms': 2345}])
def test_invalid_private_duration_changes_same_id_rejected(roster, changes):
    obs = observation(roster, payload=PRIVATE)
    reducer = PrivateKillStore(roster)
    original = reducer.record(obs, human=HUMAN)
    with roster.transaction():
        changed = changed_observation(roster, obs, POLICY, **changes)
        before = tuple(roster.conn.execute('SELECT * FROM realm_kill').fetchone())
    assert type(reducer.record(changed, human=HUMAN)) is Rejected
    with roster.transaction():
        assert reducer.get_in_unit(obs.event.kill_id) == original
        assert tuple(roster.conn.execute('SELECT * FROM realm_kill').fetchone()) == before

@pytest.mark.parametrize('table,key', [('game_event', 'row_id'), ('realm_source', 'id')])
def test_unavailable_original_binding_rejects_without_repair(roster, table, key):
    class UndoCorruption(Exception):
        pass

    obs = observation(roster, payload=PRIVATE)
    reducer = PrivateKillStore(roster)
    original = reducer.record(obs, human=HUMAN)
    with pytest.raises(UndoCorruption):
        with roster.transaction():
            before = tuple(roster.conn.execute('SELECT * FROM realm_kill').fetchone())
            roster.conn.execute('PRAGMA defer_foreign_keys=ON')
            roster.conn.execute(f'DELETE FROM {table} WHERE id=?', (getattr(original.capture.source, key),))
            assert type(reducer.get_in_unit(obs.event.kill_id)) is Rejected
            assert type(reducer.revalidate_in_unit(original)) is Rejected
            with pytest.raises(PrivateKillRejected):
                reducer.record_in_unit(obs, human=HUMAN)
            assert tuple(roster.conn.execute('SELECT * FROM realm_kill').fetchone()) == before
            raise UndoCorruption()
    with roster.transaction():
        assert reducer.get_in_unit(obs.event.kill_id) == original