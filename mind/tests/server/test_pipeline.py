"""Server turns share the real transport, scheduler and privacy boundaries."""
import asyncio
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


def test_failed_post_record_rolls_back_message_and_receipts(rig, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError('synthetic failure')
    monkeypatch.setattr(rig.engine, '_record', fail)
    m = meta(server_conversation='failed')
    assert rig.engine._deliver_discord(turn(),m,rig.engine.personas.get(101),'invented line',[]) == 'record_error'
    assert not rig.store.conn.execute('SELECT id FROM discord_message').fetchall()
    assert counts(rig.store,rig.clock.local().date().isoformat())['messages'] == 0
    assert rig.store.get_meta('server:conversation:failed') is None
    assert not rig.reactions


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
