import asyncio
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import pytest
from conftest import run,seed_test_cast,make_persona
from discord.test_discord_desk import desk_app
from leeroy_mind.discord.presence import Presence
from leeroy_mind.endpoint import discord_args,BadRequest
from leeroy_mind.talk.pipeline import TurnMeta
from leeroy_mind.talk.turns import Turn,TurnKind
from leeroy_mind.tuning.effective import effective,MemberTuning
from leeroy_mind.voice import wire
from leeroy_mind.voice_notes import files,service
from leeroy_mind.voice_notes.config import VoiceNotesConfig
from leeroy_mind.server import records
from voice_notes.test_files import wav,inventory


def fixture(store,clock,monkeypatch):
    root=Path(store.conn.execute('PRAGMA database_list').fetchone()['file']).parent
    (root/'mind.toml').write_text('[voice_notes]\nenabled=true\nshare=0.5')
    seed_test_cast(store)
    seen=[]
    app=desk_app(store,clock,data_dir=root,engine=SimpleNamespace(),
        discord_talk=SimpleNamespace(on_his_post=lambda mid:seen.append(mid),begin_photo=lambda:None))
    app.voice=SimpleNamespace(specs={101:{}},client=SimpleNamespace(greeted=True,status=wire.Status(True,'closed','none','parakeet',False,0,()),send=lambda msg:None))
    monkeypatch.setattr(service,'presence_of',lambda *a,**k:Presence('online',False,0))
    p=effective(make_persona(101,'Arkon'),MemberTuning({'talkative':10}))
    turn=Turn(TurnKind.OPEN,101,'dm:101',to_guid=1)
    meta=TurnMeta('test-turn')
    return app,p,turn,meta,seen


def test_choice_stable_and_caps_share_server_day_without_losing_other_counts(store,clock,monkeypatch):
    app,p,t,m,_=fixture(store,clock,monkeypatch); notes=app.voice_notes
    assert all(notes.eligible(t,m,p,'An invented short message') for _ in range(10))
    day=clock.local().date().isoformat()
    store.set_meta('server:day:'+day,'{"messages":4}')
    with store.transaction():
        from leeroy_mind.discord.store import post
        mid=post(store,channel='dm:101',author=101,origin='mind',text='An invented short message',now=clock.now())
        notes.record(mid,101,wire.Rendered('a'*32,True,'discord/voice/'+'a'*32+'.wav',1000,100,'ok'))
        records.increment(store,day,messages=1)
    value=service.counts(store,day)
    assert value['messages']==5 and value['voice_notes']==value['voice_note:101']==1
    notes.config=replace(notes.config,per_member_day=1)
    assert not notes.eligible(t,m,p,'An invented short message')


@pytest.mark.parametrize('gate',['off','no_spec','down','engine','idle','short','long','item','emoji','channel','zero','bad_count','guild_cap','lease'])
def test_choice_conditions_fail_closed(store,clock,monkeypatch,gate):
    app,p,t,m,_=fixture(store,clock,monkeypatch); notes=app.voice_notes; text='An invented short message'
    if gate=='off':notes.config=VoiceNotesConfig()
    elif gate=='no_spec':app.voice.specs={}
    elif gate=='down':app.voice.client.greeted=False
    elif gate=='engine':app.voice.client.status=replace(app.voice.client.status,ok=False)
    elif gate=='idle':monkeypatch.setattr(service,'presence_of',lambda *a,**k:Presence('idle',False,0))
    elif gate=='short':text='Hi there'
    elif gate=='long':text='word '*61
    elif gate=='item':text='An item |Hitem:42|h[thing]|h'
    elif gate=='emoji':text='👍 👍 👍'
    elif gate=='channel':t=replace(t,channel='discord:general')
    elif gate=='zero':p=effective(p,MemberTuning({'talkative':0}))
    elif gate=='bad_count':store.set_meta('server:day:'+clock.local().date().isoformat(),'[]')
    elif gate=='guild_cap':notes.config=replace(notes.config,guild_day=0)
    elif gate=='lease':app.arbiter=SimpleNamespace(holders=set())
    assert not notes.eligible(t,m,p,text)


def test_offline_render_status_is_separate_from_room_output(store,clock,monkeypatch):
    app,p,t,m,_=fixture(store,clock,monkeypatch)
    app.voice.client.status=replace(app.voice.client.status,ok=False,render=True)
    assert app.voice_notes.eligible(t,m,p,'An invented short message')


def test_timeout_and_service_down_fall_back_without_audio(store,clock,monkeypatch):
    async def go():
        app,p,t,m,_=fixture(store,clock,monkeypatch)
        monkeypatch.setattr(service,'TIMEOUT',.001)
        before=inventory(app.data_dir/'discord')
        assert await app.voice_notes.prepare(t,m,p,'An invented short message') is None
        assert not app.voice_notes._pending
        assert inventory(app.data_dir/'discord')==before
    run(go())


