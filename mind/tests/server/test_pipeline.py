"""Server turns share the real transport, scheduler and privacy boundaries."""
import asyncio
import random
from dataclasses import replace
from types import SimpleNamespace

import pytest

from conftest import HUMAN, unit
from engine_rig import engine_clock, make_rig
from leeroy_mind.discord import store as dstore
from leeroy_mind.discord.transport import DiscordTarget
from leeroy_mind.llm.scheduler import Priority
from leeroy_mind.guards import Verdict
from leeroy_mind.server.records import counts
from leeroy_mind.talk.pipeline import TurnMeta
from leeroy_mind.talk.turns import Turn, TurnKind
from leeroy_mind.storage.rows import pack_vec


@pytest.fixture
def clock():
    return engine_clock()


@pytest.fixture
def rig(store, clock, tmp_path):
    rig = make_rig(store, clock, tmp_path)
    callbacks = []
    rig.engine.server_app = SimpleNamespace(
        server=SimpleNamespace(reactions=SimpleNamespace(after_post=lambda *a, **k: callbacks.append((a,k)))),
        engine=rig.engine, world=rig.world, store=store, clock=clock,
        personas=rig.engine.personas, human_guid=HUMAN, guild=None)
    rig.reactions = callbacks
    yield rig
    rig.close()


def turn(guid=101, channel='discord:general', **kwargs):
    return Turn(TurnKind.CONTINUE, guid, channel, priority=Priority.BACKGROUND, **kwargs)


def meta(**kwargs):
    return TurnMeta('', why='server:purpose', **kwargs)


def post(rig, text, *, author=102, channel='general', age=0):
    return dstore.post(rig.store, channel=channel, author=author, origin='mind',
                       text=text, now=rig.clock.now()-age)


def test_server_thread_keeps_old_rows_and_never_imports_private_or_deleted_rows(rig):
    old = post(rig, 'OLD THREAD FACT', age=90000)
    parent = post(rig, 'THREAD REPLY', author=103, age=900)
    unrelated = post(rig, 'UNRELATED RECENT LINE')
    secret = post(rig, 'PRIVATE DM FACT', channel='dm:102')
    officer = post(rig, 'OFFICER FACT', channel='officer-chat')
    deleted = post(rig, 'DELETED FACT')
    dstore.delete(rig.store, deleted, author=102, now=rig.clock.now())
    m = meta(server_thread=(old, parent, secret, officer, deleted),
             discord=DiscordTarget(parent, None), review_required=True)
    history, context = rig.engine.live_review.history(turn(), m)
    assert [h.id for h in context] == [old, parent]
    assert unrelated in {h.id for h in history}  # repeat evidence only
    block = rig.engine.live_review.block(turn(), m, close=False)
    assert 'OLD THREAD FACT' in block and 'THREAD REPLY' in block
    assert all(s not in block for s in ('PRIVATE DM FACT', 'OFFICER FACT', 'DELETED FACT', 'UNRELATED RECENT LINE'))


def test_deleted_parent_yields_no_context(rig):
    parent = post(rig, 'GONE PARENT')
    dstore.delete(rig.store, parent, author=102, now=rig.clock.now())
    assert rig.engine.live_review.history(turn(), meta(server_thread=(parent,),
           discord=DiscordTarget(parent,None))) == ((), ())


def test_thread_ids_have_no_effect_with_server_off(rig):
    old = post(rig, 'OLD THREAD FACT', age=900)
    recent = post(rig, 'RECENT CHANNEL FACT')
    rig.engine.server_app.server = None
    _, context = rig.engine.live_review.history(turn(), meta(server_thread=(old,)))
    assert [h.id for h in context] == [recent]


def test_model_context_uses_the_same_explicit_thread(rig):
    old = post(rig, 'DISTINCT OLD HARBOR FACT', age=900)
    post(rig, 'UNRELATED RECENT VEGETABLE FACT')
    m = meta(server_thread=(old,), discord=DiscordTarget(old,None), review_required=True)
    composed = rig.sync(rig.engine._compose(turn(addressee=102), m, live=False))
    rendered = str(composed.messages)
    assert 'DISTINCT OLD HARBOR FACT' in rendered
    assert 'UNRELATED RECENT VEGETABLE FACT' not in rendered


