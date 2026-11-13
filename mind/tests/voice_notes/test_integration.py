import asyncio
from dataclasses import replace
from types import SimpleNamespace
import pytest

from conftest import run
from selfies.fakes import mind_app
from engine_rig import save_member
from discord.test_own_time import at
from leeroy_mind.discord import store as ds
from leeroy_mind.discord.talk import DEFERRED_KEY
from leeroy_mind.discord.presence import Presence
from leeroy_mind.endpoint import discord_args
from leeroy_mind.memory.privacy import derive_scope
from leeroy_mind.talk.pipeline import TurnMeta
from leeroy_mind.talk.turns import Turn,TurnKind
from leeroy_mind.tuning.effective import effective,MemberTuning
from leeroy_mind.voice import wire
from leeroy_mind.voice_notes import service,files
from voice_notes.test_files import wav,inventory


@pytest.mark.parametrize('success',[True,False])
def test_real_own_time_tick_never_answers_pending_placeholder(tmp_path,success):
    async def go():
        app=mind_app(tmp_path,toml='[voice_notes]\nenabled=true\n[talk]\ndm_own_time=true',
                     prepare=lambda store:save_member(store,101,region='US'))
        app.clock=app.discord_talk.app.clock=at(2026,10,30,3)
        queued=[];app.engine._queue=lambda items:queued.extend(items)
        entered,release=asyncio.Event(),asyncio.Event()
        async def request(msg):
            entered.set();await release.wait()
            return wire.Transcript(msg.id,success,"Don't tell anyone about the invented blue shed." if success else '',
                                   'ok' if success else 'engine_error')
        app.voice_notes.request=request
        try:
            prior=ds.post(app.store,channel='dm:101',author=app.human_guid,origin='human',text='An earlier question.',now=app.clock.now())
            app.discord_talk.on_his_post(prior)
            assert app.store.get_meta(DEFERRED_KEY.format(guid=101))==str(prior)
            token,_,_=files.stage(tmp_path,wav())
            result=await app.discord.handle(discord_args({'action':'post','channel':'dm:101','voice':token}))
            await entered.wait()
            app.clock=app.discord_talk.app.clock=at(2026,10,30,20)
            app.discord_talk.tick(app.clock.now())
            assert not queued
            assert app.store.get_meta(DEFERRED_KEY.format(guid=101))!=str(result['id'])
            release.set();await asyncio.gather(*tuple(app.voice_notes._tasks))
            app.discord_talk.tick(app.clock.now()+300)
            assert len(queued)==1 and queued[0][1].discord.reply_to==result['id']
            instruction=queued[0][0].instruction
            assert ('invented blue shed' in instruction) is success
            if not success:assert 'Do not invent its contents' in instruction
            assert app.discord_talk.instruction(102,'dm:102',result['id'],None) is None
            line=int(app.store.get_meta(f'discord:line:{result["id"]}'))
            scope=derive_scope(app.store.conn,101,[f'chat:{line}'])
            assert scope.scope==('secret' if success else 'private') and scope.scope_with==app.human_guid
            assert app.store.conn.execute('SELECT thread_id FROM chat_line WHERE id=?',(line,)).fetchone()[0]
        finally:await app.stop()
    run(go())