@pytest.mark.parametrize('success',[True,False])
def test_his_transcript_is_private_and_only_then_handed_to_answers(store,clock,monkeypatch,success):
    async def go():
        app,_,_,_,seen=fixture(store,clock,monkeypatch)
        entered,release=asyncio.Event(),asyncio.Event()
        async def request(msg):
            entered.set(); await release.wait()
            return wire.Transcript(msg.id,success,"Don't tell anyone: an invented secret." if success else '', 'ok' if success else 'engine_error')
        app.voice_notes.request=request
        token,_,_=files.stage(app.data_dir,wav()); before=inventory(app.data_dir/'discord')
        args=discord_args({'action':'post','channel':'dm:101','voice':token})
        posted=await app.discord.handle(args); assert posted['ok']
        await entered.wait()
        assert not seen and store.get_meta('discord:dm:deferred:101') is None
        assert store.conn.execute('SELECT text FROM discord_message').fetchone()[0]==service.PLACEHOLDER
        release.set(); await asyncio.gather(*tuple(app.voice_notes._tasks))
        assert seen==[posted['id']]
        row=store.conn.execute('SELECT channel,to_guid,text FROM chat_line').fetchone()
        assert row['channel']=='dm:101' and row['to_guid']==101
        assert ("Don't tell" in row['text']) is success
        assert bool(store.conn.execute('SELECT transcript_ok FROM discord_voice').fetchone()[0]) is success
        from leeroy_mind.memory.privacy import derive_scope
        lid=store.conn.execute('SELECT id FROM chat_line').fetchone()[0]
        assert derive_scope(store.conn,101,[f'chat:{lid}']).scope==('secret' if success else 'private')
        await app.discord.handle({'action':'delete','message':posted['id']})
        assert all(p.read_bytes()==data for p,data in before.items())
        assert len(inventory(app.data_dir/'discord'))>=len(before)
        await app.voice_notes.close()
    run(go())


@pytest.mark.parametrize('when',['pending','ready'])
def test_shutdown_recovery_hands_terminal_note_once(store,clock,monkeypatch,when):
    async def go():
        app,_,_,_,seen=fixture(store,clock,monkeypatch)
        started=asyncio.Event()
        async def request(msg):
            started.set()
            if when=='pending':await asyncio.Event().wait()
            return wire.Transcript(msg.id,True,'An invented private sentence.','ok')
        app.voice_notes.request=request
        hook=app.discord_talk.on_his_post
        if when=='ready':
            app.discord_talk.on_his_post=lambda mid: (_ for _ in ()).throw(RuntimeError('synthetic handoff failure'))
        token,_,_=files.stage(app.data_dir,wav())
        result=await app.discord.handle(discord_args({'action':'post','channel':'dm:101','voice':token}))
        await started.wait()
        if when=='ready':await asyncio.gather(*tuple(app.voice_notes._tasks),return_exceptions=True)
        before=inventory(app.data_dir/'discord')
        await app.voice_notes.close()
        assert not seen
        assert store.get_meta(service.PENDING_KEY+str(result['id']))==when
        config=app.voice_notes.config
        app.voice_notes=service.VoiceNotes(app,config)
        app.discord_talk.on_his_post=hook
        calls=[]
        async def recovered(msg):
            calls.append(msg)
            return wire.Transcript(msg.id,True,'An invented recovered sentence.','ok')
        app.voice_notes.request=recovered
        app.voice_notes.tick()
        await asyncio.gather(*tuple(app.voice_notes._tasks))
        app.voice_notes.tick()
        assert seen==[result['id']]
        assert bool(calls)==(when=='pending')
        assert store.get_meta(service.PENDING_KEY+str(result['id'])) is None
        assert inventory(app.data_dir/'discord')==before
        await app.voice_notes.close()
    run(go())


@pytest.mark.parametrize('mode',['disconnected','refused'])
def test_disconnected_requests_fail_without_wait(store,clock,monkeypatch,mode):
    async def go():
        app,_,_,_,_=fixture(store,clock,monkeypatch)
        if mode=='disconnected':app.voice.client.greeted=False
        else:app.voice.client.send=lambda msg:False
        assert await asyncio.wait_for(app.voice_notes.request(wire.Transcribe('a'*32,'discord/voice/'+'b'*32+'.wav')),.1) is None
        assert not app.voice_notes._pending
    run(go())


def test_voice_post_parser_accepts_no_caption_and_refuses_channels():
    args={'action':'post','channel':'dm:101','voice':'a'*32}
    assert discord_args(args)['text']==service.PLACEHOLDER
    assert discord_args(args|{'text':'ignored'})['text']==service.PLACEHOLDER
    for extra in ({'channel':'general'},{'image':'b'*32},{'voice':'../file'}):
        with pytest.raises(BadRequest):discord_args(args|extra)
