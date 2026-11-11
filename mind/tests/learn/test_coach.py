import asyncio
import json
import logging
from dataclasses import replace

import pytest

from tests.claims.conftest import context
from leeroy_mind.learn import coach, lessons
from leeroy_mind.learn.config import load_learn_config
from leeroy_mind.learn.weights import ensure
from leeroy_mind.learn.recipe import record
from leeroy_mind.learn.cli import learn_main
from leeroy_mind.nightly.llm import check_schema, ReflectionError
from leeroy_mind.config import load_config
from leeroy_mind.protocol import Chat
from leeroy_mind.talk.turns import Turn, TurnKind
from leeroy_mind.talk.pipeline import TurnMeta
from leeroy_mind.tuning.effective import effective, MemberTuning


def note(text='He keeps replies short and leaves room for the owner.',scope='guild',**over):
    return dict(text=text,scope=scope,non_manipulative=True,forward_min=0)|over


def answer(items=None,**over):
    return dict(lessons=[note()] if items is None else items,reinforce=[],drop=[],arm=None)|over


def enabled(tmp_path,auto=False):
    (tmp_path/'mind.toml').write_text('[learn]\nenabled=true\ncoach=true\nauto_lessons='+str(auto).lower()+'\n')


def exchanges(ctx,guid=101,channel='guild',n=10,offset=0):
    store=ctx.store
    now=ctx.clock.now()
    ids=[]
    for i in range(n):
        stamp=now-1000+offset+i*3
        for sender,recipient,text,origin in ((1,guid,f'owner fixture {offset+i}','human'),
                (guid,1,f'member fixture {offset+i}','mind')):
            lid=store.insert_chat_line(Chat(0,channel,sender,'Invented',recipient,text,0,0),
                                      ts=stamp+(sender!=1),session_id=None,origin=origin)
        with store.transaction():
            weights=ensure(store.conn,now=now)
            store.conn.execute('INSERT INTO talk_reward(line_id,guid,value,parts,weights_version,scored_at) '
                               'VALUES (?,?,?,?,?,?)',(lid,guid,(i%10)/5-1,'{}',weights.version,now))
            record(store.conn,lid,Turn(TurnKind.REPLY_HUMAN,guid,channel,to_guid=1),TurnMeta(f'turn:{lid}'),
                   ctx.persona(guid),load_config(ctx.settings.data_dir/'mind.toml'))
        ids.append(lid)
    return ids


@pytest.mark.parametrize('channel,scope',[('guild','guild'),('discord:general','guild'),('whisper','whisper'),
                                        ('dm:101','whisper'),('party','group'),('voice:room','group')])
def test_fake_model_proposes_scoped_lesson_once_without_logging_content(context,store,clock,tmp_path,caplog,channel,scope):
    enabled(tmp_path)
    async def go():
        async with context(lambda *_:json.dumps(answer([note(scope=scope)])),learn_enabled=True,learn_coach=True) as (ctx,llm):
            exchanges(ctx,channel=channel)
            with caplog.at_level(logging.INFO):
                await coach.stage(ctx)
                await coach.stage(ctx)
            saved=lessons.read(store.conn,guid=101,now=clock.now())
            assert len(saved)==1 and saved[0]['scope']==scope and saved[0]['status']=='proposed'
            assert saved[0]['expires']==clock.now()+14*lessons.DAY
            assert len(llm.calls)==1
            encoded=json.dumps(llm.calls)
            assert 'member fixture 0' in encoded and 'member fixture 9' in encoded
            assert len(coach.evidence(ctx,101)[2]) == 6
            assert 'owner fixture' not in caplog.text and note()['text'] not in caplog.text
    asyncio.run(go())


@pytest.mark.parametrize('item',[
    note('He guilt-trips the owner into staying.'),note('He pressures the owner to keep playing.'),
    note('He isolates the owner from friends.'),note('He escalates romance beyond the dials.'),
    note('He discusses sexual details.'),note('He repeats Avery Example in every reply.'),
    note('You keep replies short.'),note('He '+('repeats '*26)),note(non_manipulative=False),
    note(forward_min=10),note('He flirts more.',forward_min=1)])
def test_lesson_screens(context,item):
    async def go():
        async with context() as (ctx,_):
            p=effective(ctx.persona(101),MemberTuning({'forward':2}))
            assert lessons.screen(item,scope='guild',persona=p,ident=ctx.ident) is None
    asyncio.run(go())


def test_schema_rejects_injected_keys_attestation_and_excess_output():
    shape=coach.response_schema(False)
    check_schema(shape,answer())
    for response in [answer([note(non_manipulative=False)]),answer([note()]*3),
                     answer(arm={'family':'length','params':{}}),answer(instruction='fixture')]:
        with pytest.raises(ValueError):
            check_schema(shape,response)


