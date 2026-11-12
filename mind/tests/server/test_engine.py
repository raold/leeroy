import asyncio
from dataclasses import replace
import json
import random
from types import SimpleNamespace

import pytest

from conftest import run
from selfies.fakes import mind_app
from leeroy_mind.discord import store as dstore
from leeroy_mind.server import engine as se, records, runtime
from leeroy_mind.server.seeds import Seed
from leeroy_mind.talk.raid import RaidPhase


@pytest.fixture
def app(tmp_path, monkeypatch):
    app = mind_app(tmp_path, toml='[server]\nenabled=true\n[guards]\nenabled=true\n')
    app.server.lease._held = True
    app.engine._login_at = float('-inf')
    app.world.human_online = False
    app.server.engine.rng = random.Random(19)
    observed = run(runtime.snapshot(app, app.clock.now()))
    observed.seeds = [Seed('test:bread', 'purpose', ('general',), 'Baking bread in the kitchen')]
    async def snapshot(*args, **kwargs):
        await asyncio.sleep(0)
        return observed
    monkeypatch.setattr(runtime, 'snapshot', snapshot)
    monkeypatch.setattr(se.rhythm, 'length', lambda rng:5)
    monkeypatch.setattr(se, 'answer_delay', lambda state,rng:0)
    # Late replies have their own explicit cancellation test below.
    app.server.engine.rng.random = lambda:.5
    app.observed, app.turns = observed, []
    async def speak(turn, meta):
        app.turns.append((turn,meta))
        meta.turn_id = 'test-'+str(len(app.turns))
        with app.store.transaction():
            dstore.post(app.store,channel=turn.channel.removeprefix('discord:'),author=turn.speaker,
                origin='mind',text='An invented thought '+str(len(app.turns)),turn=meta.turn_id,
                reply_to=meta.discord.reply_to,now=app.clock.now())
            records.posted(app.store,app.clock.local().date().isoformat(),app.clock.now(),
                           conversation=meta.server_conversation)
        return ['An invented thought']
    monkeypatch.setattr(app.engine, 'speak_meta', speak)
    yield app
    run(app.stop())


async def finished(engine):
    for _ in range(100):
        if not engine.live():
            return
        await asyncio.sleep(0)
    pytest.fail('conversation did not end')


def test_a_conversation_runs_and_replies_to_messages(app):
    async def go():
        await app.server.engine.tick(app.clock.now())
        assert len(app.server.engine.live())==1
        await finished(app.server.engine)
    run(go())
    rows = app.store.conn.execute('SELECT id,author,reply_to FROM discord_message ORDER BY id').fetchall()
    assert len(rows)==5 and rows[0]['reply_to'] is None
    assert all(r['reply_to'] in {x['id'] for x in rows[:i]} for i,r in enumerate(rows) if i)
    assert all(a['author']!=b['author'] for a,b in zip(rows,rows[1:]))
    assert all(t.priority==se.Priority.BACKGROUND and t.max_lines==1 for t,m in app.turns)
    assert records.counts(app.store,app.clock.local().date().isoformat())['conversations']==1


@pytest.mark.parametrize('reason',['lease','window','raid','login','guards','off'])
def test_no_start_without_readiness(app,monkeypatch,reason):
    e = app.server.engine
    if reason=='lease':
        app.server.lease._held = False
    elif reason=='window':
        monkeypatch.setattr(app.clock,'local',lambda:app.clock._start.replace(hour=4))
    elif reason=='raid':
        monkeypatch.setattr(app.engine.raid,'phase',lambda now:RaidPhase.COMBAT)
    elif reason=='login':
        app.engine._login_at = app.clock.now()
    elif reason=='guards':
        app.engine.live_review.config = replace(app.engine.live_review.config,enabled=False)
    else:
        e.config = replace(e.config,enabled=False)
    run(e.tick(app.clock.now()))
    assert not e.live() and not app.turns


def test_members_in_play_are_never_cast(app):
    app.observed.in_play = frozenset({101,102,103})
    async def go():
        await app.server.engine.tick(app.clock.now())
        await finished(app.server.engine)
    run(go())
    assert app.turns
    assert all(t.speaker not in app.observed.in_play for t,m in app.turns)


def test_his_post_cancels_before_the_task_starts_and_frees_the_slot(app):
    async def go():
        await app.server.engine.tick(app.clock.now())
        app.server.engine.his_post('general')
        await finished(app.server.engine)
        assert not app.turns
        assert json.loads(app.store.get_meta('server:recent'))[-1]['reason']=='his_post'
    run(go())


