"""Permanent memory attribution through the actual Store writes."""
from dataclasses import replace
import json
import sqlite3

import pytest

from conftest import make_mem
from leeroy_mind.realm.memory_lineage import (
    RealmLineageError, attach_realm_dependencies_in_unit, realm_dependencies_in_unit)
from test_realm_fact_stamps import make_fact
from test_realm_live_proof import roster


def links(store, mid):
    return tuple(r[0] for r in store.conn.execute(
        'SELECT fact FROM realm_memory_dependency WHERE memory=? ORDER BY fact', (mid,)))


def corrupt_link(store, memory, fact):
    """Simulate a historical FK-off write, then restore normal connection settings."""
    assert not store.in_unit
    conn = store.conn
    original = conn.autocommit
    assert conn.execute('PRAGMA foreign_keys').fetchone()[0] == 1
    try:
        conn.autocommit = True
        conn.execute('PRAGMA foreign_keys=OFF')
        assert conn.execute('PRAGMA foreign_keys').fetchone()[0] == 0
        conn.execute('INSERT INTO realm_memory_dependency(memory,fact) VALUES (?,?)', (memory, fact))
        assert conn.execute('SELECT 1 FROM realm_memory_dependency WHERE memory=? AND fact=?',
                            (memory, fact)).fetchone() is not None
    finally:
        conn.execute('PRAGMA foreign_keys=ON')
        conn.autocommit = original
    assert conn.execute('PRAGMA foreign_keys').fetchone()[0] == 1
    assert conn.autocommit == original


def test_direct_shown_child_and_keyed_old_target_are_permanent(roster):
    fact = make_fact(roster)
    ref = f'realm:fact:{fact.id}'
    direct = roster.insert_memory(make_mem(evidence=(ref, ref)))
    shown = roster.insert_memory(make_mem(evidence=(), key='key'), shown=iter([ref]))
    child = roster.insert_memory(make_mem(evidence=(f'memory:{direct}',)))
    assert links(roster, direct) == links(roster, shown) == links(roster, child) == (fact.id,)
    rewritten = roster.upsert_keyed_memory(make_mem(id=direct, evidence=(), key='key', text='new'))
    assert rewritten == shown and links(roster, shown) == (fact.id,)
    roster.merge_memory(shown, (), 7)
    roster.touch_memories([shown], 2000)
    with roster.transaction():
        roster.conn.execute("UPDATE memory SET status='archived',scope='private',superseded_by=? WHERE id=?", (child, shown))
        roster.conn.execute("UPDATE realm_fact_origin SET state='revoked' WHERE fact=?", (fact.id,))
    assert links(roster, shown) == (fact.id,)


def test_pre_hook_direct_refs_in_child_and_target_survive_rewrite(roster):
    fact = make_fact(roster)
    ref = f'realm:fact:{fact.id}'
    child = roster.insert_memory(make_mem(evidence=(), key='legacy'))
    with roster.transaction():
        roster.conn.execute('UPDATE memory SET evidence=? WHERE id=?', (json.dumps([ref]), child))
    copy = roster.insert_memory(make_mem(evidence=(f'memory:{child}',)))
    assert links(roster, copy) == (fact.id,)
    roster.upsert_keyed_memory(make_mem(evidence=(), key='legacy'))
    assert links(roster, child) == (fact.id,)


@pytest.mark.parametrize('ref,reason', [('realm:fact:01', 'invalid_input'), ('realm:other:1', 'invalid_input'),
    ('memory:0', 'invalid_input'), ('realm:fact:999999', 'missing_row'), ('x'*129, 'graph_limit')])
def test_bad_new_refs_roll_back(roster, ref, reason):
    before = roster.conn.execute('SELECT count(*) FROM memory').fetchone()[0]
    with pytest.raises(RealmLineageError) as error:
        roster.insert_memory(make_mem(evidence=('event:1', ref)))
    assert error.value.rejection.reason == reason
    assert roster.conn.execute('SELECT count(*) FROM memory').fetchone()[0] == before


def test_large_distinct_input_and_legacy_direct_marker_are_not_truncated(roster):
    with pytest.raises(RealmLineageError) as error:
        roster.insert_memory(make_mem(evidence=tuple(f'event:{n}' for n in range(1, 258))))
    assert error.value.rejection.reason == 'graph_limit'
    mid = roster.insert_memory(make_mem(evidence=(), key='legacy'))
    with roster.transaction():
        roster.conn.execute('UPDATE memory SET evidence=? WHERE id=?',
            (json.dumps(['event:1'] * 1000 + ['realm:bad']), mid))
    with pytest.raises(RealmLineageError) as error:
        roster.upsert_keyed_memory(make_mem(evidence=(), key='legacy', text='changed'))
    assert error.value.rejection.reason == 'invalid_row'
    assert roster.get_memories([mid])[0].text != 'changed'


