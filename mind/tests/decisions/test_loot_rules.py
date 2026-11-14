"""Loot rules by officer poll (D4 Part B): the modes, his claim, the poll's cadence, when a rule takes
effect (never inside a raid) and his undo. Invented members only; he is guid 1."""
import asyncio
from datetime import datetime, timezone
import json
from types import SimpleNamespace

import pytest

from leeroy_mind.decisions import apply, ledger, polls
from leeroy_mind.decisions.config import DecisionsConfig
from leeroy_mind.decisions.loot_rules import (MODES, POLL_S, LootRule, LootRules, award_with, current_rule,
                                              drama_since, poll_due, price_of, rule_for_raid,
                                              snapshot)
from leeroy_mind.decisions.seeds import his_loot_seeds
from leeroy_mind.decisions.service import DecisionsService
from leeroy_mind.guild.config import DkpConfig
from leeroy_mind.guild.dkp import Bidder
from leeroy_mind.guild.ports import LivePorts
from leeroy_mind.server.seeds import gather
from leeroy_mind.talk.raid import RaidPhase

DAY = 86400.0
PRICES = DkpConfig().prices
OFFICERS = frozenset({102, 104})


@pytest.mark.parametrize('mode', MODES)
def test_his_claim_always_wins(mode):
    bidders = [Bidder(101, 500, 30, True), Bidder(102, 10, 5, False), Bidder(1, 0, 40, True)]
    for prices, price in ((None, 50), ({'trinket': 75.0}, 75.0)):
        rule = LootRule(mode, prices=prices, role_first={'trinket': ('melee',)})
        result = award_with(rule, price_of(rule, 'trinket', PRICES), bidders, human_claimed=True, human_guid=1,
                            slot='trinket', roles={101: 'tank', 102: 'melee'}, shares={101: 1.0, 102: 0.2})
        assert result.guid == 1 and result.priority == -1 and result.close is False
        # Charged at that mode's price: the trinket's (a rule's own price while it sets one), none by council
        assert result.paid == (0.0 if mode == 'loot_council' else float(price))
        # The member who lost it to him is the one the mode would have given it to
        assert result.runner_up == (102 if mode == 'gear_priority' else 101)


def test_council_charges_nothing_and_records_a_zero_row():
    bidders = [Bidder(101, 900, 10, True), Bidder(102, 0, 30, False), Bidder(103, 50, None, False)]
    rule = LootRule('loot_council')
    result = award_with(rule, 50, bidders, human_claimed=False, human_guid=1, slot='other',
                        shares={101: 1.0, 102: 0.5})
    # gain x (0.5 + attendance share): 102's 30 x 1.0 beats 101's 10 x 1.5; DKP plays no part
    assert (result.guid, result.paid, result.bid, result.runner_up, result.close) == (102, 0.0, 0.0, 101, False)
    # The DKP rule would have sold it to the richest
    assert award_with(LootRule(), 50, bidders, human_claimed=False, human_guid=1).guid == 101
    # Nobody interested: no award (the uninterested 103 never takes it)
    assert award_with(rule, 50, [bidders[2]], human_claimed=False, human_guid=1) is None


def test_role_first_puts_tanks_first_on_trinkets():
    bidders = [Bidder(101, 10, 5, False), Bidder(102, 500, 16, True)]
    roles = {101: 'tank', 102: 'melee'}
    first = {'trinket': ('tank', 'healer')}
    # Gear priority puts the role first outright; a council weighs it (first of two roles x1.5):
    # on trinkets the tank's 5 x 1.5 x 1.5 beats the melee's 16 x 0.5, elsewhere it does not
    for mode in ('gear_priority', 'loot_council'):
        rule = LootRule(mode, role_first=first)
        assert award_with(rule, 50, bidders, human_claimed=False, human_guid=1, slot='trinket', roles=roles,
                          shares={101: 1.0, 102: 0.0}).guid == 101
        # Other slots keep the mode's own order: the bigger upgrade
        assert award_with(rule, 50, bidders, human_claimed=False, human_guid=1, slot='weapon_1h', roles=roles,
                          shares={101: 1.0, 102: 0.0}).guid == 102
    # The DKP rule has no role priority
    assert award_with(LootRule('dkp', role_first=first), 50, bidders, human_claimed=False, human_guid=1,
                      slot='trinket', roles=roles).guid == 102
    # Gear priority charges as DKP: the tank pays what DKP's rule asks of an uncontested bid
    gear = award_with(LootRule('gear_priority', role_first=first), 50, bidders, human_claimed=False,
                      human_guid=1, slot='trinket', roles=roles)
    assert (gear.guid, gear.paid) == (101, 10.0)


