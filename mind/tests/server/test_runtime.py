import asyncio
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from conftest import seed_test_cast
from leeroy_mind.discord.store import post
from leeroy_mind.server import runtime


def app_for(store):
    personas = seed_test_cast(store)
    cast = store.cast()
    loop_thread = threading.get_ident()
    calls = []
    def loop_only(name, value):
        assert threading.get_ident() == loop_thread
        calls.append(name)
        return value
    world = SimpleNamespace(human_online=False, group=None, online=set(),
        discord_members=lambda:loop_only('cast',cast),
        is_officer=lambda g:loop_only('officers',g in {102,104}))
    app = SimpleNamespace(store=store, world=world, human_guid=1, clock=store.clock,
        data_dir=Path(store.conn.execute('PRAGMA database_list').fetchone()['file']).parent,
        personas=SimpleNamespace(get=lambda g:loop_only('persona',personas.get(g))),
        guild=SimpleNamespace(members_in_play=lambda:loop_only('play',frozenset({103}))),
        engine=SimpleNamespace(comms=SimpleNamespace(room_of=lambda g:loop_only('room',None))),
        realm=SimpleNamespace(cfg=SimpleNamespace(trial_rank=4)))
    return app, personas, calls


def test_snapshot_uses_a_separate_read_only_connection_on_a_worker(store, monkeypatch):
    app,personas,calls=app_for(store)
    loop_thread=threading.get_ident()
    real_connect, real_state=runtime.connect,runtime.state_of
    workers,connections=[],[]
    def connect(path):
        workers.append(threading.get_ident())
        assert workers[-1] != loop_thread
        conn=real_connect(path)
        assert conn is not store.conn
        with pytest.raises(sqlite3.OperationalError,match='readonly'):
            conn.execute("INSERT INTO meta VALUES ('worker_write','forbidden')")
        conn.rollback()
        connections.append(conn)
        return conn
    def state(*args,**kwargs):
        assert threading.get_ident()!=loop_thread
        assert kwargs['conn'] is connections[-1] and kwargs['observed'].persona is personas[args[1]]
        return real_state(*args,**kwargs)
    monkeypatch.setattr(runtime,'connect',connect)
    monkeypatch.setattr(runtime,'state_of',state)
    result=asyncio.run(runtime.snapshot(app,app.clock.now()))
    assert result.in_play==frozenset({103}) and result.officers==frozenset({102,104})
    assert result.classes[101]==1 and set(result.personas)==set(personas)
    assert result.states[103].presence.playing
    assert calls and len(workers)==1
    assert store.get_meta('worker_write') is None


def test_snapshot_uses_captured_runtime_values_after_the_loop_changes(store, monkeypatch):
    app,personas,_=app_for(store)
    entered,release=threading.Event(),threading.Event()
    real_connect=runtime.connect
    original=personas[101]
    def connect(path):
        entered.set()
        assert release.wait(3)
        return real_connect(path)
    monkeypatch.setattr(runtime,'connect',connect)
    async def run():
        task=asyncio.create_task(runtime.snapshot(app,app.clock.now()))
        try:
            assert await asyncio.to_thread(entered.wait,2)
            personas[101]=replace(original,character_name='Changed')
            app.world.online.add(101)
            app.human_guid=105
            release.set()
            result=await task
            assert result.personas[101] is original and result.human==1
            assert not result.states[101].presence.playing
        finally:
            release.set()
            await asyncio.gather(task,return_exceptions=True)
    asyncio.run(run())