def test_unrelated_legacy_cycles_missing_and_malformed_ancestry_remain_opaque(roster):
    mid = roster.insert_memory(make_mem(evidence=()))
    with roster.transaction():
        roster.conn.execute('UPDATE memory SET evidence=? WHERE id=?',
                            (json.dumps([f'memory:{mid}', 'memory:999999'] * 300), mid))
    copy = roster.insert_memory(make_mem(evidence=(f'memory:{mid}', 'memory:999999')))
    assert links(roster, copy) == ()
    with roster.transaction():
        roster.conn.execute("UPDATE memory SET evidence='broken' WHERE id=?", (mid,))
    assert links(roster, roster.insert_memory(make_mem(evidence=(f'memory:{mid}',)))) == ()


def test_index_and_link_failure_roll_back_all_writes(roster, monkeypatch):
    fact = make_fact(roster)
    mid = roster.insert_memory(make_mem(key='key', evidence=(f'realm:fact:{fact.id}',)))
    original = roster.get_memories([mid])[0]
    def fail(*args):
        raise sqlite3.IntegrityError('injected index failure')
    monkeypatch.setattr(roster, '_index_actors', fail)
    with pytest.raises(sqlite3.IntegrityError):
        roster.upsert_keyed_memory(replace(original, text='changed', evidence=()))
    assert roster.get_memories([mid])[0] == original and links(roster, mid) == (fact.id,)


def test_unit_required_and_attach_unions_old_links(roster):
    fact = make_fact(roster)
    mid = roster.insert_memory(make_mem(evidence=(f'realm:fact:{fact.id}',)))
    with pytest.raises(RuntimeError):
        realm_dependencies_in_unit(roster, evidence=())
    with roster.transaction():
        attach_realm_dependencies_in_unit(roster, mid, ())
        attach_realm_dependencies_in_unit(roster, mid, (fact.id, fact.id))
    assert links(roster, mid) == (fact.id,)


def test_201_fact_union_and_corrupt_link_fail_without_mutation(roster):
    with roster.transaction():
        for number in range(1, 202):
            roster.conn.execute("INSERT INTO realm_fact(id,kind,text,source,ts) VALUES (?, 'test', 'x', 'event', 1)", (number,))
    first = roster.insert_memory(make_mem(key='a', evidence=tuple(f'realm:fact:{i}' for i in range(1, 201))))
    before = roster.get_memories([first])[0]
    with pytest.raises(RealmLineageError) as error:
        roster.upsert_keyed_memory(make_mem(key='a', text='changed', evidence=('realm:fact:201',)))
    assert error.value.rejection.reason == 'graph_limit'
    assert roster.get_memories([first])[0] == before and links(roster, first) == tuple(range(1, 201))
    # A legacy database may have been written while FK enforcement was off.
    corrupt_link(roster, first, 999999)
    with pytest.raises(RealmLineageError):
        roster.upsert_keyed_memory(make_mem(key='a', text='changed', evidence=()))
    assert roster.get_memories([first])[0] == before


def test_dependency_insert_failure_restores_existing_target(roster):
    first = make_fact(roster)
    mid = roster.insert_memory(make_mem(key='key', evidence=()))
    before = roster.get_memories([mid])[0]
    with roster.transaction():
        roster.conn.execute("CREATE TRIGGER fail_lineage BEFORE INSERT ON realm_memory_dependency BEGIN SELECT RAISE(ABORT, 'injected'); END")
    with pytest.raises(sqlite3.IntegrityError):
        roster.upsert_keyed_memory(make_mem(key='key', text='changed', evidence=(f'realm:fact:{first.id}',)))
    assert roster.get_memories([mid])[0] == before and links(roster, mid) == ()


def test_vector_access_and_scope_edits_leave_links(roster):
    fact = make_fact(roster)
    mid = roster.insert_memory(make_mem(evidence=(f'realm:fact:{fact.id}',)))
    roster.set_embedding(mid, bytes(2048))
    roster.touch_memories([mid], 1234)
    with roster.transaction():
        roster.conn.execute("UPDATE memory SET scope='secret',scope_with=0 WHERE id=?", (mid,))
    assert links(roster, mid) == (fact.id,)


def test_nested_savepoint_failure_does_not_discard_caught_outer_write(roster):
    with roster.transaction():
        roster.set_meta('before', 'kept')
        with pytest.raises(RealmLineageError):
            roster.insert_memory(make_mem(evidence=('realm:fact:999999',)))
    assert roster.get_meta('before') == 'kept'


def test_missing_stored_fact_link_rejects_entire_rewrite(roster):
    mid = roster.insert_memory(make_mem(key='broken', evidence=()))
    before = roster.get_memories([mid])[0]
    corrupt_link(roster, mid, 999999)
    with pytest.raises(RealmLineageError) as error:
        roster.upsert_keyed_memory(make_mem(key='broken', text='changed', evidence=()))
    assert error.value.rejection.reason == 'missing_row'
    assert roster.get_memories([mid])[0] == before