def settled_rule(store, value, now):
    ident = ledger.propose(store, kind='loot_rule', subject='loot', value=value, positions=[], now=now)
    ledger.settle(store, ident, settled_by=0, now=now)
    return ident


def test_prices_override_dkp_while_in_effect(store, clock):
    now = clock.now()
    assert current_rule(store.conn) == LootRule()
    ident = settled_rule(store, {'mode': 'dkp', 'prices': {'trinket': 120}}, now)
    assert current_rule(store.conn).prices is None  # settled is not yet in effect
    ledger.take_effect(store, ident, effect_ref={'rule': 'dkp'}, now=now)
    rule = current_rule(store.conn)
    assert (price_of(rule, 'trinket', PRICES), price_of(rule, 'weapon_2h', PRICES)) == (120.0, 80)
    assert rule.decision == ident
    # A malformed price list is ignored, never half applied
    from leeroy_mind.decisions.loot_rules import rule_from_value
    assert rule_from_value({'mode': 'dkp', 'prices': {'trinket': -1, 'other': 5}}).prices is None
    assert rule_from_value({'mode': 'nope', 'role_first': {'trinket': ['tank', 'tank']}}) == LootRule()


@pytest.fixture
def kind():
    rules = LootRules()
    apply.register(rules)
    yield rules
    apply.unregister('loot_rule')


def app_for(store, clock, guild, *, engine=None):
    members = store.cast()
    return SimpleNamespace(store=store, clock=clock, personas=guild, human_guid=1,
                           world=SimpleNamespace(discord_members=lambda: members, is_officer=lambda g: g in OFFICERS),
                           guild=SimpleNamespace(raid_night=SimpleNamespace(active=False),
                                                 offline=SimpleNamespace(active=False)),
                           engine=engine, decisions=SimpleNamespace(config=DecisionsConfig(enabled=True)))


def service(app):
    svc = DecisionsService(app, DecisionsConfig(enabled=True))
    svc._present = lambda guids, now: dict.fromkeys(guids, 'offline')  # nobody votes by themselves here
    return svc


def move(clock, ts):
    clock._t = float(ts)


def open_raid(store, raid_id, started):
    with store.transaction():
        store.conn.execute("INSERT INTO guild_raid(raid_id,started,raid_key,size,heroic,offline,lead_guid,tank_guid,"
                           "loot_guid) VALUES (?,?,'icc',10,0,0,102,101,102)", (raid_id, started))