def test_snapshot_seeds_keep_private_sources_out_and_library_read_only(store, monkeypatch):
    app,personas,_=app_for(store)
    now=app.clock.now()
    library=app.data_dir/'library.db'
    with sqlite3.connect(library) as conn:
        conn.execute('CREATE TABLE item(id INTEGER,name TEXT)')
        conn.execute("INSERT INTO item VALUES (123,'Test axe')")
    with store.transaction():
        store.conn.execute("INSERT INTO dkp_ledger(ts,guid,delta,reason,item_id) VALUES (?,101,-20,'item',123)",(now,))
        for channel,readers,text in [('guild',list(personas),'Public plan'),('dm:101',[101,1],'PRIVATE DM PLAN'),
                                     ('officer',[102,104],'PRIVATE OFFICER PLAN')]:
            source=store.conn.execute("INSERT INTO chat_line(ts,channel,from_guid,from_name,to_guid,text,origin,audience) "
                "VALUES (?,?,101,'Arkon',1,?,'mind',?)",(now,channel,text,json.dumps(readers))).lastrowid
            store.conn.execute("INSERT INTO member_claim(guid,kind,subject,text,polarity,source,source_line,first_seen,last_seen,embedding) "
                "VALUES (101,'plan','task',?,1,'chat',?,?,?,zeroblob(2048))",(text,source,now,now))
        store.conn.execute("INSERT INTO relationship(a,b,affinity,notes,pinned) VALUES (101,1,60,'PRIVATE NOTES','[17]')")
    post(store,channel='dm:101',author=1,origin='human',text='PRIVATE DM BODY',now=now)
    real_state=runtime.state_of
    def state(*args,**kwargs):
        with pytest.raises(sqlite3.OperationalError,match='readonly'):
            kwargs['conn'].execute("UPDATE library.item SET name='forbidden'")
        return real_state(*args,**kwargs)
    monkeypatch.setattr(runtime,'state_of',state)
    result=asyncio.run(runtime.snapshot(app,now,include_seeds=True))
    assert [s.fact for s in result.seeds if s.kind=='plan']==['Public plan']
    assert any('Test axe' in s.fact for s in result.seeds)
    assert not any('PRIVATE' in s.fact for s in result.seeds)
    assert result.relationships[101].notes=='' and result.relationships[101].pinned==()
    assert result.ties[101,1]=='friend'
    with sqlite3.connect(library) as conn:
        assert conn.execute('SELECT name FROM item').fetchone()[0]=='Test axe'


def test_runtime_query_budget_interrupts_a_runaway_source(store,monkeypatch):
    app,_,_=app_for(store)
    ticks=iter(range(0,10**9,100))  # every look at the worker's clock is past the last budget
    monkeypatch.setattr(runtime,'time',SimpleNamespace(thread_time=lambda:float(next(ticks))))
    monkeypatch.setattr(runtime,'RETRY_WAIT_S',(0,))
    def slow(*args,**kwargs):
        kwargs['conn'].execute('WITH RECURSIVE n(x) AS (VALUES(0) UNION ALL SELECT x+1 FROM n WHERE x<1000000) SELECT sum(x) FROM n').fetchone()
    monkeypatch.setattr(runtime,'state_of',slow)
    with pytest.raises(sqlite3.OperationalError,match='interrupted'):
        asyncio.run(runtime.snapshot(app,app.clock.now()))


def test_snapshot_away_and_ties_are_observed_from_the_database(store):
    app,_,_=app_for(store)
    now=app.clock.now()
    with store.transaction():
        store.conn.execute("INSERT INTO away(guid,left_at,returns_at) VALUES (101,?,?)",(now-60,now+60))
        store.conn.execute("INSERT INTO relationship(a,b,affinity,romance_stage) VALUES (101,102,-50,'none'),(102,101,0,'notices')")
        store.conn.execute("INSERT INTO feud(a,b,started,cause,evidence) VALUES (103,104,?,'test','[]')",(now,))
    result=asyncio.run(runtime.snapshot(app,now))
    assert result.away==frozenset({101})
    assert result.ties[101,102]=='rival' and result.ties[102,101]=='crush'
    assert result.ties[103,104]==result.ties[104,103]=='feud'


def locked():
    error=sqlite3.OperationalError('database is locked')
    error.sqlite_errorname='SQLITE_BUSY'
    return error


