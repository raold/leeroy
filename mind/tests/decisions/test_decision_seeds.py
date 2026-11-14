from datetime import datetime, timezone

from leeroy_mind.decisions import ledger, polls
from leeroy_mind.decisions.positions import lineup_positions, loot_rule_positions
from leeroy_mind.decisions.seeds import decision_seeds
from leeroy_mind.discord import layout
from leeroy_mind.server import rhythm
from leeroy_mind.server.seeds import gather

OFFICERS = frozenset({102, 104})


def test_seeds_carry_data_not_text(store, clock, guild):
    now = clock.now()
    with store.transaction():
        store.conn.execute("INSERT INTO discord_message(channel,author,origin,text,ts) "
                           "VALUES ('raid-planning',103,'mind','UNSEEN_MODEL_WORDS',?)", (now - 60,))
    lineup = {'members': [101, 102], 'bench': [103]}
    ledger.propose(store, kind='lineup', subject='raid:9', value=lineup,
                   positions=lineup_positions(store.conn, lineup, guild, now=now), now=now)
    bank = ledger.propose(store, kind='bank', subject='bank:5', value={'guid': 105, 'kind': 'raid_flasks', 'count': 1},
                          positions=[], now=now)
    ledger.settle(store, bank, settled_by=104, now=now)
    poll = polls.open_poll(store, channel='officer-chat', question='Loot rules for next week?',
                           options=('dkp', 'loot_council'), voters=OFFICERS, closes=now + 86400, now=now,
                           kind='loot_rule', subject='loot',
                           positions=loot_rule_positions(store.conn, OFFICERS, guild, now=now))
    seeds = {s.key: s for s in decision_seeds(store.conn, now=now, personas=guild, officers=OFFICERS)}
    assert set(seeds) == {f'decision:{bank - 1}:proposed', f'decision:{bank}:settled', f'decision:p{poll}:poll'}
    proposed, settled, voted = (seeds[k] for k in sorted(seeds))
    assert proposed.kind == 'decision' and proposed.channels == ('raid-planning',) and not proposed.officers_only
    assert 'Raid lineup proposed, raid night #9: 2 in, 1 on the bench' in proposed.fact
    assert 'Coros for object (benched 2 of the last 3)' in proposed.fact
    assert set(proposed.about) == {101, 102, 103}
    assert settled.officers_only and settled.channels == ('bank-requests',)
    assert settled.fact == 'Bank request settled by Dunna: raid_flasks x1 for Ebbo'
    assert voted.officers_only and voted.channels == ('officer-chat',)
    assert 'Belra for dkp (DKP 340, rank 1 of 3)' in voted.fact
    assert 'Dunna for loot council (1 of 3 raids in 30 days)' in voted.fact
    assert not any('UNSEEN_MODEL_WORDS' in s.fact for s in seeds.values())
    # Officers' seeds never reach a public channel, and the stakeholders weigh in the cast
    local = datetime(2026, 10, 29, 20, tzinfo=timezone.utc)
    for seed in (settled, voted):
        assert all(layout.channel(c).officers_only for c in seed.channels)
        assert rhythm.choose_seed_and_channel([seed], local=local, officers_free=0, rng=__import__('random').Random(1)) is None


def test_gather_adds_decisions_only_when_on(store, clock, guild):
    now = clock.now()
    ledger.propose(store, kind='rank', subject='rank:105', value={'rank': 4}, positions=[], now=now)
    args = dict(now=now, local=datetime(2026, 10, 29, 20, tzinfo=timezone.utc), cast=guild,
                officers=OFFICERS, human=1, human_name='', trial_rank=4)
    assert not [s for s in gather(store.conn, **args) if s.kind == 'decision']
    seeds = [s for s in gather(store.conn, decisions_enabled=True, **args) if s.kind == 'decision']
    assert [(s.key, s.channels, s.officers_only) for s in seeds] == [
        ('decision:1:proposed', ('applications', 'officer-chat'), True)]


def test_each_stage_is_argued_once(store, clock, guild):
    from leeroy_mind.server.seeds import fresh, mark_used
    now = clock.now()
    ledger.propose(store, kind='lineup', subject='raid:9', value={'members': [101], 'bench': [103]},
                   positions=[{'guid': 103, 'side': 'object', 'why': 'benched_twice'}], now=now)
    [seed] = decision_seeds(store.conn, now=now, personas=guild, officers=OFFICERS)
    assert fresh(store.conn, [seed], now=now) == [seed]
    mark_used(store, seed, now=now)
    later = now + 30 * 86400  # still proposed a month on: not argued again
    assert fresh(store.conn, decision_seeds(store.conn, now=later, personas=guild, officers=OFFICERS), now=later) == []


def test_a_polled_proposal_never_seeds_publicly_before_its_poll(store, clock, guild):
    now = clock.now()
    positions = [{'guid': 101, 'side': 'gear_priority', 'why': 'theorycrafter'}]
    ledger.propose(store, kind='loot_rule', subject='loot', value={'mode': 'gear_priority'},
                   positions=positions, now=now)
    [seed] = decision_seeds(store.conn, now=now, personas=guild, officers=OFFICERS)
    assert seed.officers_only and seed.channels == ('officer-chat',) and 101 not in seed.about
    public = polls.open_poll(store, channel='loot-and-dkp', question='Loot rules?', options=('dkp', 'gear_priority'),
                             voters=(101, 102), closes=now + 3600, now=now, kind='loot_rule', subject='loot:open')
    ledger.propose(store, kind='loot_rule', subject='loot:open', value={'mode': 'gear_priority'},
                   positions=positions, now=now, poll=public)
    seeds = {s.key: s for s in decision_seeds(store.conn, now=now, personas=guild, officers=OFFICERS)}
    assert not seeds['decision:2:proposed'].officers_only
    assert seeds['decision:2:proposed'].channels == ('loot-and-dkp',)


def test_the_daily_sweep_keeps_a_decision_stages_receipt(store, clock, guild):
    from datetime import date
    from leeroy_mind.server import records
    from leeroy_mind.server.seeds import fresh, mark_used
    now = clock.now()
    ledger.propose(store, kind='lineup', subject='raid:9', value={'members': [101], 'bench': [103]},
                   positions=[{'guid': 103, 'side': 'object', 'why': 'benched_twice'}], now=now)
    [seed] = decision_seeds(store.conn, now=now, personas=guild, officers=OFFICERS)
    mark_used(store, seed, now=now)
    store.set_meta('server:seed:life:1', str(now))
    later = now + 8 * 86400  # past the sweep's seven days, still proposed
    records.prune(store, date.fromtimestamp(later).isoformat(), later)
    assert store.get_meta('server:seed:life:1') is None  # other receipts still go
    assert fresh(store.conn, decision_seeds(store.conn, now=later, personas=guild, officers=OFFICERS), now=later) == []