def test_the_officers_poll_settles_a_rule_announced_to_the_guild(store, clock, guild, kind):
    app = app_for(store, clock, guild)
    svc = service(app)
    asyncio.run(svc.tick())
    [poll] = polls.open_polls(store.conn)
    assert (poll['channel'], poll['kind'], poll['subject']) == ('officer-chat', 'loot_rule', 'loot')
    assert polls.options_of(poll) == MODES and polls.voters_of(poll) == {1, 102, 104}
    assert polls.positions_of(poll) == [{'guid': 102, 'side': 'dkp', 'why': 'high_dkp'},
                                        {'guid': 104, 'side': 'loot_council', 'why': 'low_attendance'}]
    carrier = store.conn.execute('SELECT channel,author,text FROM discord_message WHERE id=?',
                                 (poll['message'],)).fetchone()
    assert (carrier['channel'], carrier['author']) == ('officer-chat', 102) and carrier['text'].startswith('Poll: ')
    assert poll['closes'] == poll['opened'] + POLL_S
    # He votes; once every officer has, the poll closes at once and its result reaches the kind
    now = clock.now()
    assert polls.vote(store, poll['id'], 1, 'loot_council', now=now)
    assert polls.vote(store, poll['id'], 102, 'dkp', why='high_dkp', now=now)
    asyncio.run(svc.tick())
    assert polls.get_poll(store.conn, poll['id'])['status'] == 'open'  # 104 has not voted yet
    assert polls.vote(store, poll['id'], 104, 'loot_council', why='low_attendance', now=now)
    asyncio.run(svc.tick())
    assert polls.get_poll(store.conn, poll['id'])['status'] == 'closed'
    asyncio.run(svc.tick())  # the next minute hands the result to the kind
    row = ledger.history(store.conn)[0]
    assert (row['kind'], row['subject'], row['settled_by'], row['poll']) == ('loot_rule', 'loot', 0, poll['id'])
    announced = store.conn.execute('SELECT channel,text FROM discord_message WHERE id=?', (row['message'],)).fetchone()
    assert announced['channel'] == 'loot-and-dkp' and 'a loot council' in announced['text']
    assert polls.get_poll(store.conn, poll['id'])['handled'] is not None
    # No raid is open: it is in effect from the next raid on
    assert row['status'] == 'in_effect' and current_rule(store.conn).mode == 'loot_council'
    assert json.loads(row['effect_ref']) == {'rule': 'loot_council'}


def test_a_tied_poll_keeps_the_rule_in_force(store, clock, guild, kind):
    app = app_for(store, clock, guild)
    svc = service(app)
    asyncio.run(svc.tick())
    [poll] = polls.open_polls(store.conn)
    polls.vote(store, poll['id'], 102, 'dkp', now=clock.now())
    polls.vote(store, poll['id'], 104, 'loot_council', now=clock.now())
    asyncio.run(svc.tick())
    asyncio.run(svc.tick())
    assert ledger.history(store.conn) == [] and current_rule(store.conn) == LootRule()
    assert polls.get_poll(store.conn, poll['id'])['handled'] is not None


def test_poll_cadence_and_an_early_poll_after_drama(store, clock, guild):
    now = clock.now()
    assert poll_due(store.conn, now=now, poll_days=14, human=1)
    poll = polls.open_poll(store, channel='officer-chat', question='Loot rules?', options=MODES, voters=OFFICERS,
                           closes=now + POLL_S, now=now, kind='loot_rule', subject='loot')
    assert not poll_due(store.conn, now=now + 20 * DAY, poll_days=14, human=1)  # one is open
    polls.close_due(store, now=now + POLL_S)
    assert not poll_due(store.conn, now=now + 13 * DAY, poll_days=14, human=1)
    # A close award since the last poll opens one early, but never within three days of it
    receipt(store, 'guild:loot:3:aa', now + DAY, winner=102, runner=104, close=True)
    assert drama_since(store.conn, now, 1) == 1
    assert not poll_due(store.conn, now=now + 2 * DAY, poll_days=14, human=1)
    assert poll_due(store.conn, now=now + 3 * DAY, poll_days=14, human=1)
    # Drama older than a week no longer counts
    assert not poll_due(store.conn, now=now + 9 * DAY, poll_days=14, human=1)
    assert poll_due(store.conn, now=now + 14 * DAY, poll_days=14, human=1)
    assert polls.get_poll(store.conn, poll)['status'] == 'closed'


def receipt(store, key, ts, *, winner, runner, close=False, claimed=False, council=False, bid=40.0, wanted=True):
    record = {'state': 'charged', 'item_id': 50001, 'name': 'Testblade', 'human_claimed': claimed,
              'bidders': [{'guid': runner, 'gain': 12, 'wanted': wanted}],
              'award': {'guid': winner, 'paid': 0.0 if council else 50.0, 'bid': 50.0, 'priority': -1 if claimed else 0,
                        'runner_up': runner, 'runner_up_bid': None if council else bid, 'close': close}}
    with store.transaction():
        store.set_meta(key, json.dumps(record))
        return store.conn.execute("INSERT INTO dkp_ledger(ts,guid,delta,reason,raid_id,item_id,note) "
                                  "VALUES (?,?,?,'item',3,50001,?)",
                                  (ts, winner, 0.0 if council else -50.0, ('council:' if council else '') + key)).lastrowid


