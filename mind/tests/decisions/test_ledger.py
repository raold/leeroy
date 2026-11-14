import json
import sqlite3

import pytest

from leeroy_mind.decisions import ledger
from leeroy_mind.decisions.ledger import DecisionError
from leeroy_mind.decisions.positions import Position


def propose(store, now, subject='raid:7', **kw):
    return ledger.propose(store, kind=kw.pop('kind', 'lineup'), subject=subject,
                          value=kw.pop('value', {'members': [101, 102], 'bench': [103]}),
                          positions=kw.pop('positions', [Position(103, 'object', 'benched_twice')]), now=now, **kw)


def in_force(store, kind, subject):
    row = ledger.current(store.conn, kind, subject)
    return row['id'] if row is not None else None


def test_one_live_decision_per_kind_and_subject(store, clock):
    now = clock.now()
    first = propose(store, now)
    other = propose(store, now, subject='raid:8')
    assert ledger.live(store.conn, 'lineup', 'raid:7')['id'] == first
    for status in ('proposed', 'settled'):  # one pending per kind and subject
        with pytest.raises(sqlite3.IntegrityError):
            store.conn.execute("INSERT INTO guild_decision(kind,subject,value,status,proposed,settled) "
                               "VALUES ('lineup','raid:7','{}',?,1,1)", (status,))
    store.conn.rollback()
    ledger.settle(store, first, settled_by=102, now=now + 1)
    ledger.take_effect(store, first, effect_ref={'raid': 44}, now=now + 2)
    with pytest.raises(sqlite3.IntegrityError):  # and one in effect
        store.conn.execute("INSERT INTO guild_decision(kind,subject,value,status,proposed,settled,effect) "
                           "VALUES ('lineup','raid:7','{}','in_effect',1,1,1)")
    store.conn.rollback()
    assert [r['id'] for r in ledger.in_effect(store.conn, 'lineup')] == [first]
    assert ledger.live(store.conn, 'lineup', 'raid:8')['id'] == other
    ledger.overrule(store, first, now=now + 3)
    assert ledger.live(store.conn, 'lineup', 'raid:7') is None
    row = ledger.get(store.conn, first)
    assert (row['status'], row['overruled_by_owner'], row['ended']) == ('overruled', 1, now + 3)
    assert json.loads(row['positions']) == [{'guid': 103, 'side': 'object', 'why': 'benched_twice'}]
    assert [r['id'] for r in ledger.history(store.conn)] == [other, first]


def test_supersede_is_atomic(store, clock):
    now = clock.now()
    first = propose(store, now)
    second = propose(store, now + 5, value={'members': [101], 'bench': [102, 103]})
    old, new = ledger.get(store.conn, first), ledger.get(store.conn, second)
    assert (old['status'], old['ended']) == ('superseded', now + 5)
    assert (new['status'], new['supersedes']) == ('proposed', None)  # it replaced no rule in force
    with store.transaction():
        store.conn.execute("CREATE TEMP TRIGGER no_insert BEFORE INSERT ON guild_decision "
                           "BEGIN SELECT RAISE(ABORT, 'refused'); END")
    with pytest.raises(sqlite3.IntegrityError):
        propose(store, now + 10)
    assert ledger.get(store.conn, second)['status'] == 'proposed'  # both writes or neither
    with store.transaction():
        store.conn.execute('DROP TRIGGER temp.no_insert')
    # take_effect ends the rule in force and puts its successor in effect together, or neither
    ledger.settle(store, second, settled_by=102, now=now + 11)
    ledger.take_effect(store, second, effect_ref={}, now=now + 12)
    third = propose(store, now + 13)
    ledger.settle(store, third, settled_by=102, now=now + 13)
    with store.transaction():
        store.conn.execute("CREATE TEMP TRIGGER no_effect BEFORE UPDATE OF effect ON guild_decision "
                           "WHEN NEW.effect IS NOT NULL BEGIN SELECT RAISE(ABORT, 'refused'); END")
    with pytest.raises(sqlite3.IntegrityError):
        ledger.take_effect(store, third, effect_ref={}, now=now + 14)
    assert in_force(store, 'lineup', 'raid:7') == second
    assert ledger.get(store.conn, second)['ended'] is None and ledger.get(store.conn, third)['status'] == 'settled'