def test_private_evidence_cannot_create_public_lessons_or_mix_other_pairs(context,store,tmp_path):
    enabled(tmp_path)
    async def go():
        async with context(lambda *_:json.dumps(answer([note(scope='guild'),note(scope='any')])),
                           learn_enabled=True,learn_coach=True) as (ctx,llm):
            exchanges(ctx,channel='whisper')
            store.insert_chat_line(Chat(0,'whisper',1,'Invented',102,'other pair secret',0,0),
                                   ts=ctx.clock.now()-980,session_id=None,origin='human')
            await coach.stage(ctx)
            assert lessons.read(store.conn,now=ctx.clock.now())==[]
            assert 'other pair secret' not in json.dumps(llm.calls)
    asyncio.run(go())


def test_room_evidence_excludes_game_and_officer_text(context,store,tmp_path):
    enabled(tmp_path)
    async def go():
        async with context(lambda *_:json.dumps(answer()),learn_enabled=True,learn_coach=True) as (ctx,llm):
            exchanges(ctx,channel='discord:general')
            exchanges(ctx,channel='guild',n=2,offset=200)
            exchanges(ctx,channel='officer',n=2,offset=300)
            await coach.stage(ctx)
            encoded=json.dumps(llm.calls)
            assert 'fixture 200' not in encoded and 'fixture 300' not in encoded
    asyncio.run(go())


def test_active_cap_expiry_reinforcement_and_foreign_ids(store,clock):
    now=clock.now()
    with store.transaction():
        for i in range(7):
            lessons.persist(store.conn,101,answer([note(f'He uses short reply style number {i}.')]),
                            known=set(),night='2026-11-11',now=now,automatic=True)
        rows=lessons.read(store.conn,guid=101,now=now)
        assert sum(r['status']=='active' for r in rows)==5
        chosen=rows[0]['id']
        lessons.persist(store.conn,101,answer([],reinforce=[chosen,999],drop=[rows[1]['id']]),
                        known={r['id'] for r in rows},night='2026-11-12',now=now+lessons.DAY,automatic=False)
    rows=lessons.read(store.conn,guid=101,now=now+lessons.DAY)
    saved=next(r for r in rows if r['id']==chosen)
    assert saved['reinforced']==1 and saved['expires']==now+15*lessons.DAY
    assert len(lessons.read(store.conn,now=now+14*lessons.DAY))==1


def test_feedback_is_numeric_and_scoped_to_room(context,store,clock):
    async def go():
        async with context() as (ctx,_):
            ids=exchanges(ctx)
            exchanges(ctx,channel='dm:101',offset=100)
            with store.transaction():
                lessons.persist(store.conn,101,answer(),known=set(),night=ctx.night,now=clock.now(),automatic=False)
                current=lessons.read(store.conn,now=clock.now())
                ident=current[0]['id']
                store.conn.execute('UPDATE talk_recipe SET lesson_ids=? WHERE line_id=?',(json.dumps([ident]),ids[-1]))
            info=coach.feedback(store.conn,101,current,now=clock.now(),channel='guild')
            assert info==[dict(id=ident,with_n=1,without_n=9,with_mean=.8,without_mean=pytest.approx(-.2))]
            assert all(type(v) in (int,float) for v in info[0].values())
    asyncio.run(go())


def test_member_minimum_cap_resume_and_budget_before_rewards(context,store,tmp_path,monkeypatch):
    enabled(tmp_path)
    monkeypatch.setattr(coach,'CAP',2)
    async def go():
        async with context(lambda *_:json.dumps(answer()),learn_enabled=True,learn_coach=True) as (ctx,llm):
            for guid in (101,102,103):
                exchanges(ctx,guid=guid)
            assert coach.count(ctx)==2
            await coach.stage(ctx)
            await coach.stage(ctx)
            assert len(llm.calls)==2 and coach.candidates(ctx)==[]
            with store.transaction():
                store.conn.execute('DELETE FROM talk_reward')
            assert coach.count(ctx)==2  # reserve work the earlier reward stage may create
            assert coach.candidates(ctx)==[]
    asyncio.run(go())


def test_disabled_and_under_ten_do_not_call_or_write(context,store,tmp_path):
    async def go():
        async with context(learn_enabled=True,learn_coach=True) as (ctx,llm):
            exchanges(ctx,n=9)
            await coach.stage(ctx)
            enabled(tmp_path)
            await coach.stage(ctx)
            assert not llm.calls and lessons.read(store.conn,now=ctx.clock.now())==[]
    asyncio.run(go())


