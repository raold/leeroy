import sqlite3
from dataclasses import replace

import pytest

from conftest import unit
from leeroy_mind.claims.extract import Candidate
from leeroy_mind.claims.merge import T_SAME, merge
from leeroy_mind.claims.store import add, expire, live_claims, nearest, reinforce, supersede
from leeroy_mind.storage.rows import pack_vec

VEC = pack_vec(unit(0))


def put(store, guid=101, **over):
    values = dict(kind='opinion', subject='saurfang', text='loves the Saurfang fight',
                  polarity=1, embedding=VEC)
    return add(store, guid, **(values | over))


def test_writes_vectors_and_history_are_atomic(store, clock):
    first = put(store, subject=' SAURFANG ')
    reinforce(store, first, now=clock.now() + 1)
    row = live_claims(store.conn, 101)[0]
    assert (row.subject, row.seen, row.first_seen, row.last_seen) == (
        'saurfang', 2, clock.now(), clock.now() + 1)
    assert nearest(store.conn, 101, VEC) == [(row, 1.0)]
    assert nearest(store.conn, 102, VEC) == []
    second = put(store, text='hates the Saurfang fight', polarity=-1)
    supersede(store, first, second)
    assert [c.id for c in live_claims(store.conn, 101)] == [second]
    assert [c.id for c, _ in nearest(store.conn, 101, VEC)] == [second]
    assert store.conn.execute('SELECT superseded_by FROM member_claim WHERE id=?', (first,)).fetchone()[0] == second
    with pytest.raises(RuntimeError), store.transaction():
        put(store, guid=102)
        raise RuntimeError('rollback')
    assert live_claims(store.conn, 102) == [] and nearest(store.conn, 102, VEC) == []


def test_plan_expiry_filters_before_cleanup_and_preserves_history(store, clock):
    at = clock.now()
    plan = put(store, kind='plan', expires=at + 1)
    fact = put(store, kind='fact', text='works at the bakery', polarity=0)
    assert len(live_claims(store.conn, 101, now=at)) == 2
    assert [c.id for c, _ in nearest(store.conn, 101, VEC, 1, now=at + 1)] == [fact]
    assert expire(store, now=at + 1) == 1
    assert expire(store, now=at + 2) == 0
    assert store.conn.execute('SELECT expires FROM member_claim WHERE id=?', (plan,)).fetchone()[0] == at + 1


def test_merge_reinforces_but_high_cosine_opposites_supersede(store, clock):
    candidate = Candidate('opinion', 'saurfang', 'loves the Saurfang fight', 1, None)
    cid, action = merge(store, 101, candidate, VEC, now=clock.now())
    assert action == 'add'
    assert merge(store, 101, candidate, VEC, now=clock.now() + 1) == (cid, 'reinforce')
    other, action = merge(store, 101, replace(candidate, text='hates the Saurfang fight', polarity=-1),
                          VEC, now=clock.now() + 2)
    assert other != cid and action == 'supersede'
    assert live_claims(store.conn, 101)[0].seen == 1
    assert merge(store, 102, candidate, VEC, now=clock.now())[1] == 'add'


def test_merge_replaced_fact_and_similarity_boundary(store, clock):
    candidate = Candidate('fact', 'her job', 'works at the bakery', 0, None)
    old, _ = merge(store, 101, candidate, VEC, now=clock.now())
    changed, action = merge(store, 101, replace(candidate, text='quit her job'), VEC, now=clock.now() + 1)
    assert action == 'supersede' and changed != old
    opinion = Candidate('opinion', 'raids', 'enjoys raids', 1, None)
    merge(store, 102, opinion, VEC, now=clock.now())
    vec = unit(0)
    vec[0], vec[1] = T_SAME - .001, (1 - (T_SAME - .001) ** 2) ** .5
    assert merge(store, 102, opinion, pack_vec(vec), now=clock.now())[1] == 'add'


@pytest.mark.parametrize('over', [dict(subject=''), dict(subject='x' * 61), dict(text='x' * 201),
                                dict(kind='rumour'), dict(polarity=2), dict(source='hearsay')])
def test_schema_constraints(store, over):
    with pytest.raises(sqlite3.IntegrityError):
        put(store, **over)


def test_nearest_on_read_only_connection(store, clock):
    from leeroy_mind.claims.cli import _load_vec
    from leeroy_mind.nightly.cli import _read_store
    put(store)
    path = store.conn.execute('PRAGMA database_list').fetchone()['file']
    with _read_store(path, clock) as reader:
        _load_vec(reader.conn)
        assert nearest(reader.conn, 101, VEC)[0][0].guid == 101
        with pytest.raises(sqlite3.OperationalError):
            reader.conn.execute('DELETE FROM member_claim')
