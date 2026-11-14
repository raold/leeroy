import dataclasses

from conftest import make_persona
from leeroy_mind.decisions import polls
from leeroy_mind.decisions.positions import (Position, VOTE_AFTER_S, bank_positions, cast_votes,
                                              lineup_positions, loot_rule_positions, member_facts,
                                              strategy_positions, vote_at)

OFFICERS = frozenset({102, 104})


def test_positions_are_deterministic(store, clock, guild):
    now = clock.now()
    first = loot_rule_positions(store.conn, OFFICERS, guild, now=now)
    assert first == loot_rule_positions(store.conn, {104, 102}, dict(reversed(guild.items())), now=now)
    lineup = {'members': [101, 102], 'bench': [103, 105]}
    assert lineup_positions(store.conn, lineup, guild, now=now) == lineup_positions(store.conn, lineup, guild, now=now)
    request = {'guid': 104, 'at': now}
    assert bank_positions(store.conn, request, OFFICERS, guild) == bank_positions(store.conn, request, OFFICERS, guild)
    proposal = {'change': 'lust_earlier'}
    assert (strategy_positions(store.conn, '631:1:0', proposal, guild, guild)
            == strategy_positions(store.conn, '631:1:0', proposal, guild, guild))
    assert all(type(p) is Position for p in first)


def test_high_dkp_officer_prefers_dkp_and_low_attendance_prefers_council(store, clock, guild):
    facts = member_facts(store.conn, [102, 104], now=clock.now())
    assert (facts[102].balance, facts[102].place, facts[102].ranked, facts[102].top) == (340, 1, 3, True)
    assert (facts[104].attended, facts[104].raids) == (1, 3)
    assert loot_rule_positions(store.conn, OFFICERS, guild, now=clock.now()) == [
        Position(102, 'dkp', 'high_dkp'), Position(104, 'loot_council', 'low_attendance')]
    # Without the data, the persona decides: a theorycrafter wants gear priority, an agreeable
    # social officer keeps the status quo, an argumentative one wants a change
    social = {**guild, 105: make_persona(105, 'Ebbo', thinks_as='social'),
              103: make_persona(103, 'Coros', thinks_as='social',
                                agreeableness={'score': 2, 'behaviour': 'argues with plans'})}
    assert loot_rule_positions(store.conn, {101, 103, 105}, social, now=clock.now() - 30 * 86400 - 1) == [
        Position(101, 'gear_priority', 'theorycrafter'), Position(103, 'loot_council', 'wants_change'),
        Position(105, 'dkp', 'status_quo')]


def test_benched_member_argues_for_a_spot(store, clock, guild):
    positions = lineup_positions(store.conn, {'members': [101, 102, 1], 'bench': [103, 105]}, guild, now=clock.now())
    assert positions == [Position(101, 'support', 'in_lineup'), Position(102, 'support', 'in_lineup'),
                         Position(103, 'object', 'benched_twice'), Position(105, 'support', 'accepts_bench')]
    assert 1 not in {p.guid for p in positions}  # he is never part of the argument


def test_votes_follow_positions_and_presence(store, clock, guild):
    now = clock.now()
    positions = loot_rule_positions(store.conn, OFFICERS, guild, now=now)
    poll = polls.open_poll(store, channel='officer-chat', question='Loot rules?',
                           options=('dkp', 'loot_council', 'gear_priority'), voters=OFFICERS,
                           closes=now + 86400, now=now, kind='loot_rule', subject='loot', positions=positions)
    times = {g: vote_at(poll, g, now, now + 86400) for g in OFFICERS}
    assert all(now + VOTE_AFTER_S[0] <= t <= now + VOTE_AFTER_S[1] for t in times.values())
    assert times == {g: vote_at(poll, g, now, now + 86400) for g in OFFICERS}  # the same after a restart
    online = {102}
    assert cast_votes(store, now=now + VOTE_AFTER_S[0] - 1, present=lambda g: True) == 0
    later = now + VOTE_AFTER_S[1]
    assert cast_votes(store, now=later, present=online.__contains__) == 1
    online.add(104)
    assert cast_votes(store, now=later + 1, present=online.__contains__) == 1
    assert cast_votes(store, now=later + 2, present=online.__contains__) == 0
    votes = store.conn.execute('SELECT member,option,why FROM guild_poll_vote WHERE poll=? ORDER BY member',
                               (poll,)).fetchall()
    assert [tuple(v) for v in votes] == [(102, 'dkp', 'high_dkp'), (104, 'loot_council', 'low_attendance')]
    assert dataclasses.asdict(positions[0]) == {'guid': 102, 'side': 'dkp', 'why': 'high_dkp'}
