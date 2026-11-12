import asyncio
import json
import logging
from dataclasses import replace
from types import SimpleNamespace as NS

import pytest

from tests.claims.conftest import context
from tests.learn.test_coach import answer, note, enabled, exchanges
from leeroy_mind.learn import coach, lessons
from leeroy_mind.tuning.effective import effective, MemberTuning
from leeroy_mind.nightly.llm import system_prompt, INVARIANTS


@pytest.mark.parametrize('text', ['She has a crush on him.', 'She is in love with him.',
    'She asks him out on a date.', 'She calls him babe and sweetheart.'])
@pytest.mark.parametrize('forward,member,world', [(0,5,5),(5,0,5),(5,5,0),(5,5,5)])
def test_romance_uses_real_dials(context,text,forward,member,world):
    async def go():
        async with context() as (ctx,_):
            p=ctx.persona(101)
            p=replace(p,identity=replace(p.identity,player_gender='female'),
                      romance=replace(p.romance,stance='drawn',dial=member))
            p=effective(p,MemberTuning({'forward':forward}))
            clean=lessons.screen(note(text),scope='guild',persona=p,ident=ctx.ident,world_romance=world)
            assert (clean is not None)==(min(forward,member,world)>0)
    asyncio.run(go())


@pytest.mark.parametrize('text', ['She gets lewd and horny.', 'She uses dirty talk.',
    'She uses suggestive innuendo.', 'She sulks until he stays online.',
    'She begs him not to log off.', 'She threatens to quit.', 'She threatens to leave the guild.',
    'She makes him feel bad when he logs off.', 'She says the others do not care about him.'])
def test_content_and_retention_screens(context,text):
    async def go():
        async with context() as (ctx,_):
            assert lessons.screen(note(text),scope='guild',persona=ctx.persona(101),ident=ctx.ident) is None
    asyncio.run(go())


def test_false_attestation_preserves_other_lesson(context,tmp_path,caplog):
    enabled(tmp_path)
    async def go():
        async with context(lambda *_:json.dumps(answer([note(non_manipulative=False),
                    note('He asks one short question.')])),learn_enabled=True,learn_coach=True) as (ctx,llm):
            exchanges(ctx)
            with caplog.at_level(logging.INFO):
                await coach.stage(ctx)
            assert len(llm.calls)==1 and ctx.done(coach.STAGE,'101')
            assert [x['text'] for x in lessons.read(ctx.store.conn,now=ctx.clock.now())]==['He asks one short question.']
            assert 'talk_coach_lesson_refused' in caplog.text and 'manipulative' in caplog.text
    asyncio.run(go())


def test_rotation_skips_ineligible_before_cap(context,tmp_path,monkeypatch,clock):
    enabled(tmp_path)
    monkeypatch.setattr(coach,'CAP',1)
    async def go():
        seen=[]
        for day in range(3):
            async with context(lambda *_:json.dumps(answer()),night=f'2026-11-{10+day}',
                               learn_enabled=True,learn_coach=True) as (ctx,llm):
                if day==0:
                    exchanges(ctx,101,channel='officer')
                    for guid in (102,103): exchanges(ctx,guid)
                if day==2: exchanges(ctx,101,channel='guild')
                seen+=coach.candidates(ctx)
                await coach.stage(ctx)
                assert len(llm.calls)==1
            await clock.advance(60)
        assert seen==[102,103,101]
    asyncio.run(go())


def test_drop_and_reproposal_cannot_reset_lifetime(store,clock):
    now=clock.now()
    lessons.persist(store.conn,101,answer(),known=set(),night='original',now=now,automatic=False)
    ident=lessons.read(store.conn,now=now)[0]['id']
    lessons.persist(store.conn,101,answer(drop=[ident]),known={ident},night='later',now=now+10,automatic=True)
    row=store.conn.execute('SELECT origin_night,expires FROM talk_lesson WHERE id=?',(ident,)).fetchone()
    assert tuple(row)==('original',now+10)


def test_named_context_overlay_and_fitted_discord_payload(context,tmp_path,monkeypatch):
    enabled(tmp_path)
    prompts=tmp_path/'prompts'; prompts.mkdir()
    (prompts/'nightly_talk_coach.txt').write_text('Private coach overlay.')
    monkeypatch.setattr(coach,'STRUCTURE',NS(options={'num_ctx':4000}))
    async def go():
        async with context(lambda *_:json.dumps(answer()),learn_enabled=True,learn_coach=True) as (ctx,llm):
            exchanges(ctx,channel='discord:general')
            ctx.store.conn.execute('UPDATE chat_line SET text=?',('long discord evidence '*100,))
            data=coach.evidence(ctx,101)
            assert {x['speaker'] for e in data[2] for x in e['context']}=={'Ownerly','Ashlen'}
            shape=coach.response_schema(False)
            system=system_prompt('talk_coach',coach.PROMPT,tmp_path,human=ctx.ident)
            payload={'exchanges':data[2]}
            fitted=json.loads(coach.fit_payload(payload,system,shape))
            assert 0<len(fitted['exchanges'])<len(data[2])
            assert fitted['exchanges'][-1]['ts']==max(e['ts'] for e in data[2])
            await coach.stage(ctx)
            assert len(llm.calls)==1
            assert 'Private coach overlay.' in str(llm.calls) and 'Nobody ever actually leaves' in str(llm.calls)
    asyncio.run(go())


def test_proposal_dedup_any_status_and_night_cap(store,clock):
    rows=[NS(family='length',params={'min_words':i},status=s)
          for i,s in enumerate(('active','retired','proposed','control'))]
    registry=NS(read=lambda _:rows,propose=lambda _,family,params,**kw:rows.append(NS(family=family,params=params)))
    for i in range(12):
        coach.propose_arm(store,registry,{'family':'length','params':{'min_words':i}},'night1',clock.now())
    assert len(rows)==9
    assert coach.propose_arm(store,registry,{'family':'length','params':{'min_words':12}},'night2',clock.now())


def test_real_runner_orders_and_budgets_coach(context,tmp_path):
    from leeroy_mind.nightly.runner import NightRunner,Stage
    enabled(tmp_path)
    async def go():
        async with context(lambda *_:json.dumps(answer()),learn_enabled=True,learn_coach=True) as (ctx,llm):
            exchanges(ctx)
            events=[]
            async def rewards(c): events.append('rewards')
            async def after(c):
                assert c.done(coach.STAGE,'101')
                events.append('after')
            runner=NightRunner([Stage('rewards',rewards,None),Stage('after',after,None)],lambda *_:ctx)
            coach.attach(runner,tmp_path,ctx.store)
            llm.set_models([ctx.settings.reflection_model])
            await runner.run(ctx.night,ctx.window)
            assert events==['rewards','after']
            assert ctx.plan['units'][coach.STAGE]==1
    asyncio.run(go())