def test_rule_waits_for_the_next_raid(store, clock, guild, kind):
    now = clock.now()
    open_raid(store, 10, now)
    assert snapshot(store.conn, 10) == LootRule()                         # raid 10 opened under DKP
    settled = settled_rule(store, {'mode': 'loot_council'}, now)          # a poll closed mid-raid
    asyncio.run(service(app_for(store, clock, guild)).tick())
    assert ledger.get(store.conn, settled)['status'] == 'settled'        # waits: raid 10 is open
    assert current_rule(store.conn) == LootRule() and rule_for_raid(store.conn, 10) == LootRule()
    with store.transaction():
        store.conn.execute('UPDATE guild_raid SET ended=? WHERE raid_id=10', (now + 3600,))
    # His raid's fight, wipe silence or debrief hold it too, with no raid record open
    for phase in (RaidPhase.COMBAT, RaidPhase.WIPE_SILENCE, RaidPhase.WIPE_DEBRIEF):
        engine = SimpleNamespace(raid=SimpleNamespace(phase=lambda now, p=phase: p), _in_boss_fight=lambda now: False)
        asyncio.run(service(app_for(store, clock, guild, engine=engine)).tick())
        assert ledger.get(store.conn, settled)['status'] == 'settled'
    forming = app_for(store, clock, guild)
    forming.guild.raid_night.active = True
    asyncio.run(service(forming).tick())
    assert ledger.get(store.conn, settled)['status'] == 'settled'
    asyncio.run(service(app_for(store, clock, guild)).tick())
    assert ledger.get(store.conn, settled)['status'] == 'in_effect'
    assert rule_for_raid(store.conn, 10) == LootRule()                    # raid 10 kept its rule
    open_raid(store, 11, now + 7200)
    assert snapshot(store.conn, 11).mode == 'loot_council'                # the next raid runs under it


def test_undo_restores_the_previous_rule(store, clock, guild, kind):
    now = clock.now()
    first = settled_rule(store, {'mode': 'dkp', 'prices': {'trinket': 90}}, now)
    ledger.take_effect(store, first, effect_ref={'rule': 'dkp'}, now=now)
    second = settled_rule(store, {'mode': 'gear_priority', 'role_first': {'trinket': ['tank']}}, now + 1)
    ledger.take_effect(store, second, effect_ref={'rule': 'gear_priority'}, now=now + 2)
    assert current_rule(store.conn).mode == 'gear_priority'
    assert apply.reversible(ledger.get(store.conn, second))
    reply = asyncio.run(service(app_for(store, clock, guild)).handle({'action': 'overrule', 'id': second}))
    assert reply['ok'] and reply['undone'] and 'back in effect' in reply['message']
    rule = current_rule(store.conn)
    assert (rule.mode, rule.decision, dict(rule.prices)) == ('dkp', first, {'trinket': 90.0})
    row = ledger.get(store.conn, second)
    assert (row['status'], row['overruled_by_owner']) == ('overruled', 1)


