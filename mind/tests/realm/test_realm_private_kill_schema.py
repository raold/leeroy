"""Disposable v11 upgrades and the private-capture SQL boundary."""
import sqlite3

import pytest

from conftest import seed_test_cast
from leeroy_mind.realm.fact_store import FactStore
from leeroy_mind.realm.kill_retention import PrivateKillStore
from leeroy_mind.realm.records import FactRow, Rejected
from leeroy_mind.storage import db
from leeroy_mind.storage.store import Store
from test_realm_foundation_schema import _insert, _kill, _source
from test_realm_live_proof import HUMAN, insert, observe, roster
from test_realm_private_kill import POLICY, PRIVATE


def old_store(tmp_path, monkeypatch, clock):
    files = db._migration_files()
    with monkeypatch.context() as patch:
        patch.setattr(db, '_migration_files', lambda: [(n, p) for n, p in files if n <= 11])
        store = Store(db.open_db(tmp_path / 'v11.db'), clock=clock)
    return store, files


def seed_public_and_tombstone(store):
    seed_test_cast(store)
    with store.transaction():
        insert(store, 'cast_member', guid=10, name='Testmaster', race=2, **{'class': 1},
               gender=0, level=80, rank=0, online=1, updated=1000.0)
        insert(store, 'realm_guild', id=50, name='Own', role='progression', faction='horde', added=1000.0)
        public_obs = observe(store, POLICY)
    fact = FactStore(store).record(public_obs, human=HUMAN)
    assert type(fact) is FactRow
    with store.transaction():
        insert(store, 'game_event', id=99, ts=1000.0, type='guard', payload='{}')
        _insert(store.conn, 'realm_source', _source(99))
        _insert(store.conn, 'realm_kill', _kill(kill_id='b' * 32 + ':2', native_boot='b' * 32, source=99))
        store.conn.execute("INSERT INTO realm_notable VALUES ('2026-01-01',20,999999)")
    return public_obs, fact


_ORDER = {'game_event': 'id', 'realm_source': 'id', 'realm_fact': 'id',
          'realm_fact_origin': 'fact', 'realm_knows': 'fact,guild',
          'realm_notable': 'day,guid', 'realm_kill': 'kill_id'}


def rows(conn):
    return {table: [tuple(r) for r in conn.execute(f'SELECT * FROM {table} ORDER BY {order}')]
            for table, order in _ORDER.items()}


def constraints(conn):
    result = {}
    for table in ('game_event', 'realm_source', 'realm_fact', 'realm_fact_origin', 'realm_knows', 'realm_kill'):
        foreign_keys = [tuple(row) for row in conn.execute(f'PRAGMA foreign_key_list({table})')]
        indexes = []
        for index in conn.execute(f'PRAGMA index_list({table})'):
            indexes.append((tuple(index), tuple(tuple(row) for row in conn.execute(f'PRAGMA index_info({index[1]})'))))
        result[table] = (foreign_keys, sorted(indexes))
    return result


def test_v11_real_public_and_old_tombstone_survive_upgrade(tmp_path, monkeypatch, clock):
    store, files = old_store(tmp_path, monkeypatch, clock)
    try:
        public_obs, fact = seed_public_and_tombstone(store)
        before = rows(store.conn)
        before_kills = before['realm_kill']
        schema_constraints = constraints(store.conn)
        assert before['realm_fact_origin'] and before['realm_knows']
        assert all(fk[5:7] == ('NO ACTION', 'NO ACTION')
                   for fks, _ in schema_constraints.values() for fk in fks)
        assert db.migrate(store.conn) == 12
        assert db.migrate(store.conn) == 12
        after = rows(store.conn)
        assert [r[:-1] for r in after.pop('realm_kill')] == before.pop('realm_kill')
        assert after == before
        assert all(r['qualification_json'] is None for r in store.conn.execute('SELECT * FROM realm_kill'))
        assert constraints(store.conn) == schema_constraints
        assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()
        assert FactStore(store).record(public_obs, human=HUMAN) == fact
        with store.transaction():
            assert type(PrivateKillStore(store).get_in_unit('b' * 32 + ':2')) is Rejected
        store.close()
        store = Store(db.open_db(tmp_path / 'v11.db'), clock=clock)
        assert db.migrate(store.conn) == 12
        assert constraints(store.conn) == schema_constraints
        reopened = rows(store.conn)
        assert [r[:-1] for r in reopened.pop('realm_kill')] == before_kills
        assert reopened == before
        assert FactStore(store).record(public_obs, human=HUMAN) == fact
        with pytest.raises(sqlite3.IntegrityError), store.transaction():
            store.conn.execute("UPDATE realm_kill SET qualification_json='{}' WHERE fact=?", (fact.id,))
    finally:
        store.close()


def test_failure_after_alter_rolls_back_populated_schema_and_version(tmp_path, monkeypatch, clock):
    store, files = old_store(tmp_path, monkeypatch, clock)
    try:
        public_obs, fact = seed_public_and_tombstone(store)
        before = rows(store.conn)
        schema_constraints = constraints(store.conn)
        schema = [tuple(row) for row in store.conn.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name")]
        migration = next(path for version, path in files if version == 12)
        broken = tmp_path / '0012_realm_private_kill_capture.sql'
        broken.write_text(migration.read_text(encoding='utf-8') + '\nCREATE TABLE realm_kill(x);', encoding='utf-8')
        with monkeypatch.context() as patch:
            patch.setattr(db, '_migration_files', lambda: [(n, p) for n, p in files if n <= 11] + [(12, broken)])
            with pytest.raises(sqlite3.OperationalError):
                db.migrate(store.conn)
        assert store.conn.execute('PRAGMA user_version').fetchone()[0] == 11
        assert 'qualification_json' not in {r['name'] for r in store.conn.execute('PRAGMA table_info(realm_kill)')}
        assert rows(store.conn) == before
        assert constraints(store.conn) == schema_constraints
        assert [tuple(row) for row in store.conn.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name")] == schema
        assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()
        assert FactStore(store).record(public_obs, human=HUMAN) == fact
        assert db.migrate(store.conn) == 12
    finally:
        store.close()


@pytest.mark.parametrize('value', ['[]', '{bad}', '{ "x":1}', '{"x":"' + 'é' * 4096 + '"}', b'{}'])
def test_capture_check_rejects_bad_json(roster, value):
    with roster.transaction():
        obs = observe(roster, POLICY, payload=PRIVATE)
    record = PrivateKillStore(roster).record(obs, human=HUMAN)
    with pytest.raises(sqlite3.IntegrityError), roster.transaction():
        roster.conn.execute('UPDATE realm_kill SET qualification_json=? WHERE kill_id=?', (value, record.event.kill_id))