def test_cold_embedding_batch_keeps_late_parent_and_thread_within_32_texts(rig, monkeypatch):
    old = post(rig, 'OLD HARBOR CONTEXT', age=90000)
    parent = post(rig, 'LATE PARENT QUESTION', age=900)
    for i in range(55):
        post(rig,f'NEW CHANNEL EVIDENCE {i}',age=100-i)
    batches = []
    async def embed(texts, priority):
        batches.append(tuple(texts))
        return [pack_vec(unit(0)) for _ in texts]
    monkeypatch.setattr(rig.engine.recall._embedder,'embed',embed)
    m = meta(server_thread=(old,parent),discord=DiscordTarget(parent,None),
             server_anchor='Harbor planning',review_required=True)
    rig.sync(rig.engine.live_review.check(turn(),m,'A new reply about the harbor'))
    assert len(batches) == 1 and len(batches[0]) <= 32
    assert 'LATE PARENT QUESTION' in batches[0] and 'OLD HARBOR CONTEXT' in batches[0]


def test_his_game_answer_precedes_the_next_server_generation(rig, monkeypatch):
    engine, order = rig.engine, []
    engine.scheduler._max = 1
    gate = asyncio.Event()
    async def compose(*args, **kwargs):
        return SimpleNamespace(memory_ids=[], close=False)
    async def generate(t, m, c, **kwargs):
        async def job(llm):
            order.append((t.speaker, t.priority))
            if t.speaker == 101:
                await gate.wait()
            return 'a useful invented thought', None
        return await engine.scheduler.submit(t.priority, job, deadline=t.deadline)
    async def checked(t, m, p, lines, c, **kwargs):
        return lines
    async def review(*args, **kwargs):
        return Verdict('send')
    monkeypatch.setattr(engine, '_compose', compose)
    monkeypatch.setattr(engine, '_generate', generate)
    monkeypatch.setattr(engine, '_guarded_lines', checked)
    monkeypatch.setattr(engine.live_review, 'check', review)
    monkeypatch.setattr(engine.guards, 'allow', lambda *a, **k: True)
    monkeypatch.setattr(engine.guards, 'chain_open', lambda *a, **k: True)
    async def run():
        first = asyncio.create_task(engine.speak_meta(turn(101), meta()))
        for _ in range(30):
            await asyncio.sleep(0)
        assert order == [(101, Priority.BACKGROUND)]
        second = asyncio.create_task(engine.speak_meta(turn(102), meta()))
        human = asyncio.create_task(engine.speak_meta(
            Turn(TurnKind.REPLY_HUMAN,103,'guild',addressee=HUMAN), TurnMeta('',why='reply:test')))
        for _ in range(30):
            await asyncio.sleep(0)
        gate.set()
        await rig.advance(90)
        await asyncio.gather(first, second, human)
    rig.sync(run())
    assert order == [(101,Priority.BACKGROUND),(103,Priority.REPLY_HUMAN),(102,Priority.BACKGROUND)]