def test_his_claim_seeds_drama(store, clock, guild):
    now = clock.now()
    names = {1: 'Testmage', 104: 'Dunna', 102: 'Belra'}
    lid = receipt(store, 'guild:loot:3:bb', now - 3600, winner=1, runner=104, claimed=True)
    [seed] = his_loot_seeds(store.conn, now=now, names=names, human=1, cast=guild)
    # Hers to grumble about, and about him: who talks passes culture's teasing gates for him
    assert (seed.key, seed.kind, seed.channels, seed.about) == (f'his_loot:{lid}', 'his_loot', ('loot-and-dkp',),
                                                                (104, 1))
    assert seed.fact == 'Testmage claimed [Item 50001] in the raid over Dunna, who wanted it and bid 40 DKP'
    assert drama_since(store.conn, now - DAY, 1) == 1
    # A council's zero row seeds too, without a bid
    council = receipt(store, 'guild:loot:3:cc', now - 60, winner=1, runner=102, claimed=True, council=True)
    seeds = his_loot_seeds(store.conn, now=now, names=names, human=1, cast=guild)
    assert [s.key for s in seeds] == [f'his_loot:{council}', f'his_loot:{lid}']
    assert seeds[0].fact.endswith('over Belra, who wanted it')
    # In the gathered seeds his claim replaces the plain loot line; with decisions off nothing changes
    local = datetime.fromtimestamp(now, timezone.utc)
    args = dict(now=now, local=local, cast=guild, officers=OFFICERS, human=1, human_name='', trial_rank=4)
    on = {s.key for s in gather(store.conn, decisions_enabled=True, **args)}
    off = {s.key for s in gather(store.conn, **args)}
    assert {f'his_loot:{lid}', f'his_loot:{council}'} <= on and not {f'loot:{lid}', f'loot:{council}'} & on
    assert {f'loot:{lid}', f'loot:{council}'} <= off and not any(k.startswith('his_loot') for k in off)
    # At most one a member a local day: her newest
    receipt(store, 'guild:loot:3:dd', now - 7200, winner=1, runner=104, claimed=True)
    assert [s.key for s in his_loot_seeds(store.conn, now=now, names=names, human=1, cast=guild)] == [
        f'his_loot:{council}', f'his_loot:{lid}']
    # Older than a week: no seed
    move(clock, now + 8 * DAY)
    assert his_loot_seeds(store.conn, now=clock.now(), names=names, human=1, cast=guild) == []


def test_live_ports_fix_the_rule_when_a_raid_opens_and_only_with_decisions_on(store, clock):
    ident = settled_rule(store, {'mode': 'loot_council'}, clock.now())
    ledger.take_effect(store, ident, effect_ref={'rule': 'loot_council'}, now=clock.now())
    off = LivePorts(SimpleNamespace(clock=clock, store=store))
    assert off.loot_rule(5) is None and rule_for_raid(store.conn, 5) is None
    on = LivePorts(SimpleNamespace(clock=clock, store=store, decisions=object()))
    assert on.loot_rule(5).mode == 'loot_council'
    with store.conn:
        on.fix_loot_rule(6)
    assert rule_for_raid(store.conn, 6).decision == ident


def test_no_poll_opens_in_the_servers_quiet_window(store, clock, guild, kind):
    from datetime import time
    app = app_for(store, clock, guild)
    app.server = SimpleNamespace(config=SimpleNamespace(quiet_window=(time(19, 0), time(21, 0))))
    asyncio.run(service(app).tick())
    assert polls.open_polls(store.conn) == []
    app.server = SimpleNamespace(config=SimpleNamespace(quiet_window=(time(3, 0), time(8, 0))))
    asyncio.run(service(app).tick())
    assert len(polls.open_polls(store.conn)) == 1


def test_she_wanted_it_only_when_she_bid_wanting_it(store, clock, guild):
    now = clock.now()
    lid = receipt(store, 'guild:loot:3:ee', now - 60, winner=1, runner=104, claimed=True, wanted=False)
    [seed] = his_loot_seeds(store.conn, now=now, names={1: 'Testmage', 104: 'Dunna'}, human=1, cast=guild)
    assert seed.key == f'his_loot:{lid}'
    assert seed.fact == 'Testmage claimed [Item 50001] in the raid over Dunna, who could use it and bid 40 DKP'


def test_his_claim_grumbling_passes_the_teasing_gates():
    from leeroy_mind.server.rhythm import choose_cast
    from leeroy_mind.server.seeds import Seed
    from conftest import make_persona
    import random
    personas = {g: make_persona(g, f'N{g}') for g in (101, 102, 103)}
    states = {g: SimpleNamespace(presence=SimpleNamespace(state='online')) for g in personas}
    seed = Seed('his_loot:9', 'his_loot', ('loot-and-dkp',), 'He claimed it', (101, 1))
    ask = []

    def tease(teaser, target):
        ask.append((teaser, target))
        return teaser != 102
    cast = choose_cast('loot-and-dkp', seed, states, personas, officers=frozenset(), human=1,
                       in_play=frozenset(), ties=lambda a, b: 1.0, rng=random.Random(4), tease=tease)
    assert cast and 102 not in cast and all(target == 1 for _, target in ask)
    assert not choose_cast('loot-and-dkp', seed, states, personas, officers=frozenset(), human=1,
                           in_play=frozenset(), ties=lambda a, b: 1.0, rng=random.Random(4),
                           tease=lambda a, b: False)