@pytest.mark.parametrize('outcome',['ok','timeout','down','lease_lost','scope_changed','caps','newer'])
def test_pipeline_records_note_or_sends_original_text(tmp_path,monkeypatch,outcome):
    async def go():
        app=mind_app(tmp_path,toml='[voice_notes]\nenabled=true\nshare=0.5')
        notes=app.voice_notes
        app.arbiter.hold('discord')
        app.voice=SimpleNamespace(specs={101:{}},client=SimpleNamespace(greeted=outcome!='down',
            status=wire.Status(False,'closed','none','parakeet',False,0,(),True),send=lambda msg:None))
        monkeypatch.setattr(service,'presence_of',lambda *a,**k:Presence('online',False,0))
        turn=Turn(TurnKind.REPLY_HUMAN,101,'dm:101',to_guid=app.human_guid)
        meta=TurnMeta('test-note')
        if outcome=='newer':
            meta.realism_followup=True
            meta.realism_sequence=app.engine.realism.member(101).sequence
        meta.still_valid=lambda:outcome!='scope_changed' or not made
        p=effective(app.personas.get(101),MemberTuning({'talkative':10}))
        text='Meet beside the invented old tree.'
        phases=[];app.engine._phase=lambda turn,phase:phases.append(phase)
        # Exercise the transport and final scope fence, without generation/typing waits.
        app.engine._blocked=lambda *a,**k:None
        app.engine.live_review.valid=lambda *a,**k:True
        made=[]
        async def request(msg):
            folder=tmp_path/'discord/voice';folder.mkdir(parents=True,exist_ok=True)
            path=folder/(msg.id+'.wav');path.write_bytes(wav());made.append(path)
            if outcome=='lease_lost':await app.arbiter.release_live('discord')
            if outcome=='caps':notes.config=replace(notes.config,guild_day=0)
            if outcome=='newer':app.engine.realism.member(101).sequence+=1
            return wire.Rendered(msg.id,True,path.relative_to(tmp_path).as_posix(),1000,len(wav()),'ok')
        if outcome=='timeout':monkeypatch.setattr(service,'TIMEOUT',.001)
        else:notes.request=request
        try:
            result=await app.engine._send_line(turn,meta,p,text,[],first=True)
            if outcome in ('scope_changed','newer'):
                assert result==('scope_changed' if outcome=='scope_changed' else 'realism_stale')
                assert app.store.conn.execute('SELECT count(*) FROM discord_message').fetchone()[0]==0
            else:
                assert result is None
                assert app.store.conn.execute('SELECT text FROM discord_message').fetchone()[0]==text
                assert app.store.conn.execute('SELECT text FROM chat_line WHERE origin="mind"').fetchone()[0]==text
                assert app.store.conn.execute('SELECT count(*) FROM discord_voice').fetchone()[0]==(outcome=='ok')
            assert ('recording' in phases)==(outcome!='down')
            assert all(path.exists() for path in made)
        finally:await app.stop()
    run(go())


def test_concurrent_transport_caps_commit_with_exactly_one_attachment(tmp_path,monkeypatch):
    async def go():
        app=mind_app(tmp_path,toml='[voice_notes]\nenabled=true\nshare=0.5\nper_member_day=1\nguild_day=1')
        app.arbiter.hold('discord')
        app.voice=SimpleNamespace(specs={101:{}},client=SimpleNamespace(greeted=True,
            status=wire.Status(False,'closed','none','parakeet',False,0,(),True)))
        monkeypatch.setattr(service,'presence_of',lambda *a,**k:Presence('online',False,0))
        entered=[];release=asyncio.Event()
        async def request(msg):
            entered.append(msg)
            if len(entered)==2:release.set()
            await release.wait()
            folder=tmp_path/'discord/voice';folder.mkdir(parents=True,exist_ok=True)
            path=folder/(msg.id+'.wav');path.write_bytes(wav())
            return wire.Rendered(msg.id,True,path.relative_to(tmp_path).as_posix(),1000,len(wav()),'ok')
        app.voice_notes.request=request
        app.engine._blocked=lambda *a,**k:None
        app.engine.live_review.valid=lambda *a,**k:True
        p=effective(app.personas.get(101),MemberTuning({'talkative':10}))
        try:
            results=await asyncio.gather(*(app.engine._send_line(
                Turn(TurnKind.REPLY_HUMAN,101,'dm:101',to_guid=app.human_guid),TurnMeta(f'note-{i}'),
                p,f'An invented sentence number {i}.',[],first=True) for i in (1,2)))
            assert results==[None,None] and len(entered)==2
            assert app.store.conn.execute('SELECT count(*) FROM discord_message').fetchone()[0]==2
            assert app.store.conn.execute('SELECT count(*) FROM discord_voice').fetchone()[0]==1
            counts=service.counts(app.store,app.clock.local().date().isoformat())
            assert counts['voice_notes']==counts['voice_note:101']==1
            assert len(list((tmp_path/'discord/voice').glob('*.wav')))==2
        finally:await app.stop()
    run(go())
