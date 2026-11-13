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
from leeroy_mind.server.seeds import Seed, fresh
from leeroy_mind.talk.raid import RaidPhase
from leeroy_mind.talk.turns import Turn, TurnKind


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


@pytest.mark.parametrize('kind', ['life','his_day'])
@pytest.mark.parametrize('unavailable', ['offline','in_play','away','pending','speaking','source_owned','missing'])
def test_unavailable_private_seed_opener_is_skipped_and_seed_remains_usable(app,monkeypatch,kind,unavailable):
    seed = Seed('private:1',kind,('general',),'An unshared fact',
                (101,) if kind=='life' else (app.human_guid,),witnesses=(101,))
    app.observed.seeds = [seed]
    e = app.server.engine
    states, personas = dict(app.observed.states), dict(app.observed.personas)
    pending = []
    active = set()
    monkeypatch.setattr(app.engine,'planned_turns',lambda:pending)
    monkeypatch.setattr(app.engine,'active_speakers',lambda **kwargs:active)
    if unavailable == 'offline':
        app.observed.states[101] = replace(states[101],presence=replace(states[101].presence,state='offline'))
    elif unavailable in {'in_play','away'}:
        setattr(app.observed,unavailable,frozenset({101}))
    elif unavailable == 'pending':
        pending.append(Turn(TurnKind.REPLY_HUMAN,101,'guild'))
    elif unavailable == 'speaking':
        other = se.Conversation('other','food',Seed('other','purpose',('food',),'Dinner'),
                                [101,102],app.clock.now(),5,in_flight=101)
        e._live[other.id] = other
    elif unavailable == 'source_owned':
        active.add(101)
    else:
        app.observed.personas.pop(101)
    async def go():
        await e.tick(app.clock.now())
        assert not app.turns
        assert seed in fresh(app.store.conn,[seed],now=app.clock.now())
        assert app.store.get_meta('server:seed:private:1') is None
        if unavailable == 'speaking':
            e._live.pop('other')
        assert not e.live()
        app.observed.states, app.observed.personas = states, personas
        app.observed.in_play = app.observed.away = frozenset()
        pending.clear()
        active.clear()
        e.next_start = 0
        await e.tick(app.clock.now())
        await finished(e)
    run(go())
    assert app.turns[0][0].speaker == 101


def test_owner_becomes_unavailable_before_opener_and_keeps_seed(app,monkeypatch):
    seed = Seed('life:1','life',('general',),'An unshared fact',(101,))
    app.observed.seeds = [seed]
    async def go():
        await app.server.engine.tick(app.clock.now())
        assert app.server.engine.live()
        app.observed.away = frozenset({101})
        await finished(app.server.engine)
    run(go())
    assert not app.turns
    assert fresh(app.store.conn,[seed],now=app.clock.now()) == [seed]


@pytest.mark.parametrize('kind', ['life','his_day'])
def test_dropped_private_opener_never_hands_the_fact_to_another_member(app,monkeypatch,kind):
    seed = Seed('private:1',kind,('general',),'An unshared fact',
                (101,) if kind=='life' else (app.human_guid,),witnesses=(101,))
    app.observed.seeds = [seed]
    async def dropped(turn,meta):
        app.turns.append((turn,meta))
        meta.turn_id = 'guard-dropped'
        return []
    monkeypatch.setattr(app.engine,'speak_meta',dropped)
    async def go():
        await app.server.engine.tick(app.clock.now())
        await finished(app.server.engine)
    run(go())
    assert len(app.turns) == 1 and app.turns[0][0].speaker == 101
    assert not app.store.conn.execute('SELECT id FROM discord_message').fetchall()
    assert json.loads(app.store.get_meta('server:recent'))[-1]['reason'] == 'opener_dropped'


@pytest.mark.parametrize('kind', ['life','his_day'])
def test_replies_only_receive_the_posted_thread_not_the_unshared_seed(app,kind):
    private = 'An unpublished detail that the opener may choose to omit'
    app.observed.seeds = [Seed('private:1',kind,('general',),private,
                              (101,) if kind=='life' else (app.human_guid,),witnesses=(101,))]
    async def go():
        await app.server.engine.tick(app.clock.now())
        await finished(app.server.engine)
    run(go())
    assert len(app.turns) == 5
    first, meta = app.turns[0]
    assert first.speaker == 101 and private in first.instruction and meta.topic == private
    assert any(t.speaker != 101 for t,m in app.turns[1:])
    for turn, meta in app.turns[1:]:
        assert private not in turn.instruction
        assert not meta.topic and private not in meta.server_anchor
        assert meta.discord.reply_to in meta.server_thread


@pytest.mark.parametrize('kind,expected', [
    ('purpose','Talk about this topic'),('rival','using only what is known'),
    ('officer','using only what is known'),('loot','this real event'),
])
def test_opener_distinguishes_topics_known_facts_and_events(app,kind,expected):
    app.observed.seeds = [Seed('test:1',kind,('general',),'An invented subject')]
    async def go():
        await app.server.engine.tick(app.clock.now())
        await finished(app.server.engine)
    run(go())
    instruction = app.turns[0][0].instruction
    assert expected in instruction
    if kind != 'loot':
        assert 'real event' not in instruction