def test_a_settled_loot_rule_waits_for_the_next_raid_while_the_old_one_stays_in_effect(store, clock):
    now = clock.now()
    dkp = ledger.propose(store, kind='loot_rule', subject='loot', value={'mode': 'dkp'}, positions=[], now=now)
    ledger.settle(store, dkp, settled_by=0, now=now)
    ledger.take_effect(store, dkp, effect_ref={}, now=now)
    council = ledger.propose(store, kind='loot_rule', subject='loot', value={'mode': 'loot_council'},
                             positions=[], now=now + 10)
    ledger.settle(store, council, settled_by=0, now=now + 20)  # the poll closed mid-raid
    assert [r['id'] for r in ledger.in_effect(store.conn, 'loot_rule')] == [dkp]
    assert ledger.live(store.conn, 'loot_rule', 'loot')['id'] == council
    ledger.take_effect(store, council, effect_ref={'raid': 8}, now=now + 30)  # the next raid
    assert in_force(store, 'loot_rule', 'loot') == council
    assert ledger.get(store.conn, council)['supersedes'] == dkp
    assert (ledger.get(store.conn, dkp)['status'], ledger.get(store.conn, dkp)['ended']) == ('superseded', now + 30)
    # His undo of the new rule puts the previous one back in effect
    assert ledger.overrule(store, council, now=now + 40, restore_previous=True) == dkp
    assert in_force(store, 'loot_rule', 'loot') == dkp and ledger.get(store.conn, dkp)['ended'] is None


def test_overruling_a_pending_successor_keeps_the_rule_in_force(store, clock):
    now = clock.now()
    old = ledger.propose(store, kind='loot_rule', subject='loot', value={'mode': 'dkp'}, positions=[], now=now)
    ledger.settle(store, old, settled_by=0, now=now)
    ledger.take_effect(store, old, effect_ref={}, now=now)
    for end in ('overrule', 'expire', 'talk_only'):
        waiting = ledger.propose(store, kind='loot_rule', subject='loot', value={'mode': 'gear_priority'},
                                 positions=[], now=now + 1)
        ledger.settle(store, waiting, settled_by=0, now=now + 2)
        {'overrule': lambda: ledger.overrule(store, waiting, now=now + 3, restore_previous=True),
         'expire': lambda: ledger.expire(store, waiting, now=now + 3),
         'talk_only': lambda: ledger.talk_only(store, waiting, reason='no_raid', now=now + 3)}[end]()
        assert in_force(store, 'loot_rule', 'loot') == old  # the status quo stands
    assert ledger.restore(store, waiting, now=now + 4) is None  # nothing it replaced


@pytest.mark.parametrize('kind,subject,value', [('rank', 'rank:105', {'rank': 3}),
                                                 ('strategy', 'strategy:631:9:0', {'change': 'lust_earlier'})])
def test_a_rank_or_strategy_proposal_keeps_the_standing_overlay_until_it_takes_effect(store, clock, kind, subject, value):
    now = clock.now()
    standing = ledger.propose(store, kind=kind, subject=subject, value=value, positions=[], now=now)
    ledger.settle(store, standing, settled_by=102, now=now)
    ledger.take_effect(store, standing, effect_ref={}, now=now)
    nxt = ledger.propose(store, kind=kind, subject=subject, value={}, now=now + 1,
                         positions=[{'guid': 102, 'side': 'promote', 'why': 'regular_raider'}])
    assert in_force(store, kind, subject) == standing  # proposing ends nothing
    ledger.settle(store, nxt, settled_by=102, now=now + 2)
    assert in_force(store, kind, subject) == standing  # neither does settling
    ledger.take_effect(store, nxt, effect_ref={}, now=now + 3)
    assert in_force(store, kind, subject) == nxt


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
    with pytest.raises(DecisionError, match='decision_ref_invalid'):  # an unknown poll
        propose(store, now, subject='raid:5', poll=99)
    for bad in ({'kind': 'gold', 'subject': 'raid:1'}, {'kind': 'lineup', 'subject': 'raid:x'},
                {'kind': 'rank', 'subject': 'rank:0'}, {'kind': 'loot_rule', 'subject': 'loot rules'}):
        with pytest.raises(DecisionError):
            ledger.propose(store, value={}, positions=[], now=now, **bad)
    with pytest.raises(DecisionError, match='positions'):
        ledger.propose(store, kind='rank', subject='rank:101', value={}, now=now,
                       positions=[{'guid': 101, 'side': 'Promote me!', 'why': 'x'}])
    with pytest.raises(DecisionError, match='too_long'):
        ledger.propose(store, kind='rank', subject='rank:101', value={'x': 'y' * 4000}, positions=[], now=now)
    fresh = propose(store, now, subject='raid:6')
    # The owner's flag means overruled; settled and in effect need their times
    for bad, target in (("overruled_by_owner=1", ident), ("status='in_effect'", ident), ("status='settled'", fresh)):
        with pytest.raises(sqlite3.IntegrityError):
            store.conn.execute(f"UPDATE guild_decision SET {bad} WHERE id=?", (target,))
    store.conn.rollback()