def test_cli_is_read_only_and_old_schema_safe(context,store,clock,tmp_path,capsys,monkeypatch):
    with store.transaction():
        lessons.persist(store.conn,101,answer(),known=set(),night='2026-11-11',now=clock.now(),automatic=False)
    monkeypatch.setattr('leeroy_mind.learn.cli.time.time',clock.now)
    before=store.conn.total_changes
    assert learn_main(['lessons','--data-dir',str(tmp_path),'--guid','101'])==0
    assert note()['text'] in capsys.readouterr().out
    assert store.conn.total_changes==before
    with store.transaction():
        store.conn.execute('DROP TABLE talk_lesson')
    assert learn_main(['lessons','--data-dir',str(tmp_path)])==0
    assert json.loads(capsys.readouterr().out)=={'available':False,'lessons':[]}


@pytest.mark.parametrize('bad',['coach=1','auto_lessons="yes"','unknown=true'])
def test_config_fails_closed(tmp_path,bad):
    (tmp_path/'mind.toml').write_text('[learn]\nenabled=true\n'+bad)
    assert not load_learn_config(tmp_path/'mind.toml').enabled


@pytest.mark.parametrize('item',[note('He pressures the owner.'),note('He mentions Avery Example.'),
                                note('He discusses sexual details.'),note(forward_min=10)])
def test_model_screen_refusals_store_no_lesson(context,store,tmp_path,item):
    enabled(tmp_path)
    async def go():
        async with context(lambda *_:json.dumps(answer([item])),learn_enabled=True,learn_coach=True) as (ctx,llm):
            exchanges(ctx)
            await coach.stage(ctx)
            assert len(llm.calls)==1 and lessons.read(store.conn,now=ctx.clock.now())==[]
    asyncio.run(go())


def test_arm_proposal_uses_optional_registry_and_is_bounded(context,store,tmp_path,monkeypatch):
    from types import SimpleNamespace as NS
    enabled(tmp_path)
    proposed=[]
    fake=NS(PRESETS={'length':[{'min_words':3,'max_words':12}]},
            Arm=lambda *args:NS(params=args[2]),
            valid=lambda family,params:family=='length' and params=={'min_words':3,'max_words':12},
            bounded=lambda arm,**kw:arm.params,
            propose=lambda conn,family,params,**kw:proposed.append((family,params)))
    monkeypatch.setattr(coach,'registry',lambda _:fake)
    async def go():
        async with context(lambda *_:json.dumps(answer([],arm={'family':'length','params':{'min_words':3,'max_words':12}})),
                           learn_enabled=True,learn_coach=True) as (ctx,_):
            exchanges(ctx)
            await coach.stage(ctx)
            await coach.stage(ctx)
    asyncio.run(go())
    assert proposed==[('length',{'min_words':3,'max_words':12})]


def test_model_errors_keep_content_out_of_logs(context,store,tmp_path,caplog):
    enabled(tmp_path)
    def broken(*_):
        raise ValueError('private failure text')
    async def go():
        async with context(broken,learn_enabled=True,learn_coach=True) as (ctx,_):
            exchanges(ctx)
            with caplog.at_level(logging.INFO):
                await coach.stage(ctx)
            assert ctx.failures[coach.STAGE]==1
            assert not ctx.done(coach.STAGE,'101')
    asyncio.run(go())
    assert 'private failure text' not in caplog.text and 'member fixture' not in caplog.text


def test_attach_requires_both_flags_and_budget_tracks_stage(context,store,tmp_path):
    from leeroy_mind.learn.night import attach
    from leeroy_mind.nightly import budget
    from types import SimpleNamespace as NS
    calls=[]
    runner=NS(stages=[],add_stage=lambda name,*a,**kw:calls.append((name,kw)))
    attach(runner,tmp_path,store)
    assert calls==[]
    enabled(tmp_path)
    attach(runner,tmp_path,store)
    assert [name for name,_ in calls]==['rewards','talk_coach']
    assert calls[-1][1]==dict(after='rewards',model='reflection')
    assert coach.STAGE in budget.EXTENSION_STAGES
    assert budget.estimate({coach.STAGE:20},measured={})[coach.STAGE]==400


def test_optional_burst_membership_supplies_the_whole_exchange(context,store):
    async def go():
        async with context() as (ctx,_):
            ids=exchanges(ctx)
            with store.transaction():
                store.conn.execute('CREATE TABLE IF NOT EXISTS talk_recipe_line(line_id INTEGER PRIMARY KEY,recipe_line_id INTEGER)')
                store.conn.executemany('INSERT INTO talk_recipe_line VALUES (?,?)',[(ids[0],ids[0]),(ids[1],ids[0])])
            row=store.conn.execute('SELECT * FROM chat_line WHERE id=?',(ids[0],)).fetchone()
            assert coach.generated_text(store.conn,row)=='member fixture 0 member fixture 1'
    asyncio.run(go())