def test_answer_queued_cancels_only_the_matching_in_flight_member(app,monkeypatch):
    app.observed.seeds = [Seed('one','purpose',('general',),'Bread'),
                          Seed('two','purpose',('food',),'Soup')]
    entered = asyncio.Queue()
    exited = []
    async def held(turn,meta):
        await entered.put(turn.speaker)
        try:
            await asyncio.Event().wait()
        finally:
            exited.append(turn.speaker)
    monkeypatch.setattr(app.engine,'speak_meta',held)
    async def go():
        e = app.server.engine
        await e.tick(app.clock.now())
        first = await asyncio.wait_for(entered.get(),2)
        e.next_start = 0
        await e.tick(app.clock.now())
        second = await asyncio.wait_for(entered.get(),2)
        assert first != second
        conversations = {c.in_flight:c for c in e._live.values()}
        e.answer_queued(999)
        assert all(not c.reason for c in conversations.values())
        e.answer_queued(first)
        await asyncio.gather(conversations[first].task,return_exceptions=True)
        assert exited == [first]
        assert conversations[first].reason == 'answer_queued'
        assert conversations[first].in_flight is None
        assert not conversations[second].task.done() and not conversations[second].reason
        assert len(e.live()) == 1
        await e.close()
    run(go())


def test_valid_refuses_a_planned_turn_but_excludes_its_own_server_turn(app,monkeypatch):
    own = Turn(TurnKind.OPEN,101,'discord:general')
    conversation = se.Conversation('one','general',app.observed.seeds[0],[101,102],
                                   app.clock.now(),5,in_flight=101,in_flight_turn=own)
    planned = [own,Turn(TurnKind.REPLY_HUMAN,102,'guild')]
    monkeypatch.setattr(app.engine,'planned_turns',lambda:planned)
    assert app.server.engine._valid(conversation,101)
    planned.append(Turn(TurnKind.REPLY_HUMAN,101,'guild'))
    assert not app.server.engine._valid(conversation,101)


@pytest.mark.parametrize('block', ['window','lease','combat','wipe_silence','wipe_debrief','boss_5man'])
def test_running_conversation_ends_when_a_hold_begins(app,monkeypatch,block):
    entered, exited = asyncio.Event(), asyncio.Event()
    async def held(turn,meta):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            exited.set()
    monkeypatch.setattr(app.engine,'speak_meta',held)
    async def go():
        e = app.server.engine
        await e.tick(app.clock.now())
        await asyncio.wait_for(entered.wait(),2)
        if block == 'window':
            monkeypatch.setattr(app.clock,'local',lambda:app.clock._start.replace(hour=4))
        elif block == 'lease':
            app.server.lease._held = False
        elif block == 'boss_5man':
            monkeypatch.setattr(app.engine,'_in_boss_fight',lambda now:True)
        else:
            monkeypatch.setattr(app.engine.raid,'phase',lambda now:RaidPhase(block))
        await e.tick(app.clock.now())
        await finished(e)
        assert exited.is_set()
    run(go())
    reason = block if block in {'window','lease'} else 'raid_hold'
    assert json.loads(app.store.get_meta('server:recent'))[-1]['reason'] == reason


def test_needs_claims_logs_once_per_local_day_without_blocking(app,monkeypatch,caplog):
    app.engine.live_review.claims_enabled = False
    async def go():
        await app.server.engine.tick(app.clock.now())
        await finished(app.server.engine)
        await app.server.engine.tick(app.clock.now()+5)
        assert len(app.turns) == 5
        assert sum(r.message=='server_needs_claims' for r in caplog.records) == 1
        tomorrow = app.clock.local().replace(day=app.clock.local().day+1)
        monkeypatch.setattr(app.clock,'local',lambda:tomorrow)
        await app.server.engine.tick(app.clock.now()+10)
    run(go())
    assert sum(r.message=='server_needs_claims' for r in caplog.records) == 2


@pytest.mark.parametrize('server_on,claims_on', [(False,False),(True,True)])
def test_no_claims_warning_when_server_is_off_or_claims_are_on(app,caplog,server_on,claims_on):
    app.server.engine.config = replace(app.server.engine.config,enabled=server_on)
    app.engine.live_review.claims_enabled = claims_on
    async def go():
        await app.server.engine.tick(app.clock.now())
        await finished(app.server.engine)
    run(go())
    assert not any(r.message=='server_needs_claims' for r in caplog.records)


@pytest.mark.parametrize('offered',[False,True])
def test_photo_seed_is_spent_only_when_its_photo_was_offered(app,monkeypatch,offered):
    """An opener that could not even be offered its photo (another photo in flight) leaves the
    seed for a later conversation; one that was offered it spends the seed as any opener does."""
    seed = Seed('photo:7','photo',('pets',),'an invented scene',(101,))
    app.observed.seeds = [seed]
    async def dropped(turn,meta):
        app.turns.append((turn,meta))
        meta.turn_id = 'photo-dropped'
        if offered:
            meta.photo = SimpleNamespace()  # offered, then refused by a guard
        return []
    monkeypatch.setattr(app.engine,'speak_meta',dropped)
    async def go():
        await app.server.engine.tick(app.clock.now())
        await finished(app.server.engine)
    run(go())
    assert len(app.turns) == 1 and app.turns[0][1].photo_seed == 7
    assert json.loads(app.store.get_meta('server:recent'))[-1]['reason'] == 'opener_dropped'
    assert (fresh(app.store.conn,[seed],now=app.clock.now()) == []) == offered
