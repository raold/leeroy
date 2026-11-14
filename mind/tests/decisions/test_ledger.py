import json
import sqlite3

import pytest

from leeroy_mind.decisions import ledger
from leeroy_mind.decisions.ledger import DecisionError
from leeroy_mind.decisions.positions import Position


def propose(store, now, subject='raid:7', **kw):
    return ledger.propose(store, kind=kw.pop('kind', 'lineup'), subject=subject,
                          value=kw.pop('value', {'members': [101, 102], 'bench': [103]}),
                          positions=kw.pop('positions', [Position(103, 'object', 'benched_twice')]), now=now)


def test_one_live_decision_per_kind_and_subject(store, clock):
    now = clock.now()
    first = propose(store, now)
    other = propose(store, now, subject='raid:8')
    assert ledger.live(store.conn, 'lineup', 'raid:7')['id'] == first
    with pytest.raises(sqlite3.IntegrityError):
        store.conn.execute("INSERT INTO guild_decision(kind,subject,value,status,proposed) "
                           "VALUES ('lineup','raid:7','{}','settled',1)")
    store.conn.rollback()
    ledger.settle(store, first, settled_by=102, now=now + 1)
    ledger.take_effect(store, first, effect_ref={'raid': 44}, now=now + 2)
    assert [r['id'] for r in ledger.in_effect(store.conn, 'lineup')] == [first]
    assert ledger.live(store.conn, 'lineup', 'raid:8')['id'] == other
    # Ended decisions leave the subject free for a new live one
    ledger.overrule(store, first, now=now + 3)
    assert ledger.live(store.conn, 'lineup', 'raid:7') is None
    row = ledger.get(store.conn, first)
    assert (row['status'], row['overruled_by_owner'], row['ended']) == ('overruled', 1, now + 3)
    assert json.loads(row['positions']) == [{'guid': 103, 'side': 'object', 'why': 'benched_twice'}]
    assert [r['id'] for r in ledger.history(store.conn)] == [other, first]


def test_supersede_is_atomic(store, clock, monkeypatch):
    now = clock.now()
    first = propose(store, now)
    second = propose(store, now + 5, value={'members': [101], 'bench': [102, 103]})
    old, new = ledger.get(store.conn, first), ledger.get(store.conn, second)
    assert (old['status'], old['ended']) == ('superseded', now + 5)
    assert (new['status'], new['supersedes']) == ('proposed', first)
    # A failing insert leaves the old decision live: both writes or neither
    monkeypatch.setattr(ledger, '_json', lambda v: (_ for _ in ()).throw(DecisionError('boom'))
                        if isinstance(v, dict) and v.get('poison') else json.dumps(v))
    with pytest.raises(DecisionError):
        propose(store, now + 9, value={'poison': True})
    assert ledger.live(store.conn, 'lineup', 'raid:7')['id'] == second
    with store.transaction():
        store.conn.execute("CREATE TEMP TRIGGER no_insert BEFORE INSERT ON guild_decision "
                           "BEGIN SELECT RAISE(ABORT, 'refused'); END")
    with pytest.raises(sqlite3.IntegrityError):
        propose(store, now + 10)
    assert ledger.get(store.conn, second)['status'] == 'proposed'


def test_transitions_are_guarded(store, clock):
    now = clock.now()
    ident = propose(store, now)
    with pytest.raises(DecisionError, match='decision_state'):
        ledger.take_effect(store, ident, effect_ref={}, now=now)
    ledger.settle(store, ident, settled_by=0, poll=None, now=now)
    with pytest.raises(DecisionError, match='decision_state'):
        ledger.settle(store, ident, settled_by=0, now=now)
    ledger.talk_only(store, ident, reason='no_raid', now=now + 1)
    row = ledger.get(store.conn, ident)
    assert (row['status'], json.loads(row['effect_ref'])) == ('talk_only', {'talk_only': 'no_raid'})
    with pytest.raises(DecisionError, match='decision_state'):
        ledger.overrule(store, ident, now=now)
    for bad in ({'kind': 'gold', 'subject': 'raid:1'}, {'kind': 'lineup', 'subject': 'raid:x'},
                {'kind': 'rank', 'subject': 'rank:0'}, {'kind': 'loot_rule', 'subject': 'loot rules'}):
        with pytest.raises(DecisionError):
            ledger.propose(store, value={}, positions=[], now=now, **bad)
    with pytest.raises(DecisionError, match='positions'):
        ledger.propose(store, kind='rank', subject='rank:101', value={}, now=now,
                       positions=[{'guid': 101, 'side': 'Promote me!', 'why': 'x'}])
    with pytest.raises(DecisionError, match='too_long'):
        ledger.propose(store, kind='rank', subject='rank:101', value={'x': 'y' * 4000}, positions=[], now=now)
