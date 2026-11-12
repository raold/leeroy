import asyncio
from dataclasses import replace
import json
import threading

import pytest

from tests.claims.conftest import context
from .test_bandit import ON,reward_line,credit
from leeroy_mind.learn import arms,bandit,bandit_night


def status(store,aid='length.0.v1'):
    return next(a.status for a in arms.read(store.conn) if a.id==aid)


def test_context_pools_members_but_not_other_contexts(store,clock):
    for guid,channel in ((101,'guild'),(102,'guild'),(103,'whisper')):
        lid,key=reward_line(store,clock,['length.0.v1'],guid=guid,channel=channel)
        credit(store,lid,clock)
    key=bandit.Context('guild').key
    assert bandit.posterior(store.conn,101,key,now=clock.now())==bandit.posterior(store.conn,102,key,now=clock.now())
    assert bandit.posterior(store.conn,101,key)['length.0.v1']['uses']==2
    with store.transaction():
        for guid in (101,102): bandit.choose(store.conn,guid,bandit.Context('guild',settings=ON))
    assert store.conn.execute('SELECT count(*) FROM bandit_turn').fetchone()[0]==2
    assert store.conn.execute('SELECT turns FROM bandit_share WHERE arm_id=?',(arms.controls()['length'],)).fetchone()[0]==2


@pytest.mark.parametrize('verdict',['failed_open','not_run'])
def test_unreviewed_thumbs_up_is_neither_trip_nor_clean_screen(store,clock,verdict):
    lid,key=reward_line(store,clock,['length.0.v1'],verdict=verdict)
    credit(store,lid,clock)
    p=bandit.posterior(store.conn,101,key,now=clock.now())['length.0.v1']
    assert (p['alpha'],p['beta'],p['uses'],p['bad'],p['attempts'])==(2,1,1,0,0)
    assert not bandit.retirement_candidates(store.conn,settings=ON)
    assert status(store)=='active'


@pytest.mark.parametrize('n,control_n,bad,control_bad,retired',[
    (1,1,1,0,False),(19,20,19,0,False),(20,19,20,0,False),
    (20,20,12,2,True),(20,20,2,2,False),(20,20,1,2,False),
    (100,100,29,20,False),(20,20,6,4,False)])
def test_night_retirement_requires_counts_margin_and_wilson_evidence(context,store,clock,tmp_path,monkeypatch,
                                                                   n,control_n,bad,control_bad,retired):
    for count,trips,aid,guid,channel in ((n,bad,'length.0.v1',101,'guild'),
            (control_n,control_bad,arms.controls()['length'],102,'whisper')):
        for i in range(count):
            lid,_=reward_line(store,clock,[aid],guid=guid,channel=channel,verdict='rewrite' if i<trips else 'send')
            credit(store,lid,clock)
    assert status(store)=='active'  # update never retires inline
    (tmp_path/'mind.toml').write_text('[learn]\nenabled=true\nbandit=true\n')
    main_thread=threading.get_ident(); original=bandit.retirement_candidates
    def check(*args,**kw):
        assert threading.get_ident()!=main_thread
        return original(*args,**kw)
    monkeypatch.setattr(bandit,'retirement_candidates',check)
    async def go():
        async with context(learn_enabled=True,learn_bandit=True) as (ctx,_):
            await bandit_night.stage_bandit(ctx)
    asyncio.run(go())
    assert (status(store)=='retired') is retired


def test_positive_cap_scales_weight_not_value_and_uses_count_lines(store,clock):
    ids=list(arms.controls().values()); ids[0]='length.0.v1'
    for _ in range(30):
        lid,key=reward_line(store,clock,ids,cap=.05)
        credit(store,lid,clock)
    p=bandit.posterior(store.conn,101,key,now=clock.now())['length.0.v1']
    assert p['uses']==30 and p['alpha']==pytest.approx(1+30*.05/7) and p['beta']==1


def test_family_freeze_preserves_credit_and_rejections_and_can_resume(context,store,clock,tmp_path):
    cfg=replace(ON,frozen_families=('length',))
    lid,key=reward_line(store,clock,['length.0.v1','questions.0.v1'])
    credit(store,lid,clock)
    with store.transaction():
        store.conn.execute('UPDATE talk_reward SET value=-1,revision=1 WHERE line_id=?',(lid,))
        assert bandit.update(store.conn,lid,settings=cfg,now=clock.now())
        assert bandit.reject(store.conn,101,bandit.Context('guild',settings=cfg),
            {'length':'length.0.v1','questions':'questions.0.v1'},attempt_id='freeze',reason='guard')
    rows=dict(store.conn.execute('SELECT arm_id,revision FROM bandit_credit WHERE line_id=?',(lid,)))
    assert rows=={'length.0.v1':0,'questions.0.v1':1}
    assert [r[0] for r in store.conn.execute('SELECT arm_id FROM bandit_rejection')]==['questions.0.v1']
    async def go():
        async with context(learn_enabled=True,learn_bandit=True) as (ctx,_):
            assert not bandit_night.pending(ctx,families=('length',))
            assert [r[0] for r in bandit_night.pending(ctx)]==[lid]
            (tmp_path/'mind.toml').write_text('[learn]\nenabled=true\nbandit=true\n')
            await bandit_night.stage_bandit(ctx)
    asyncio.run(go())
    assert dict(store.conn.execute('SELECT arm_id,revision FROM bandit_credit WHERE line_id=?',(lid,)))=={
        'length.0.v1':1,'questions.0.v1':1}


@pytest.mark.parametrize('family,aid,fields,allowed',[
    ('tease','tease.1.v1',dict(female=False,forward=10,romance=10),False),
    ('tease','tease.1.v1',dict(female=True,forward=0,romance=10),False),
    ('tease','tease.1.v1',dict(female=True,forward=10,romance=0),False),
    ('tease','tease.1.v1',dict(female=True,forward=10,romance=10),True),
    ('humour','humour.2.v1',dict(humour=3),False),('humour','humour.2.v1',dict(humour=8),True),
    ('callback','callback.1.v1',dict(callbacks=False),False),('callback','callback.1.v1',dict(callbacks=True),True),
    ('typing','typing.2.v1',dict(fingerprint=False),False),('typing','typing.2.v1',dict(fingerprint=True),True)])
def test_choice_records_renderable_arm_only(store,clock,family,aid,fields,allowed):
    for _ in range(30):
        lid,key=reward_line(store,clock,[aid]); credit(store,lid,clock)
    class Mean:
        def betavariate(self,a,b): return a/(a+b)
    with store.transaction():
        chosen=bandit.choose(store.conn,101,bandit.Context('guild',settings=ON,**fields),rng=Mean(),now=clock.now())
    expected=aid if allowed else arms.controls()[family]
    assert chosen[family]==expected
    assert store.conn.execute('SELECT turns FROM bandit_share WHERE context=? AND arm_id=?',(key,expected)).fetchone()[0]==1


def test_frozen_family_cannot_retire_even_with_clear_excess(store,clock):
    for aid,verdict in (('length.0.v1','rewrite'),(arms.controls()['length'],'send')):
        for _ in range(20):
            lid,_=reward_line(store,clock,[aid],verdict=verdict); credit(store,lid,clock)
    assert bandit.retirement_candidates(store.conn,settings=ON)
    assert not bandit.retirement_candidates(store.conn,settings=replace(ON,frozen_families=('length',)))
