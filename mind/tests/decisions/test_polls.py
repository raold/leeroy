import pytest

from leeroy_mind.decisions import polls
from leeroy_mind.decisions.ledger import DecisionError
from leeroy_mind.decisions.positions import Position

OFFICERS = (102, 104)


def open_loot_poll(store, now, **kw):
    store.set_meta('human_guid', '1')
    return polls.open_poll(store, channel='officer-chat', question='Loot rules for next week?',
                           options=kw.pop('options', ('dkp', 'loot_council', 'gear_priority')),
                           voters=kw.pop('voters', OFFICERS), closes=now + 86400, now=now,
                           kind='loot_rule', subject='loot', **kw)


def test_a_vote_is_final(store, clock):
    now = clock.now()
    poll = open_loot_poll(store, now)
    assert polls.vote(store, poll, 102, 'dkp', why='high_dkp', now=now + 1)
    assert not polls.vote(store, poll, 102, 'loot_council', why='wants_change', now=now + 2)
    row = store.conn.execute('SELECT option,why,ts FROM guild_poll_vote WHERE poll=? AND member=102',
                             (poll,)).fetchone()
    assert tuple(row) == ('dkp', 'high_dkp', now + 1)
    assert not polls.vote(store, poll, 103, 'dkp', now=now)       # no voter
    assert not polls.vote(store, poll, 104, 'roll', now=now)      # not an option
    assert polls.vote(store, poll, 1, 'gear_priority', now=now)   # he is a voter in every poll
    assert polls.tally(store.conn, poll) == {'dkp': 1, 'loot_council': 0, 'gear_priority': 1}


def test_tie_keeps_the_status_quo(store, clock):
    now = clock.now()
    tied, empty, won = (open_loot_poll(store, now) for _ in range(3))
    polls.vote(store, tied, 102, 'dkp', now=now)
    polls.vote(store, tied, 104, 'loot_council', now=now)
    polls.vote(store, won, 102, 'loot_council', now=now)
    polls.vote(store, won, 104, 'loot_council', now=now)
    polls.vote(store, won, 1, 'dkp', now=now)
    assert polls.close_due(store, now=now + 86400) == [(tied, None), (empty, None), (won, 'loot_council')]
    assert [polls.get_poll(store.conn, p)['result'] for p in (tied, empty, won)] == [None, None, 'loot_council']
    assert not polls.vote(store, won, 1, 'dkp', now=now + 86400)


def test_close_due_only_after_closes(store, clock):
    now = clock.now()
    poll = open_loot_poll(store, now)
    for member in (1, *OFFICERS):
        polls.vote(store, poll, member, 'dkp', now=now + 60)
    assert polls.close_due(store, now=now + 86399) == []
    assert polls.get_poll(store.conn, poll)['status'] == 'open'
    assert not polls.vote(store, poll, 102, 'dkp', now=now + 86400)  # past its closing time
    assert polls.close_due(store, now=now + 86400) == [(poll, 'dkp')]
    assert polls.close_due(store, now=now + 90000) == []


def test_a_poll_is_checked_when_it_opens(store, clock):
    now = clock.now()
    for bad in ({'options': ('dkp',)}, {'options': ('dkp', 'dkp')}, {'options': ('DKP!', 'x')},
                {'positions': [Position(103, 'dkp', 'high_dkp')]},
                {'positions': [Position(102, 'roll', 'high_dkp')]}):
        with pytest.raises(DecisionError):
            open_loot_poll(store, now, **bad)
    with pytest.raises(DecisionError, match='channel'):
        polls.open_poll(store, channel='nowhere', question='q', options=('a', 'b'), voters=(102,),
                        closes=now + 1, now=now)
    poll = open_loot_poll(store, now, positions=[Position(102, 'dkp', 'high_dkp')])
    assert polls.voters_of(polls.get_poll(store.conn, poll)) == {1, 102, 104}
    assert polls.positions_of(polls.get_poll(store.conn, poll)) == [{'guid': 102, 'side': 'dkp', 'why': 'high_dkp'}]


def test_the_database_keeps_a_vote_final(store, clock):
    import sqlite3
    now = clock.now()
    poll = open_loot_poll(store, now)
    assert polls.vote(store, poll, 102, 'dkp', why='high_dkp', now=now)
    for sql in ("UPDATE guild_poll_vote SET option='loot_council'", 'DELETE FROM guild_poll_vote'):
        with pytest.raises(sqlite3.IntegrityError, match='a vote is final'):
            store.conn.execute(sql)
    store.conn.rollback()
    assert polls.tally(store.conn, poll)['dkp'] == 1


def test_a_poll_names_its_post_once(store, clock):
    now = clock.now()
    poll = open_loot_poll(store, now)
    with store.transaction():
        posts = [store.conn.execute("INSERT INTO discord_message(channel,author,origin,text,ts) "
                                    "VALUES ('officer-chat',102,'mind','vote',?)", (now,)).lastrowid for _ in range(2)]
    assert polls.attach_message(store, poll, posts[0]) and not polls.attach_message(store, poll, posts[1])
    assert polls.get_poll(store.conn, poll)['message'] == posts[0]