def test_merge_link_failure_restores_evidence_importance_and_counter(roster):
    fact = make_fact(roster)
    mid = roster.insert_memory(make_mem(evidence=(), importance=2))
    before = roster.get_memories([mid])[0]
    with roster.transaction():
        roster.conn.execute("CREATE TRIGGER fail_merge BEFORE INSERT ON realm_memory_dependency BEGIN SELECT RAISE(ABORT, 'injected'); END")
    with pytest.raises(sqlite3.IntegrityError):
        roster.merge_memory(mid, (f'realm:fact:{fact.id}',), 10)
    assert roster.get_memories([mid])[0] == before and links(roster, mid) == ()


def test_distinct_bounds_count_once_without_truncating_storage(roster):
    refs = tuple(f'event:{i}' for i in range(1, 257))
    mid = roster.insert_memory(make_mem(evidence=refs + refs))
    assert roster.get_memories([mid])[0].evidence == refs + refs
    with pytest.raises(RealmLineageError) as error:
        roster.insert_memory(make_mem(evidence=refs + ('event:257',)))
    assert error.value.rejection.reason == 'graph_limit'


def test_insert_dependency_failure_leaves_no_memory_or_indexes(roster):
    fact = make_fact(roster)
    with roster.transaction():
        roster.conn.execute("CREATE TRIGGER fail_insert_lineage BEFORE INSERT ON realm_memory_dependency BEGIN SELECT RAISE(ABORT, 'injected'); END")
    with pytest.raises(sqlite3.IntegrityError):
        roster.insert_memory(make_mem(text='uniquefailureword', actors=(101,), evidence=(f'realm:fact:{fact.id}',)))
    assert roster.conn.execute('SELECT count(*) FROM memory').fetchone()[0] == 0
    assert roster.conn.execute('SELECT count(*) FROM memory_actor').fetchone()[0] == 0
    assert roster.conn.execute("SELECT count(*) FROM memory_fts WHERE memory_fts MATCH 'uniquefailureword'").fetchone()[0] == 0
    assert roster.conn.execute('SELECT count(*) FROM realm_memory_dependency').fetchone()[0] == 0


@pytest.mark.parametrize('operation', ['insert', 'upsert', 'merge'])
def test_orphan_dependency_on_missing_child_rejects_all_hooks_atomically(roster, operation):
    fact = make_fact(roster)
    target = roster.insert_memory(make_mem(key='target', text='old', actors=(101,), evidence=()))
    before = roster.get_memories([target])[0]
    corrupt_link(roster, 999999, fact.id)
    with pytest.raises(RealmLineageError) as error:
        if operation == 'insert':
            roster.insert_memory(make_mem(text='neworphanword', actors=(102,),
                                           evidence=('memory:999999',)))
        elif operation == 'upsert':
            roster.upsert_keyed_memory(make_mem(key='target', text='neworphanword', actors=(102,),
                                               evidence=('memory:999999',)))
        else:
            roster.merge_memory(target, ('memory:999999',), 10)
    assert error.value.rejection.reason == 'missing_row'
    assert roster.get_memories([target])[0] == before
    assert roster.conn.execute('SELECT count(*) FROM memory').fetchone()[0] == 1
    assert [tuple(r) for r in roster.conn.execute('SELECT memory_id,guid FROM memory_actor')] == [(target, 101)]
    assert roster.conn.execute("SELECT count(*) FROM memory_fts WHERE memory_fts MATCH 'neworphanword'").fetchone()[0] == 0
    assert [tuple(r) for r in roster.conn.execute('SELECT memory,fact FROM realm_memory_dependency')] == [(999999, fact.id)]


@pytest.mark.parametrize('where', ['evidence', 'shown'])
def test_malformed_utf8_new_ref_rejects_atomically(roster, where):
    bad = 'chat:\ud800'
    evidence = (bad,) if where == 'evidence' else ()
    shown = (bad,) if where == 'shown' else ()
    with pytest.raises(RealmLineageError) as error:
        roster.insert_memory(make_mem(text='invalidrefword', actors=(101,), evidence=evidence), shown=shown)
    assert error.value.rejection.reason == 'invalid_input'
    assert roster.conn.execute('SELECT count(*) FROM memory').fetchone()[0] == 0
    assert roster.conn.execute('SELECT count(*) FROM memory_actor').fetchone()[0] == 0
    assert roster.conn.execute("SELECT count(*) FROM memory_fts WHERE memory_fts MATCH 'invalidrefword'").fetchone()[0] == 0
    assert roster.conn.execute('SELECT count(*) FROM realm_memory_dependency').fetchone()[0] == 0
