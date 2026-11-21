"""Part C's kinds (rank reviews, boss strategy, bank requests) each need their own [decisions]
switch, default off; rank reviews open at most POLLS_PER_TICK polls a tick and POLLS_PER_DAY a
local day; looking for bank requests reads the bank at most every DISCOVER_EVERY_S."""
import asyncio
from dataclasses import replace
from datetime import datetime, timedelta
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from leeroy_mind.decisions import bank, ledger, polls, ranks
from leeroy_mind.decisions.attach import attach_decisions
from leeroy_mind.decisions.config import DecisionsConfig, load_decisions_config
from leeroy_mind.decisions.service import DecisionsService

PART_C = {'rank_reviews': 'rank', 'boss_strategy': 'strategy', 'bank_requests': 'bank'}
RANKS = {"officer": 1, "raider": 2, "social": 3}


def write(tmp_path, extra=''):
    path = tmp_path / 'mind.toml'
    path.write_text('[server]\nenabled = true\n[decisions]\nenabled = true\n' + extra, encoding='utf-8')
    return path


def test_part_c_switches_default_off_and_load(tmp_path):
    cfg = load_decisions_config(write(tmp_path))
    assert (cfg.rank_reviews, cfg.boss_strategy, cfg.bank_requests) == (False, False, False)
    assert [k for k in ('lineup', 'loot_rule', 'bank', 'strategy', 'rank') if cfg.kind_on(k)] == ['lineup', 'loot_rule']
    for switch, kind in PART_C.items():
        cfg = load_decisions_config(write(tmp_path, f'{switch} = true\n'))
        assert getattr(cfg, switch) is True and cfg.kind_on(kind)
        assert not replace(cfg, enabled=False).kind_on(kind)
        # Invalid is off: the whole table, as any invalid [decisions] key
        assert load_decisions_config(write(tmp_path, f'{switch} = 1\n')) == DecisionsConfig()


def test_service_runs_only_the_kinds_switched_on(store, clock):
    def service(**switches):
        return DecisionsService(NS(store=store, clock=clock, human_guid=1),
                                DecisionsConfig(enabled=True, **switches))
    assert service().kinds_on == {'lineup', 'loot_rule'}
    assert service(rank_reviews=True, bank_requests=True).kinds_on == {'lineup', 'loot_rule', 'rank', 'bank'}

    off, on = service(), service(**{s: True for s in PART_C})
    for svc in (off, on):
        svc.calls = []
        for kind in ('lineup', 'loot_rule', 'bank', 'strategy', 'rank'):
            async def tick(app, now, kind=kind, svc=svc):
                svc.calls.append(('tick', kind))
            async def closed(app, poll, winner, kind=kind, svc=svc):
                svc.calls.append(('closed', kind))
            svc.rules[kind] = NS(kind=kind, tick=tick, closed=closed)
    now = clock.now()
    poll = polls.open_poll(store, channel='applications', question='Review?', options=('approve', 'keep'),
                           voters=(102,), closes=now + 60, now=now, kind='rank', subject='rank:105')
    polls.close_due(store, now=now + 61)
    asyncio.run(off.kinds(now))
    asyncio.run(off.deliver())
    assert off.calls == [('tick', 'lineup'), ('tick', 'loot_rule')]
    assert polls.get_poll(store.conn, poll)['handled'] is None  # an off kind's poll waits
    asyncio.run(on.kinds(now))
    asyncio.run(on.deliver())
    assert {k for c, k in on.calls if c == 'tick'} == {'lineup', 'loot_rule', 'bank', 'strategy', 'rank'}
    assert ('closed', 'rank') in on.calls and polls.get_poll(store.conn, poll)['handled'] is not None


def app_for(tmp_path, training=None):
    handlers, hooks, background = [], [], []
    return NS(data_dir=tmp_path, server=object(), engine=NS(), training=training,
              register_background=background.append, register_event_handler=lambda n, h: handlers.append(n),
              register_connection_hook=hooks.append, handlers=handlers, night_runner=None)


def training():
    listener = NS(guild_layer=None, guild_played=None, guild_hold=lambda: False)
    return NS(listener=listener, applier=NS(guild_allowed=None, guild_hold=None))


def test_attach_wires_training_and_the_cast_only_for_their_switches(tmp_path):
    write(tmp_path)
    off = app_for(tmp_path, training())
    assert attach_decisions(off) is not None
    assert off.handlers == ['hello'] and off.training.listener.guild_layer is None
    assert off.training.applier.guild_hold is None and off.training.listener.guild_hold() is False
    write(tmp_path, 'rank_reviews = true\nboss_strategy = true\n')
    on = app_for(tmp_path, training())
    assert attach_decisions(on) is not None
    assert on.handlers == ['hello', 'cast'] and callable(on.training.listener.guild_layer)
    assert off.engine.decisions_kinds == {'lineup', 'loot_rule'}
    assert on.engine.decisions_kinds == {'lineup', 'loot_rule', 'rank', 'strategy'}
    assert on.training.applier.guild_hold is on.training.listener.guild_hold


