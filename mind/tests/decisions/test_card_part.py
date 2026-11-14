from dataclasses import replace

import pytest

from leeroy_mind.awareness import CardContext, card
from leeroy_mind.awareness.card import DECISION_LINES, PRIORITY
from leeroy_mind.decisions import ledger, polls
from leeroy_mind.decisions.positions import lineup_positions, loot_rule_positions
from leeroy_mind.text import estimate_tokens

OFFICERS = frozenset({102, 104})


def part(result):
    return '\n'.join(line for p in result.parts if p.key == 'decisions' for line in p.lines)


@pytest.fixture
def card_ctx(store, clock, guild):
    """An officers' loot poll Belra (102) voted in, a public lineup she is in and Coros (103) is
    benched from, and a settled bank request (officers' business)."""
    now = clock.now()
    with store.transaction():
        store.conn.execute("UPDATE persona_core SET json=json_set(json,'$.play.guild_role','officer') "
                           'WHERE guid IN (102,104)')
    poll = polls.open_poll(store, channel='officer-chat', question='Loot rules for next week?',
                           options=('dkp', 'loot_council'), voters=OFFICERS, closes=now + 86400, now=now - 600,
                           kind='loot_rule', subject='loot',
                           positions=loot_rule_positions(store.conn, OFFICERS, guild, now=now))
    polls.vote(store, poll, 102, 'dkp', why='high_dkp', now=now - 300)
    lineup = {'members': [101, 102], 'bench': [103]}
    ledger.propose(store, kind='lineup', subject='raid:9', value=lineup,
                   positions=lineup_positions(store.conn, lineup, guild, now=now), now=now - 200)
    bank = ledger.propose(store, kind='bank', subject='bank:5', positions=[], now=now - 100,
                          value={'guid': 102, 'kind': 'raid_flasks', 'count': 1})
    ledger.settle(store, bank, settled_by=104, now=now - 100)
    return store


def ctx(clock, channel, **kw):
    return CardContext(channel=channel, now=clock.now(), decisions=True, **kw)


def discord(clock, channel, readers=(1, 101, 102, 103, 104), **kw):
    return ctx(clock, channel, server=True, readers=readers, **kw)


def test_game_card_shows_her_vote(card_ctx, clock):
    officers = part(card(card_ctx, 102, context=ctx(clock, 'officer')))
    assert 'Open poll in #officer-chat: Loot rules for next week? (options: dkp, loot_council); you voted dkp (high_dkp)' in officers
    guild = part(card(card_ctx, 102, context=ctx(clock, 'guild')))
    assert 'Loot rules' not in guild and 'voted' not in guild  # the officers' vote stays with them
    benched = part(card(card_ctx, 103, context=ctx(clock, 'guild')))
    assert 'Raid lineup proposed: raid night #9: 2 in, 1 on the bench; you are on the bench' in benched
    assert 'your position: object (benched_twice)' in benched
    # The same vote and position reach her Discord cards
    assert 'you voted dkp' in part(card(card_ctx, 102, context=discord(clock, 'discord:officer-chat')))
    assert 'your position: object' in part(card(card_ctx, 103, context=discord(clock, 'discord:raid-planning')))
    assert 'your position: object' in part(card(card_ctx, 103, context=discord(clock, 'dm:103', to_guid=1)))


@pytest.mark.parametrize('channel,readers,shown', [
    ('officer', None, True), ('voice:officers', None, True), ('discord:officer-chat', None, True),
    ('discord:bank-requests', None, True), ('guild', None, False), ('whisper', None, False),
    ('discord:general', None, False), ('dm:102', None, False),
    ('gdm:3', (1, 102, 104), False), ('gdm:3', (1, 102, 103), False), ('gdm:3', (102, 104, 105), False)])
def test_officer_decisions_stay_with_officers(card_ctx, clock, channel, readers, shown):
    readers = readers or (1, 101, 102, 103, 104)
    context = (discord(clock, channel, readers=readers, to_guid=1 if channel.startswith('dm:') else 0)
               if channel.startswith(('discord:', 'dm:', 'gdm:')) else ctx(clock, channel, to_guid=1))
    text = part(card(card_ctx, 102, context=context))
    assert ('Bank request settled: raid_flasks x1' in text) is shown
    assert ('Loot rules' in text) is shown
    # A member who is no officer never sees officers' business, even in an officers' game room
    assert 'Bank request' not in part(card(card_ctx, 101, context=ctx(clock, 'officer')))


def test_card_part_absent_when_off(card_ctx, clock):
    for context in (ctx(clock, 'officer'), discord(clock, 'discord:officer-chat'), ctx(clock, 'guild')):
        off = card(card_ctx, 102, context=replace(context, decisions=False))
        assert 'decisions' not in {p.key for p in off.parts}
        assert 'Guild decisions' not in off.render(2000)
    on = card(card_ctx, 102, context=ctx(clock, 'officer'))
    keys = [p.key for p in on.parts]
    assert keys.index('decisions') == keys.index('standing') + 1
    assert list(PRIORITY).index('decisions') == list(PRIORITY).index('standing') + 1


def test_the_part_is_bounded_and_fits_the_budget(card_ctx, clock, guild):
    now = clock.now()
    for n in range(10, 20):
        ledger.propose(store := card_ctx, kind='rank', subject=f'rank:{102}' if n == 10 else f'rank:{n + 100}',
                       value={'rank': 2}, positions=[{'guid': 102, 'side': 'promote', 'why': 'regular_raider'}],
                       now=now - n)
    result = card(store, 102, context=ctx(clock, 'officer'))
    lines = next(p for p in result.parts if p.key == 'decisions').lines
    assert len(lines) <= DECISION_LINES and any(line.startswith('Open poll') for line in lines)
    for budget in (150, 400, 600):
        assert estimate_tokens(result.render(budget)) <= budget


def test_a_polled_proposal_keeps_its_positions_with_the_officers_until_its_poll_is_known(card_ctx, clock):
    now = clock.now()
    ledger.propose(card_ctx, kind='loot_rule', subject='loot:next', value={'mode': 'gear_priority'}, now=now,
                   positions=[{'guid': 102, 'side': 'gear_priority', 'why': 'theorycrafter'}])
    assert 'your position: gear_priority' not in part(card(card_ctx, 102, context=ctx(clock, 'guild')))
    assert 'your position: gear_priority' in part(card(card_ctx, 102, context=ctx(clock, 'officer')))


def test_a_long_summary_is_cut_not_dropped(card_ctx, clock):
    now = clock.now()
    first = {f'slot{n}': ['tank', 'healer', 'melee', 'ranged'] for n in range(12)}
    rule = ledger.propose(card_ctx, kind='loot_rule', subject='loot', value={'mode': 'dkp', 'role_first': first},
                          positions=[], poll=None, now=now)
    ledger.settle(card_ctx, rule, settled_by=0, now=now)
    line = next(line for line in part(card(card_ctx, 101, context=ctx(clock, 'guild'))).splitlines()
                if line.startswith('Loot rule'))
    assert line.startswith('Loot rule settled: mode dkp; slot0: tank>healer') and line.endswith('…')