@pytest.mark.parametrize('source_kind',['conversation','dm'])
@pytest.mark.parametrize('queued',[False,True],ids=['holding-low-slot','waiting-for-low-slot'])
def test_his_answer_cancels_same_members_background_review_lock(rig, monkeypatch, source_kind, queued):
    from leeroy_mind.server.config import ServerConfig
    from leeroy_mind.server.dms import DmOpeners
    from leeroy_mind.server.engine import Conversation, ServerEngine
    from leeroy_mind.server.seeds import Seed
    engine, app = rig.engine, rig.engine.server_app
    engine.live_review.config = replace(engine.live_review.config,enabled=True)
    engine.scheduler._max, engine.scheduler._max_low = 3,1
    service = ServerEngine(app,ServerConfig(enabled=True),random.Random(1))
    dm = DmOpeners(app,ServerConfig(enabled=True),random.Random(1))
    app.server.engine, app.server.dms = service,dm
    gate, serial_entered, low_entered = asyncio.Event(),asyncio.Event(),asyncio.Event()
    generated = []
    async def compose(*args, **kwargs):
        return SimpleNamespace(memory_ids=[],close=False)
    async def generate_serial(t,m,c,**kwargs):
        # _generate's real per-member serial lock surrounds this scheduler wait.
        if t.priority == Priority.BACKGROUND:
            serial_entered.set()
        async def job(llm):
            generated.append((t.priority,rig.clock.now()))
            if t.priority == Priority.BACKGROUND:
                low_entered.set()
                await gate.wait()
            return 'yes I can join tonight',None
        return await engine.scheduler.submit(t.priority,job,deadline=t.deadline)
    async def checked(t,m,p,lines,c,**kwargs):
        return lines
    async def review(*args,**kwargs):
        return Verdict('send')
    monkeypatch.setattr(engine,'_compose',compose)
    monkeypatch.setattr(engine,'_generate_serial',generate_serial)
    monkeypatch.setattr(engine,'_guarded_lines',checked)
    monkeypatch.setattr(engine.live_review,'check',review)
    monkeypatch.setattr(engine.guards,'allow',lambda *a,**k:True)
    monkeypatch.setattr(engine.guards,'chain_open',lambda *a,**k:True)
    async def run():
        blocker = None
        if queued:
            async def occupy_low(llm):
                low_entered.set()
                await gate.wait()
            blocker = asyncio.create_task(engine.scheduler.submit(Priority.EXTRACTION,occupy_low))
            await asyncio.wait_for(low_entered.wait(),1)
        background = asyncio.create_task(engine.speak_meta(turn(101),meta()))
        if source_kind == 'conversation':
            conversation = Conversation('in-flight','general',Seed('purpose:test','purpose',('general',),'a topic'),
                                        [101,102],rig.clock.now(),5)
            conversation.in_flight = 101
            conversation.task = background
            service._live[conversation.id] = conversation
        else:
            dm._pending,dm._pending_guid = background,101
        try:
            await asyncio.wait_for(serial_entered.wait(),1)
            if not queued:
                await asyncio.wait_for(low_entered.wait(),1)
            assert engine.live_review.owners[101] is background
            assert 101 in engine.active_speakers()
            assert 101 not in engine.active_speakers(exclude_task=background)
            before = rig.clock.now()
            engine._queue([(Turn(TurnKind.REPLY_HUMAN,101,'guild',addressee=HUMAN),
                            TurnMeta('owed-answer',why='reply:test'))])
            assert background.cancelling()  # before the answer task can wait on her lock
            await rig.advance(40)
            answered = [at for priority,at in generated if priority == Priority.REPLY_HUMAN]
            assert answered and answered[0] < before+30
            assert background.cancelled()
            assert not any(row['dropped']=='expired' for row in rig.utterances())
            assert any(row['kind']=='reply_human' and row['dropped'] is None for row in rig.utterances())
            if queued:
                assert all(priority != Priority.BACKGROUND for priority,_ in generated)
        finally:
            gate.set()
            await asyncio.gather(background,*([blocker] if blocker else []),return_exceptions=True)
            await service.close()
            await dm.close()
    rig.sync(run())


@pytest.mark.parametrize('kind,fields',[(TurnKind.REPLY_HUMAN,{}),
    (TurnKind.CONTINUE,{'answers_human':True}),(TurnKind.CONTINUE,{'first_question_answer':True}),
    (TurnKind.EMOTE_REACT,{'why':'emote:his'})])
def test_queue_yields_background_sources_for_every_kind_of_owed_answer(rig, monkeypatch, kind, fields):
    called = []
    rig.engine.server_app.server.engine = SimpleNamespace(answer_queued=lambda g:called.append(('engine',g)))
    rig.engine.server_app.server.dms = SimpleNamespace(answer_queued=lambda g:called.append(('dm',g)))
    m = TurnMeta('owed',**fields)
    rig.engine._answer_queued(Turn(kind,101,'guild'),m)
    assert called == [('engine',101),('dm',101)]