def test_a_read_that_met_another_connection_is_asked_again(store,monkeypatch,caplog):
    app,personas,_=app_for(store)
    real_connect,opened=runtime.connect,[]
    def connect(path):
        opened.append(path)
        if len(opened)<3:
            raise locked()
        return real_connect(path)
    monkeypatch.setattr(runtime,'connect',connect)
    monkeypatch.setattr(runtime,'RETRY_WAIT_S',(0,))
    with caplog.at_level('INFO'):
        result=asyncio.run(runtime.snapshot(app,app.clock.now()))
    assert set(result.personas)==set(personas) and len(opened)==3
    assert [r.getMessage() for r in caplog.records if 'server_read' in r.getMessage()]==[
        'server_read_retry code=busy attempt=1 seeds=0','server_read_retry code=busy attempt=2 seeds=0']


def test_a_read_that_keeps_failing_names_its_class_and_never_the_message(store,monkeypatch,caplog):
    app,_,_=app_for(store)
    opened=[]
    def connect(path):
        opened.append(path)
        raise locked()
    monkeypatch.setattr(runtime,'connect',connect)
    monkeypatch.setattr(runtime,'RETRY_WAIT_S',(0,))
    with caplog.at_level('INFO'), pytest.raises(sqlite3.OperationalError):
        asyncio.run(runtime.snapshot(app,app.clock.now()))
    assert len(opened)==len(runtime.READ_CPU_S)
    assert caplog.records[-1].getMessage()=='server_read_failed code=busy attempts=3 seeds=0'
    assert not any('database is locked' in r.getMessage() for r in caplog.records)


def test_a_fault_a_retry_would_repeat_is_not_retried(store,monkeypatch):
    app,_,_=app_for(store)
    opened=[]
    def connect(path):
        opened.append(path)
        raise sqlite3.OperationalError('no such table: secret_table')
    monkeypatch.setattr(runtime,'connect',connect)
    with pytest.raises(sqlite3.OperationalError):
        asyncio.run(runtime.snapshot(app,app.clock.now()))
    assert len(opened)==1


def test_a_retry_gets_a_larger_budget(store,monkeypatch):
    app,_,_=app_for(store)
    real_state=runtime.state_of
    clock=iter(range(0,10**9,3))  # 3 CPU seconds pass between two looks at the worker's clock
    monkeypatch.setattr(runtime,'time',SimpleNamespace(thread_time=lambda:float(next(clock))))
    monkeypatch.setattr(runtime,'RETRY_WAIT_S',(0,))
    def slow(*args,**kwargs):
        kwargs['conn'].execute('WITH RECURSIVE n(x) AS (VALUES(0) UNION ALL SELECT x+1 FROM n WHERE x<3000) SELECT sum(x) FROM n').fetchone()
        return real_state(*args,**kwargs)
    monkeypatch.setattr(runtime,'state_of',slow)
    monkeypatch.setattr(runtime,'READ_CPU_S',(2.0,1e12))
    result=asyncio.run(runtime.snapshot(app,app.clock.now()))  # interrupted at 2 s, then given room
    assert result.states


def test_the_relationship_read_takes_two_columns_of_every_pair_and_his_rows_whole(store):
    app,_,_=app_for(store)
    with store.transaction():
        store.conn.execute("INSERT INTO relationship(a,b,affinity,trust,notes,romance_stage) VALUES "
                           "(101,102,70,5,'PRIVATE PAIR NOTES','none'),(102,1,10,44,'PRIVATE','banter')")
    result=asyncio.run(runtime.snapshot(app,app.clock.now()))
    assert result.ties[101,102]=='friend' and result.ties[102,1]=='crush'
    assert result.relationships[102].trust==44 and result.relationships[102].notes==''
    assert result.relationships[101].affinity==0
    assert 'notes' not in runtime._TIE_SQL and '*' not in runtime._TIE_SQL+runtime._HIS_SQL