def test_seeds_leave_out_the_kinds_switched_off(store, guild, clock):
    from leeroy_mind.decisions.seeds import decision_seeds
    now = clock.now()
    ledger.propose(store, kind='strategy', subject='strategy:631:11:3', value={'context': '631:11:3', 'wipes': [],
                   'change': 'lust_earlier', 'levers': {'lust_at': 85}, 'lead': 102}, positions=[], now=now)
    polls.open_poll(store, channel='applications', question='Review?', options=('approve', 'keep'),
                    voters=(102,), closes=now + 3600, now=now, kind='rank', subject='rank:105')
    officers = frozenset({102})
    every = {s.key for s in decision_seeds(store.conn, now=now, personas=guild, officers=officers)}
    assert any(k.endswith(':proposed') for k in every) and any(k.endswith(':poll') for k in every)
    off = decision_seeds(store.conn, now=now, personas=guild, officers=officers, kinds=frozenset({'lineup', 'loot_rule'}))
    assert off == []


def rank_app(store, guild, clock, cast):
    app = NS(store=store, human_guid=1, clock=clock, personas=guild, world=NS(members=cast),
             guild=NS(cfg=NS(ranks=RANKS), roster=NS(_ranks_due=False)), engine=NS(raid=None))
    rules = ranks.rank_rules()
    rules.seen = True
    return app, rules


def test_rank_reviews_open_at_most_two_a_tick_and_four_a_day(store, guild, clock):
    # Twelve raiders with no raid in 30 days and one trial with four: the trial's promotion first
    base = store.cast()
    template = base[103]
    cast = {g: replace(m, rank=1 if g == 102 else 2) for g, m in base.items()}
    for g in range(200, 212):
        cast[g] = replace(template, guid=g, name=f'Raider{g}', rank=2)
        guild[g] = replace(guild[103], guid=g)
    cast[104] = replace(cast[104], rank=4)
    now = clock.now()
    for i in range(4):
        store.conn.execute("INSERT INTO guild_raid VALUES (?,?,?,'icc',10,0,0,102,101,102,1,0,NULL)",
                           (50 + i, now - 3600, now - 60))
        store.conn.execute("INSERT INTO guild_attendance VALUES (?,104,'raided')", (50 + i,))
    ranks.observe_cast(store, cast, now=now - 30 * 86400)
    app, rules = rank_app(store, guild, clock, cast)
    due = rules.due(app, cast, now)
    assert len(due) >= 12
    day = datetime.fromtimestamp(now).astimezone().date()
    # Start the day's first tick just after local midnight, so all the ticks fall on one date
    start = datetime.combine(day + timedelta(days=1), datetime.min.time()).astimezone().timestamp() + 60

    def opened():
        return [r['subject'] for r in store.conn.execute("SELECT subject FROM guild_decision WHERE kind='rank' "
                                                         "ORDER BY id")]
    clock._t = start
    asyncio.run(rules.tick(app, clock.now()))
    assert len(opened()) == ranks.POLLS_PER_TICK == 2
    assert opened()[0] == 'rank:104'  # the promotion before the demotions
    for _ in range(5):
        clock._t += 60
        asyncio.run(rules.tick(app, clock.now()))
    assert len(opened()) == ranks.POLLS_PER_DAY == 4
    assert store.conn.execute("SELECT count(*) FROM guild_poll WHERE kind='rank'").fetchone()[0] == 4
    clock._t = start + 86400
    asyncio.run(rules.tick(app, clock.now()))
    assert len(opened()) == 6


def bank_app(store, guild, clock):
    cast = {g: replace(m, rank=1 if g == 101 else 3) for g, m in store.cast().items()}
    guild[101] = replace(guild[101], play=replace(guild[101].play, officer_title="Bank"))
    rules = bank.bank_rules()
    rules.features = frozenset()
    app = NS(store=store, clock=clock, human_guid=1, world=NS(members=cast), personas=guild, engine=NS(raid=None),
             guild=NS(raid_night=NS(active=False), offline=NS(active=False)),
             decisions=NS(rules_for=lambda _: rules, config=DecisionsConfig(enabled=True, bank_requests=True)))
    return app, rules


def test_bank_discovery_reads_the_bank_at_most_every_fifteen_minutes(store, guild, clock, monkeypatch):
    app, rules = bank_app(store, guild, clock)
    read = AsyncMock(return_value=None)
    monkeypatch.setattr(bank, 'read', read)
    start = clock.now()
    for minute in range(15):
        clock._t = start + 60 * minute
        asyncio.run(rules.tick(app, clock.now()))
    assert read.await_count == 1
    clock._t = start + bank.DISCOVER_EVERY_S
    asyncio.run(rules.tick(app, clock.now()))
    assert read.await_count == 2


def test_bank_requests_off_never_reads_the_bank(store, guild, clock, monkeypatch):
    app, rules = bank_app(store, guild, clock)
    read = AsyncMock(return_value=None)
    monkeypatch.setattr(bank, 'read', read)
    service = DecisionsService(app, DecisionsConfig(enabled=True))
    service.rules['bank'] = rules
    for minute in range(20):
        clock._t += 60
        asyncio.run(service.kinds(clock.now()))
    read.assert_not_awaited()
