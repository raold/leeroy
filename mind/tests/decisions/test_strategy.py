import asyncio
import dataclasses
import json
from types import SimpleNamespace as NS

from conftest import make_persona
from leeroy_mind.decisions import ledger, strategy
from leeroy_mind.training import store as training_store
from tests.training.test_tg_store import context, record


def wipes(store, clock):
    for i in range(2):
        at = int((clock.now()-5000+i*500) * 1000)
        rec = record(f"{at}-77-0", outcome="wiped")
        rec.header = dataclasses.replace(rec.header, start_ms=at, end_ms=at+90000)
        training_store.put_pull(store.conn, rec, context(raid_id=1, lead_guid=102, covariates={"healers": 1}),
                                {101: "tank"}, received=at/1000 + 90)


def test_two_wipes_propose_one_menu_change(store, guild, clock):
    assert strategy.propose_strategy(store.conn, "631:0:0", now=clock.now()) is None
    wipes(store, clock)
    result = strategy.propose_strategy(store.conn, "631:0:0", now=clock.now())
    assert result["change"] in strategy.MENU and len(result["levers"]) == 1


def test_strategy_waits_out_the_fight(store, guild, clock):
    wipes(store, clock)
    value = strategy.propose_strategy(store.conn, "631:0:0", now=clock.now())
    ident = ledger.propose(store, kind="strategy", subject="strategy:631:0:0", value=value, positions=[], now=clock.now())
    ledger.settle(store, ident, settled_by=102, now=clock.now())
    app = NS(store=store, clock=clock, human_guid=1, world=NS(members=store.cast()),
             engine=NS(raid=NS(phase=lambda _: "combat"), _in_boss_fight=lambda _: True))
    assert strategy.layer_for(app, "631:0:0") is None
    assert ledger.get(store.conn, ident)["status"] == "settled"
    app.engine = NS(raid=None)
    assert strategy.layer_for(app, "631:0:0")[0] == ident
    strategy.played(app, ident)
    assert ledger.get(store.conn, ident)["status"] == "in_effect"


def test_training_off_is_talk_only(store, guild, clock):
    wipes(store, clock)
    app = NS(store=store, clock=clock, human_guid=1, world=NS(members=store.cast()), personas=NS(get=guild.get),
             training=None, engine=NS(raid=None))
    rules = strategy.strategy_rules()
    asyncio.run(rules.tick(app, clock.now()))
    asyncio.run(rules.tick(app, clock.now()+3*3600))
    assert ledger.history(store.conn)[0]["status"] == "talk_only"


def test_large_guild_strategy_is_proposed_and_settled_for_the_actual_raiders(store, guild, clock):
    wipes(store, clock)
    cast = store.cast()
    people = dict(guild)
    for guid in range(1000, 1151):
        cast[guid] = dataclasses.replace(cast[101], guid=guid, name='Synthetic')
        people[guid] = make_persona(guid, 'Synthetic')
    app = NS(store=store, clock=clock, human_guid=1, world=NS(members=cast),
             personas=NS(get=people.get), training=None, engine=NS(raid=None))
    rules = strategy.strategy_rules()
    asyncio.run(rules.tick(app, clock.now()))
    rows = ledger.history(store.conn)
    assert len(rows) == 1 and rows[0]['status'] == 'proposed'
    assert {p['guid'] for p in json.loads(rows[0]['positions'])} == {101, 102}
    assert len(rows[0]['positions']) <= ledger.MAX_JSON
    asyncio.run(rules.tick(app, clock.now() + 3 * 3600))
    assert ledger.history(store.conn)[0]['status'] == 'talk_only'
    assert len(ledger.history(store.conn)) == 1


def test_stale_wipe_proposal_expires_without_a_new_wipe(store,guild,clock):
    from leeroy_mind.decisions.seeds import decision_seeds
    wipes(store,clock)
    now = clock.now()
    proposal = strategy.propose_strategy(store.conn,"631:0:0",now=now)
    ident = ledger.propose(store,kind="strategy",subject="strategy:631:0:0",value=proposal,positions=[],now=now)
    clock._t += strategy.WEEK+1
    assert not any(s.key == f"decision:{ident}:proposed" for s in
                   decision_seeds(store.conn,now=clock.now(),personas=guild,officers={102}))
    app = NS(store=store,clock=clock,world=NS(members={}))
    asyncio.run(strategy.strategy_rules().tick(app,clock.now()))
    assert ledger.get(store.conn,ident)["status"] == "expired"