def test_server_post_and_all_receipts_commit_together(rig):
    engine, now = rig.engine, rig.clock.now()
    m = meta(server_conversation='test-conversation')
    m.turn_id = 'first'
    assert engine._deliver_discord(turn(),m,engine.personas.get(101),'invented first line',[]) is None
    assert engine._deliver_discord(turn(102),replace(m,turn_id='second',said=0),
           engine.personas.get(102),'invented second line',[]) is None
    c = counts(rig.store, rig.clock.local().date().isoformat())
    assert c == {'conversations':1,'messages':2,'reactions':0,'dms':0}
    assert rig.store.get_meta('server:conversation:test-conversation') == str(now)
    assert len(rig.reactions) == 2 and not rig.sent.log


def test_failed_receipt_rolls_back_message_before_record_has_any_effect(rig, monkeypatch):
    from leeroy_mind.server import records
    recorded = []
    def fail(*args, **kwargs):
        raise RuntimeError('synthetic failure')
    monkeypatch.setattr(records, 'posted', fail)
    monkeypatch.setattr(rig.engine, '_record', lambda *a, **k: recorded.append(k))
    m = meta(server_conversation='failed')
    assert rig.engine._deliver_discord(turn(),m,rig.engine.personas.get(101),'invented line',[]) == 'record_error'
    assert not rig.store.conn.execute('SELECT id FROM discord_message').fetchall()
    assert counts(rig.store,rig.clock.local().date().isoformat())['messages'] == 0
    assert rig.store.get_meta('server:conversation:failed') is None
    assert not rig.reactions and not recorded and m.said == 0


def test_record_hook_failure_cannot_roll_back_ids_already_used_in_memory(rig):
    engine = rig.engine
    def fail(*args, **kwargs):
        raise RuntimeError('synthetic hook failure')
    engine.signals = SimpleNamespace(sent=fail)
    m = meta(server_conversation='durable', words='invented words')
    m.turn_id = 'durable-turn'
    assert engine._deliver_discord(turn(),m,engine.personas.get(101),'invented line',[]) == 'record_error'
    message = rig.store.conn.execute('SELECT id FROM discord_message').fetchone()
    line = rig.store.conn.execute('SELECT id FROM chat_line').fetchone()
    assert message and line and engine._words[line[0]] == 'invented words'
    assert m.said == 1 and engine._turn_lines[m.turn_id] == 1
    assert counts(rig.store,rig.clock.local().date().isoformat())['messages'] == 1
    assert rig.store.get_meta('server:conversation:durable') is not None


@pytest.mark.parametrize('failure,event', [('store','discord_store_failed'),('record','say_record_failed')])
def test_server_off_keeps_existing_failure_log_names(rig, monkeypatch, caplog, failure, event):
    from leeroy_mind.talk import pipeline
    rig.engine.server_app.server = None
    def fail(*args, **kwargs):
        raise RuntimeError('synthetic failure')
    if failure == 'store':
        monkeypatch.setattr(pipeline,'store_line',fail)
    else:
        monkeypatch.setattr(rig.engine,'_record',fail)
    assert rig.engine._deliver_discord(turn(),meta(),rig.engine.personas.get(101),'invented line',[]) == failure+'_error'
    assert any(r.message.startswith(event+':') for r in caplog.records)
    assert not any(r.message.startswith('discord_delivery_failed') for r in caplog.records)


@pytest.mark.parametrize('raw', ['not-json','[]','{"messages":-1}','{"dms":true}'])
def test_bad_server_counter_blocks_server_posts_but_keeps_d1_and_reactions(rig, monkeypatch, caplog, raw):
    key = 'server:day:'+rig.clock.local().date().isoformat()
    rig.store.set_meta(key,raw)
    for _ in range(2):
        assert rig.engine._deliver_discord(turn(),meta(),rig.engine.personas.get(101),'blocked line',[]) == 'record_error'
    assert not rig.store.conn.execute('SELECT id FROM discord_message').fetchall()
    assert not rig.store.conn.execute('SELECT id FROM chat_line').fetchall()
    assert rig.engine._deliver_discord(turn(),TurnMeta('d1',why='discord:reply'),
           rig.engine.personas.get(101),'a useful answer to him',[]) is None
    parent = post(rig,'the invented raid went well')
    monkeypatch.setattr(rig.engine.guards,'allow',lambda *a,**k:True)
    monkeypatch.setattr(rig.engine.guards,'chain_open',lambda *a,**k:True)
    m = TurnMeta('react',why='discord:reply',discord=DiscordTarget(parent,None))
    rig.sync(rig.engine._guard_react(turn(deadline=rig.clock.now()+180),m,'🫡'))
    assert rig.store.conn.execute('SELECT count(*) FROM discord_reaction').fetchone()[0] == 1
    assert rig.store.get_meta(key) == raw
    assert sum(r.message == 'server_day_counter_bad' for r in caplog.records) == 1