def test_the_result_post_waits_out_the_quiet_window(store, clock, guild, kind):
    from datetime import time
    app = app_for(store, clock, guild)
    svc = service(app)
    asyncio.run(svc.tick())
    [poll] = polls.open_polls(store.conn)
    for member, option in ((1, 'loot_council'), (102, 'dkp'), (104, 'loot_council')):
        polls.vote(store, poll['id'], member, option, now=clock.now())
    asyncio.run(svc.tick())
    app.server = SimpleNamespace(config=SimpleNamespace(quiet_window=(time(19, 0), time(21, 0))))
    asyncio.run(svc.tick())
    # The result is the kind's at once (settled, the poll handled); only its post waits
    [row] = ledger.history(store.conn)
    assert (row['status'], row['message']) == ('in_effect', None)
    assert polls.get_poll(store.conn, poll['id'])['handled'] is not None
    assert store.conn.execute("SELECT count(*) FROM discord_message WHERE channel='loot-and-dkp'").fetchone()[0] == 0
    for _ in range(6):  # well past the hook's retry limit: nothing is lost
        asyncio.run(svc.tick())
    app.server = None
    asyncio.run(svc.tick())
    row = ledger.get(store.conn, row['id'])
    post = store.conn.execute('SELECT channel,author,text FROM discord_message WHERE id=?', (row['message'],)).fetchone()
    assert (post['channel'], post['author']) == ('loot-and-dkp', 102) and 'a loot council' in post['text']
    asyncio.run(svc.tick())
    assert store.conn.execute("SELECT count(*) FROM discord_message WHERE channel='loot-and-dkp'").fetchone()[0] == 1


def test_a_poll_that_keeps_the_rule_in_force_makes_no_new_decision(store, clock, guild, kind):
    app = app_for(store, clock, guild)
    svc = service(app)
    for round_ in range(2):
        asyncio.run(svc.tick())
        [poll] = polls.open_polls(store.conn)
        for member in (102, 104):
            polls.vote(store, poll['id'], member, 'dkp', now=clock.now())
        asyncio.run(svc.tick())
        asyncio.run(svc.tick())
        assert ledger.history(store.conn) == [] and current_rule(store.conn) == LootRule()
        assert polls.get_poll(store.conn, poll['id'])['handled'] is not None
        move(clock, clock.now() + 15 * DAY)
        with store.transaction():  # the guild keeps raiding, so polls keep their cadence
            store.conn.execute("INSERT INTO guild_raid(started,ended,raid_key,size,heroic,offline,lead_guid,"
                               "tank_guid,loot_guid) VALUES (?,?,'icc',10,0,0,102,101,102)",
                               (clock.now() - DAY, clock.now() - DAY + 3600))
    posts = [r[0] for r in store.conn.execute("SELECT text FROM discord_message WHERE channel='loot-and-dkp'")]
    assert posts == ['Loot rules stay: DKP.', 'Loot rules stay: DKP.']
    # A council in force voted again stays too, with its role priorities
    ident = settled_rule(store, {'mode': 'loot_council'}, clock.now())
    ledger.take_effect(store, ident, effect_ref={'rule': 'loot_council'}, now=clock.now())
    asyncio.run(svc.tick())
    [poll] = polls.open_polls(store.conn)
    for member in (102, 104):
        polls.vote(store, poll['id'], member, 'loot_council', now=clock.now())
    asyncio.run(svc.tick())
    asyncio.run(svc.tick())
    assert [r['id'] for r in ledger.history(store.conn)] == [ident]
    assert ledger.get(store.conn, ident)['status'] == 'in_effect'