def test_his_post_ends_a_pending_conversation_and_d1_is_still_called(app,monkeypatch):
    entered = asyncio.Event()
    async def held(turn,meta):
        entered.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(app.engine,'speak_meta',held)
    queued=[]
    monkeypatch.setattr(app.engine,'_queue',lambda items:queued.extend(items))
    monkeypatch.setattr(app.discord_talk,'_take_lease',lambda:None)
    async def go():
        await app.server.engine.tick(app.clock.now())
        await asyncio.wait_for(entered.wait(),2)
        mid=dstore.post(app.store,channel='general',author=app.human_guid,origin='human',
                       text='What are you making?',now=app.clock.now())
        app.discord_talk.on_his_post(mid)
        await finished(app.server.engine)
    run(go())
    assert queued and all(m.discord.reply_to for t,m in queued)


def test_a_deleted_parent_ends_the_conversation(app,monkeypatch):
    original=app.engine.speak_meta
    async def post_and_delete(turn,meta):
        result=await original(turn,meta)
        # Retain the id in the conversation before deleting on the following snapshot.
        return result
    calls=0
    async def snapshot(*args,**kwargs):
        nonlocal calls
        calls+=1
        if calls==3:
            app.store.conn.execute('UPDATE discord_message SET deleted=?',(app.clock.now(),))
        return app.observed
    monkeypatch.setattr(runtime,'snapshot',snapshot)
    monkeypatch.setattr(app.engine,'speak_meta',post_and_delete)
    async def go():
        await app.server.engine.tick(app.clock.now())
        await finished(app.server.engine)
    run(go())
    assert len(app.turns)==1
    assert json.loads(app.store.get_meta('server:recent'))[-1]['reason']=='deleted_parent'


def test_dry_turns_end_it_and_keep_the_seed_reserved(app,monkeypatch):
    calls=[]
    async def dry(turn,meta):
        meta.turn_id='dry-'+str(len(calls))
        calls.append(meta)
        return []
    monkeypatch.setattr(app.engine,'speak_meta',dry)
    async def go():
        await app.server.engine.tick(app.clock.now())
        await finished(app.server.engine)
    run(go())
    assert len(calls)==2
    assert app.store.get_meta('server:seed:test:bread') is not None
    assert records.counts(app.store,app.clock.local().date().isoformat())['conversations']==0


def test_parent_deleted_during_generation_drops_the_pending_reply(app,monkeypatch):
    original=app.engine.speak_meta
    async def speak(turn,meta):
        if meta.discord.reply_to is not None:
            app.store.conn.execute('UPDATE discord_message SET deleted=? WHERE id=?',
                (app.clock.now(),meta.discord.reply_to))
            assert not meta.still_valid()
            meta.turn_id='withdrawn'
            return []
        return await original(turn,meta)
    monkeypatch.setattr(app.engine,'speak_meta',speak)
    async def go():
        await app.server.engine.tick(app.clock.now())
        await finished(app.server.engine)
    run(go())
    assert len(app.turns)==1
    assert json.loads(app.store.get_meta('server:recent'))[-1]['reason']=='deleted_parent'


def test_close_joins_every_conversation(app,monkeypatch):
    entered,exited=asyncio.Event(),asyncio.Event()
    async def held(turn,meta):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            exited.set()
    monkeypatch.setattr(app.engine,'speak_meta',held)
    async def go():
        await app.server.engine.tick(app.clock.now())
        await asyncio.wait_for(entered.wait(),2)
        await app.server.engine.close()
        assert exited.is_set() and not app.server.engine.live()
    run(go())


def test_needs_guards_logs_once_per_day(app,caplog):
    app.engine.live_review.config=replace(app.engine.live_review.config,enabled=False)
    async def go():
        await app.server.engine.tick(app.clock.now())
        await app.server.engine.tick(app.clock.now()+5)
    run(go())
    assert sum(r.message=='server_needs_guards' for r in caplog.records)==1


def test_each_channel_has_only_one_conversation_and_two_total(app,monkeypatch):
    app.observed.seeds=[Seed('one','purpose',('general',),'Bread'),Seed('two','purpose',('irl',),'Tea'),
                        Seed('three','purpose',('food',),'Soup')]
    async def held(turn,meta):
        await asyncio.Event().wait()
    monkeypatch.setattr(app.engine,'speak_meta',held)
    async def go():
        for _ in range(3):
            app.server.engine.next_start=0
            await app.server.engine.tick(app.clock.now())
        rows=app.server.engine.live()
        assert len(rows)==2 and len({r['channel'] for r in rows})==2
        await app.server.engine.close()
    run(go())


def test_banter_discord_yields_while_game_leg_keeps_its_rule(app,monkeypatch):
    from leeroy_mind.banter.service import Banter
    from leeroy_mind.banter.config import BanterConfig
    # The existing service's construction is independent from feature attachment.
    service=Banter(app,BanterConfig(enabled=True))
    app.world.human_online=True
    monkeypatch.setattr(service,'owner_last',lambda c:(0,0))
    assert not service.allowed('discord:general',app.clock.now())
    assert service.allowed('guild',app.clock.now())