def test_server_dm_post_is_pair_private_and_records_opening(rig):
    t = turn(101,'dm:101',to_guid=HUMAN,addressee=HUMAN)
    m = TurnMeta('dm-first', why='server:dm', initiates=True)
    assert rig.engine._deliver_discord(t,m,rig.engine.personas.get(101),'PRIVATE OPENER FACT',[]) is None
    row = rig.store.conn.execute('SELECT id FROM chat_line').fetchone()
    assert set(rig.store.chat_lines([row[0]])[0].audience) == {HUMAN,101}
    assert rig.store.get_meta('server:dm:open:101') == str(rig.clock.now())
    assert rig.store.get_meta('server:dm:day:'+rig.clock.local().date().isoformat()+':101') is not None
    assert counts(rig.store,rig.clock.local().date().isoformat())['dms'] == 1
    _, context = rig.engine.live_review.history(turn(102),meta())
    assert all('PRIVATE OPENER FACT' not in h.text for h in context)
    assert not rig.sent.log


def test_d1_posts_schedule_reactions_without_server_counts(rig):
    m = TurnMeta('d1', why='discord:reply')
    assert rig.engine._deliver_discord(turn(),m,rig.engine.personas.get(101),'invented answer',[]) is None
    assert len(rig.reactions) == 1
    assert counts(rig.store,rig.clock.local().date().isoformat())['messages'] == 0


def test_server_off_transport_has_no_new_receipts_or_reactions(rig):
    rig.engine.server_app.server = None
    assert rig.engine._deliver_discord(turn(),meta(),rig.engine.personas.get(101),'invented answer',[]) is None
    assert not rig.store.conn.execute("SELECT key FROM meta WHERE key LIKE 'server:%'").fetchall()
    assert not rig.reactions


def test_speak_meta_assigns_id_preserves_caller_turn_and_limits_to_one_line(rig, monkeypatch):
    seen = []
    async def generate(t,m,c,**kwargs):
        seen.append((t.max_lines,t.priority,m.review_required,m.keep_channel))
        return 'invented reply',None
    async def compose(*args, **kwargs):
        return SimpleNamespace(memory_ids=[],close=False)
    async def checked(t,m,p,lines,c,**kwargs):
        return lines
    async def review(*args, **kwargs):
        return Verdict('send')
    monkeypatch.setattr(rig.engine,'_generate',generate)
    monkeypatch.setattr(rig.engine,'_compose',compose)
    monkeypatch.setattr(rig.engine,'_guarded_lines',checked)
    monkeypatch.setattr(rig.engine.live_review,'check',review)
    t, m = turn(), meta()
    async def run():
        task = asyncio.create_task(rig.engine.speak_meta(t,m))
        await rig.advance(60)
        assert task.done() and task.result()
    rig.sync(run())
    assert m.turn_id and t.deadline == 0 and t.max_lines == 2
    assert seen == [(1,Priority.BACKGROUND,True,True)]


def test_guard_filler_reaction_counts_only_a_new_persisted_reaction(rig, monkeypatch):
    parent = post(rig,'the invented raid went well')
    m = meta(discord=DiscordTarget(parent,None))
    m.turn_id = 'filler'
    monkeypatch.setattr(rig.engine.guards,'allow',lambda *a,**k:True)
    monkeypatch.setattr(rig.engine.guards,'chain_open',lambda *a,**k:True)
    t = turn(deadline=rig.clock.now()+180)
    rig.sync(rig.engine._guard_react(t,m,'🫡'))
    rig.sync(rig.engine._guard_react(t,m,'🫡'))
    assert counts(rig.store,rig.clock.local().date().isoformat())['reactions'] == 1
    assert rig.store.conn.execute('SELECT count(*) FROM discord_reaction').fetchone()[0] == 1
    assert not rig.sent.log
