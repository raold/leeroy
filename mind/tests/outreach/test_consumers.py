"""Outreach follows actual pipeline delivery, keeping one pair's cross-medium history."""
import asyncio
from dataclasses import replace
import hashlib
from types import SimpleNamespace

import pytest

from conftest import HUMAN, run
from engine_rig import engine_clock, make_rig
from photos.test_pipeline_photo import finish
from selfies.fakes import ready_snap
from server.test_dms import fixture as dm_fixture, due
from tuning.scenario import GOLDEN, golden, record
from leeroy_mind.outreach import store as records
from leeroy_mind.outreach.service import Outreach
from leeroy_mind.outreach.attach import attach
from leeroy_mind.outreach.sources import BIRTHDAY_ABOUT, member_local
from leeroy_mind.bridge import AckTimeout
from leeroy_mind.discord import store as ds
from leeroy_mind.discord.store import AttachmentIn
from leeroy_mind.discord.transport import DiscordTarget
from leeroy_mind.guards import Verdict
from leeroy_mind.personas.io import save_persona
from leeroy_mind.photos.offer import PhotoOffer
from leeroy_mind.protocol import Chat, Group
from leeroy_mind.selfies import files, snaps
from leeroy_mind.talk.pipeline import ECHO_WAIT_S, TurnMeta
from leeroy_mind.talk.turns import Turn, TurnKind


TOPIC = 'I am planting rosemary in a blue pot this weekend.'


def app_for(rig, tmp_path, *, owes=lambda guid:False):
    app = SimpleNamespace(engine=rig.engine, store=rig.store, clock=rig.clock,
        world=rig.world, personas=rig.engine.personas, human_guid=HUMAN, data_dir=tmp_path,
        discord_talk=SimpleNamespace(owes=owes), stopping=False,
        register_event_handler=lambda *a,**k:None, register_connection_hook=lambda *a:None,
        register_shutdown_drain=lambda *a:None, is_backlog=lambda ev:False,
        embedder=rig.engine.recall._embedder)
    app.outreach = Outreach(app)
    return app


@pytest.fixture
def clock():
    return engine_clock()


@pytest.fixture
def rig(store,clock,tmp_path):
    rig = make_rig(store,clock,tmp_path,reply='the rosemary pot is ready for the weekend')
    store.set_meta('human_guid',str(HUMAN))
    yield rig
    rig.close()


def plan(rig, *, guid=101, about=TOPIC, days=1):
    store, now = rig.store, rig.clock.now()
    source = store.insert_chat_line(Chat(ts=int((now-30)*1000),channel='whisper',
        from_guid=guid,from_name='Arkon',to_guid=HUMAN,text=about,map=0,zone=10),
        session_id=None,origin='mind',audience=(guid,HUMAN))
    store.log_utterance(now,guid,'whisper','open',about,[],None,None,to_guid=HUMAN)
    cid = store.conn.execute('INSERT INTO member_claim(guid,kind,subject,text,polarity,source,'
        'source_line,first_seen,last_seen,expires,embedding) '
        "VALUES (?,'plan','weekend',?,1,'chat',?,?,?,?,zeroblob(2048))",
        (guid,about,source,now-30,now-30,now+days*86400)).lastrowid
    item = records.add(store,guid=guid,kind='plan',ref=f'plan:{cid}',about=about,
        audience='pair',weight=2,created=now,not_before=now,expires=now+days*86400)
    assert item is not None
    return item


def bind(app,item,via,*,turn_id='outreach-test'):
    channel = f'dm:{item.guid}' if via=='dm' else via
    turn = Turn(TurnKind.WHISPER if via=='whisper' else TurnKind.OPEN,item.guid,channel,
                to_guid=HUMAN,addressee=HUMAN,max_lines=1)
    meta = TurnMeta(turn_id,why='outreach:test',initiates=True)
    assert app.outreach.bind(turn,meta,item)
    return turn,meta


def speak(rig,turn,meta):
    async def go():
        return await finish(rig,asyncio.create_task(rig.engine.speak_meta(turn,meta)))
    return rig.sync(go())


@pytest.mark.parametrize('first,other',[('whisper','dm'),('dm','whisper')])
def test_real_delivery_tells_once_across_game_and_discord(rig,tmp_path,first,other):
    app = app_for(rig,tmp_path)
    item = plan(rig)
    assert app.outreach.select(101,first).id == item.id
    turn, meta = bind(app,item,first)
    before = (rig.engine._last_talk_at, dict(rig.engine._answered_at), len(rig.world.history))
    lines = speak(rig,turn,meta)
    assert len(lines)==1 and meta.said==1
    told = records.get(rig.store,item.id)
    assert (told.state,told.told_via,told.ended)==('told',first,'told')
    line = rig.store.chat_lines([told.line_id])[0]
    assert line.text==lines[0] and line.from_guid==101 and line.to_guid==HUMAN
    assert line.channel==turn.channel and TOPIC in rig.prompts()[-1]
    assert app.outreach.select(101,other) is None and not app.outreach._held
    if first=='dm':
        assert not rig.sent.says()
        assert line.session_id is None
        assert (rig.engine._last_talk_at,dict(rig.engine._answered_at),len(rig.world.history))==before
        post = rig.store.conn.execute('SELECT id,text FROM discord_message WHERE turn=?',
                                     (meta.turn_id,)).fetchone()
        assert post['text']==lines[0]
        assert rig.store.get_meta(f'discord:line:{post["id"]}')==str(told.line_id)
    else:
        assert len(rig.sent.says())==1 and rig.sent.says()[0].channel=='whisper'
        assert not rig.store.conn.execute('SELECT id FROM discord_message').fetchall()


@pytest.mark.parametrize('via',['whisper','dm'])
def test_dropped_draft_keeps_item_open_and_releases_reservation(rig,tmp_path,monkeypatch,via):
    app = app_for(rig,tmp_path)
    item = plan(rig)
    turn,meta = bind(app,item,via)
    monkeypatch.setattr(rig.engine,'_blocked',lambda *a,**k:'synthetic_guard')
    assert speak(rig,turn,meta)==[]
    assert records.get(rig.store,item.id).state=='open'
    assert app.outreach.select(101,via).id==item.id and not app.outreach._held
    assert not rig.sent.says() and not rig.chat_calls()
    assert not rig.store.conn.execute('SELECT id FROM discord_message').fetchall()


def test_native_refusal_leaves_item_open(rig,tmp_path):
    app = app_for(rig,tmp_path)
    item = plan(rig)
    turn,meta = bind(app,item,'whisper')
    rig.sent.ack = lambda command:(False,'synthetic refusal')
    assert speak(rig,turn,meta)==[]
    assert records.get(rig.store,item.id).state=='open' and meta.said==0
    assert not app.outreach._held


@pytest.mark.parametrize('confirmed',[False,True])
def test_lost_native_ack_reserves_until_echo_or_settlement(rig,tmp_path,confirmed):
    app = app_for(rig,tmp_path)
    item = plan(rig)
    turn,meta = bind(app,item,'whisper')
    def timeout(command):
        raise AckTimeout('synthetic lost acknowledgement')
    rig.sent.ack=timeout
    assert speak(rig,turn,meta)==[]
    assert item.id in app.outreach._held and rig.engine._unacked
    assert records.get(rig.store,item.id).state=='open'
    assert app.outreach.select(101,'dm') is None
    if confirmed:
        echo=Chat(ts=0,channel='whisper',from_guid=101,from_name='Arkon',to_guid=HUMAN,
                  text=rig.sent.says()[0].text,map=0,zone=10,via_bridge=True)
        assert rig.engine.is_echo(echo,rig.clock.now())
        told=records.get(rig.store,item.id)
        assert told.state=='told' and told.told_via=='whisper' and told.line_id
        assert app.outreach.select(101,'dm') is None
    else:
        rig.engine._settle_unacked(rig.clock.now()+ECHO_WAIT_S+1)
        assert records.get(rig.store,item.id).state=='open'
        assert app.outreach.select(101,'dm').id==item.id
    assert not app.outreach._held and not rig.engine._unacked


def test_failed_late_echo_record_releases_its_reservation(rig,tmp_path,monkeypatch):
    app = app_for(rig,tmp_path)
    item = plan(rig)
    turn,meta = bind(app,item,'whisper')
    def timeout(command):
        raise AckTimeout('synthetic lost acknowledgement')
    rig.sent.ack=timeout
    assert speak(rig,turn,meta)==[]
    assert item.id in app.outreach._held
    def fault(*args,**kwargs):
        raise RuntimeError('synthetic record fault')
    monkeypatch.setattr(rig.engine,'_record',fault)
    echo=Chat(ts=0,channel='whisper',from_guid=101,from_name='Arkon',to_guid=HUMAN,
              text=rig.sent.says()[0].text,map=0,zone=10,via_bridge=True)
    assert rig.engine.is_echo(echo,rig.clock.now())
    assert not app.outreach._held and not rig.engine._unacked
    assert records.get(rig.store,item.id).state=='open'
    assert app.outreach.select(101,'dm').id==item.id


@pytest.mark.parametrize('via',['whisper','dm'])
def test_receipt_fault_rolls_back_line_and_post_together(rig,tmp_path,monkeypatch,via):
    app = app_for(rig,tmp_path)
    item = plan(rig)
    turn,meta = bind(app,item,via)
    original = records.told
    def fault(*args,**kwargs):
        assert original(*args,**kwargs)
        raise RuntimeError('synthetic receipt fault')
    monkeypatch.setattr(records,'told',fault)
    before = rig.store.conn.execute('SELECT count(*) FROM chat_line').fetchone()[0]
    assert speak(rig,turn,meta)==[]
    assert records.get(rig.store,item.id).state=='open'
    assert rig.store.conn.execute('SELECT count(*) FROM chat_line').fetchone()[0]==before
    assert not rig.store.conn.execute('SELECT id FROM discord_message').fetchall()
    assert meta.said==0 and not rig.engine._words and not app.outreach._held


@pytest.mark.parametrize('channel',['guild','party','officer','discord:general',
                                    'discord:officer-chat','gdm:1','dm:102'])
def test_pair_item_cannot_bind_to_a_wider_or_different_audience(rig,tmp_path,channel):
    app = app_for(rig,tmp_path)
    item = plan(rig)
    turn = Turn(TurnKind.OPEN,101,channel,to_guid=HUMAN,addressee=HUMAN)
    meta = TurnMeta('privacy')
    with pytest.raises(AssertionError):
        app.outreach.bind(turn,meta,item)
    assert meta.outreach_item is None and not meta.topic and not app.outreach._held
    assert not rig.chat_calls()


@pytest.mark.parametrize('changed',['channel','addressee','speaker','to_guid'])
def test_pair_binding_is_rechecked_before_private_instruction(rig,tmp_path,changed):
    app = app_for(rig,tmp_path)
    item = plan(rig)
    turn,meta = bind(app,item,'whisper')
    setattr(turn,changed,{'channel':'guild','addressee':102,'speaker':102,'to_guid':102}[changed])
    with pytest.raises(AssertionError):
        app.outreach.instruction(turn,meta,'ordinary instruction')


@pytest.mark.parametrize('reason',['owes','chattiness'])
def test_bound_turn_drops_when_answer_is_owed_or_chattiness_becomes_zero(rig,tmp_path,reason):
    owed = set()
    app = app_for(rig,tmp_path,owes=lambda guid:guid in owed)
    item = plan(rig)
    turn,meta = bind(app,item,'whisper')
    if reason=='owes':
        owed.add(101)
    else:
        rig.engine.cfg = replace(rig.engine.cfg,talk=replace(rig.engine.cfg.talk,chattiness=0))
    assert app.outreach.select(101,'whisper') is None
    assert speak(rig,turn,meta)==[]
    assert records.get(rig.store,item.id).state=='open' and not rig.chat_calls()


def test_whisper_consumer_uses_item_and_public_opener_keeps_pair_topic_out(rig,tmp_path,monkeypatch):
    app = app_for(rig,tmp_path)
    item = plan(rig)
    queued=[]
    monkeypatch.setattr(rig.engine,'_queue',queued.extend)
    rig.engine._queue_whisper(101,'start',rig.clock.now())
    assert len(queued)==1
    turn,meta = queued[0]
    assert meta.outreach_item==item.id and meta.single_line and meta.recall_query==TOPIC
    assert TOPIC not in meta.topic+turn.instruction
    app.outreach.release(meta)
    queued.clear()
    monkeypatch.setattr(rig.engine,'_candidates',lambda *a,**k:[101])
    async def capture(turn,meta):
        queued.append((turn,meta))
        return []
    monkeypatch.setattr(rig.engine,'_speak',capture)
    rig.sync(rig.engine._open_to_him())
    assert len(queued)==1 and queued[0][0].channel=='guild'
    assert queued[0][1].outreach_item is None and TOPIC not in queued[0][1].topic


@pytest.mark.parametrize('reason',['owes','chattiness'])
def test_game_consumer_does_not_fall_back_to_a_topic_when_ineligible(rig,tmp_path,monkeypatch,reason):
    app = app_for(rig,tmp_path,owes=lambda guid:reason=='owes')
    item = plan(rig)
    if reason=='chattiness':
        rig.engine.cfg = replace(rig.engine.cfg,talk=replace(rig.engine.cfg.talk,chattiness=0))
    queued=[]
    monkeypatch.setattr(rig.engine,'_queue',queued.extend)
    rig.engine._queue_whisper(101,'start',rig.clock.now())
    assert not queued and records.get(rig.store,item.id).state=='open'


@pytest.mark.parametrize('reason',['owes','chattiness','checking'])
def test_dm_consumer_skips_ineligible_member_before_legacy_fallback(store,clock,monkeypatch,reason):
    opener,snap,calls = dm_fixture(store,clock,monkeypatch)
    snap.personas = {101:snap.personas[101]}
    if reason=='chattiness':
        opener.app.engine.cfg.talk.chattiness=0
    if reason=='owes':
        opener.app.discord_talk=SimpleNamespace(owes=lambda guid:True)
    opener.app.outreach=SimpleNamespace(eligible=lambda guid:False,
        select=lambda *a:pytest.fail('ineligible member reached topic selection'))
    run(due(opener,clock))
    assert not calls


def birthday(rig):
    p=rig.engine.personas.get(101)
    local=member_local(rig.clock.local(),p)
    p=replace(p,real_life=replace(p.real_life,birthday=local.strftime('%m-%d')))
    save_persona(rig.store,p,source='seed',ts=rig.clock.now(),race='Orc',class_name='Warrior')
    rig.engine.personas.invalidate(101)
    return records.add(rig.store,guid=101,kind='birthday',ref=f'birthday:{local.date()}',
        about=BIRTHDAY_ABOUT,audience='guild',weight=5,created=rig.clock.now(),
        not_before=rig.clock.now(),expires=rig.clock.now()+3600)


def dm_opener(rig,app,monkeypatch):
    opener,snap,_=dm_fixture(rig.store,rig.clock,monkeypatch)
    opener.app=app
    app.guild=None
    app.server=SimpleNamespace(lease=SimpleNamespace(ready=lambda:True),reactions=None,dms=opener)
    rig.engine.server_app=app
    rig.world.human_online=False
    rig.engine.live_review.config=replace(rig.engine.live_review.config,enabled=True)
    snap.personas={101:rig.engine.personas.get(101)}
    # Content quality is tested separately; this case isolates real source,
    # selection, pipeline, transport and transactional receipt wiring.
    async def approve(*args,**kwargs):
        return Verdict('send')
    monkeypatch.setattr(rig.engine.live_review,'check',approve)
    def open_once():
        async def go():
            await finish(rig,asyncio.create_task(due(opener,rig.clock)))
        rig.sync(go())
    return opener,open_once


def test_real_dm_opener_delivers_selected_item_through_pipeline(rig,tmp_path,monkeypatch):
    app=app_for(rig,tmp_path)
    item=plan(rig)
    opener,open_once=dm_opener(rig,app,monkeypatch)
    try:
        open_once()
    finally:
        rig.sync(opener.close())
    told=records.get(rig.store,item.id)
    assert told.state=='told' and told.told_via=='dm'
    assert TOPIC in rig.prompts()[-1]
    assert len(rig.store.conn.execute('SELECT id FROM discord_message').fetchall())==1
    assert not rig.sent.says() and not app.outreach._held
    assert app.outreach.select(101,'whisper') is None


def test_real_public_opener_uses_guild_item_and_records_its_delivery(rig,tmp_path,monkeypatch):
    app=app_for(rig,tmp_path)
    item=birthday(rig)
    monkeypatch.setattr(rig.engine,'_candidates',lambda *a,**k:[101])
    rig.script('another birthday means another excuse for cake')
    async def go():
        await finish(rig,asyncio.create_task(rig.engine._open_to_him()))
    rig.sync(go())
    told=records.get(rig.store,item.id)
    assert told.state=='told' and told.told_via=='guild'
    assert BIRTHDAY_ABOUT in rig.prompts()[-1]
    assert len(rig.sent.says())==1 and rig.sent.says()[0].channel=='guild'
    assert app.outreach.select(101,'dm') is None


@pytest.mark.parametrize('channel',['guild','party'])
def test_public_item_stays_typed_while_she_sits_in_his_voice_room(rig,tmp_path,monkeypatch,channel):
    from voice.conftest import VRig
    app=app_for(rig,tmp_path)
    voice=VRig(rig)
    voice.him('guild_hall')
    voice.seat(101,'guild_hall')
    if channel=='party':
        rig.sync(rig.event(Group(ts=0,leader_guid=HUMAN,raid=False,members=(HUMAN,101))))
    item=birthday(rig)
    monkeypatch.setattr(rig.engine,'_candidates',lambda *a,**k:[101])
    rig.script('another birthday means another excuse for cake')
    async def go():
        await finish(rig,asyncio.create_task(rig.engine._open_to_him()))
    rig.sync(go())
    told=records.get(rig.store,item.id)
    assert told.state=='told' and told.told_via==channel
    assert rig.store.chat_lines([told.line_id])[0].channel==channel
    assert [s.channel for s in rig.sent.says()]==[channel] and not voice.speaks()
    assert list(rig.engine._to_him_at)==[101]
    assert [tuple(r) for r in rig.store.conn.execute('SELECT channel,dropped FROM utterance WHERE guid=101')]==[(channel,None)]
    assert not app.outreach._held and app.outreach.select(101,'whisper') is None


def him_photo(rig,tmp_path):
    """A ready `him` snap of 101's, offered as PhotoSender offers one: to a DM opener or an
    answer to his DM, while it is still unsent."""
    sid=ready_snap(rig.store,tmp_path,101,audience='him')
    snap=snaps.ready_for(rig.store,101,now=rig.clock.now(),phase='evening')[0]
    path=files.snap(tmp_path,101,sid)
    asked=[]
    def offer(turn,meta,now):
        asked.append(meta.why)
        status=rig.store.conn.execute('SELECT status FROM selfie_snap WHERE id=?',(sid,)).fetchone()[0]
        if meta.why not in ('server:dm','discord:reply') or status!='ready':
            return None
        def sent(line,mid):
            assert snaps.mark_sent(rig.store,sid,sender=101,channel='dm',line_id=line,now=rig.clock.now())
        return PhotoOffer(snap,'You are posting a photo of an invented garden. Write a short caption.',
            sent,lambda:None,AttachmentIn('snap',path.relative_to(tmp_path).as_posix(),832,1216,path.stat().st_size,sid))
    rig.engine.attach_photos(offer)
    return sid,asked


def attached(rig):
    return [tuple(r) for r in rig.store.conn.execute('SELECT m.id,a.snap FROM discord_message m '
        'LEFT JOIN discord_attachment a ON a.message=m.id ORDER BY m.id')]


def test_dm_opener_sends_her_photo_first_and_tells_the_item_on_a_later_opener(rig,tmp_path,monkeypatch):
    app=app_for(rig,tmp_path)
    item=plan(rig,days=3)
    sid,asked=him_photo(rig,tmp_path)
    opener,open_once=dm_opener(rig,app,monkeypatch)
    rig.script('the garden finally looks like something','the rosemary pot is ready for the weekend')
    told_here='This is the one thing you mean to tell him now'
    try:
        open_once()
        assert asked==['server:dm'] and [snap for _,snap in attached(rig)]==[sid]
        assert 'invented garden' in rig.prompts()[-1] and told_here not in rig.prompts()[-1]
        assert records.get(rig.store,item.id).state=='open' and not app.outreach._held
        rig.sync(rig.clock.advance(49*3600,49*3600))
        open_once()
    finally:
        rig.sync(opener.close())
    assert asked==['server:dm','server:dm'] and [snap for _,snap in attached(rig)]==[sid,None]
    told=records.get(rig.store,item.id)
    assert told.state=='told' and told.told_via=='dm'
    assert 'invented garden' not in rig.prompts()[-1] and told_here+', in your own words: '+TOPIC in rig.prompts()[-1]
    assert not app.outreach._held


def test_answer_to_his_dm_keeps_its_photo_beside_an_open_item(rig,tmp_path):
    app=app_for(rig,tmp_path)
    item=plan(rig)
    sid,asked=him_photo(rig,tmp_path)
    mid=ds.post(rig.store,channel='dm:101',author=HUMAN,origin='human',text='how is your day?',now=rig.clock.now())
    turn=Turn(TurnKind.REPLY_HUMAN,101,'dm:101',to_guid=HUMAN,addressee=HUMAN)
    lines=speak(rig,turn,TurnMeta('reply',why='discord:reply',discord=DiscordTarget(mid,None)))
    assert lines and asked==['discord:reply'] and attached(rig)[-1][1]==sid
    assert records.get(rig.store,item.id).state=='open' and not app.outreach._held


@pytest.mark.parametrize('consumer',['whisper','guild','dm'])
def test_each_consumer_puts_the_item_text_in_its_prompt_once(rig,tmp_path,monkeypatch,consumer):
    app=app_for(rig,tmp_path)
    item=birthday(rig)
    rig.script('another birthday means another excuse for cake')
    if consumer=='dm':
        opener,open_once=dm_opener(rig,app,monkeypatch)
        try:
            open_once()
        finally:
            rig.sync(opener.close())
    else:
        queued=[]
        monkeypatch.setattr(rig.engine,'_queue',queued.extend)
        monkeypatch.setattr(rig.engine,'_candidates',lambda *a,**k:[101])
        if consumer=='whisper':
            rig.engine._queue_whisper(101,'start',rig.clock.now())
            speak(rig,*queued[0])
        else:
            async def go():
                await finish(rig,asyncio.create_task(rig.engine._open_to_him()))
            rig.sync(go())
    told=records.get(rig.store,item.id)
    assert told.state=='told' and told.told_via==consumer
    assert rig.prompts()[-1].count(BIRTHDAY_ABOUT)==1


def test_off_keeps_seeded_dm_speakers_and_topics(store,tmp_path,monkeypatch):
    (tmp_path/'mind.toml').write_text('[outreach]\nenabled=false\n',encoding='utf-8')
    disabled=attach(SimpleNamespace(data_dir=tmp_path))
    assert disabled is None
    async def sequence(explicit_off):
        clock=engine_clock()
        opener,snap,_=dm_fixture(store,clock,monkeypatch)
        if explicit_off:
            opener.app.outreach=disabled
        snap.seeds=[SimpleNamespace(kind='life',about=(101,),fact='The harbor walk went well.',
                                    officers_only=False)]
        choices=[]
        async def capture(turn,meta):
            assert meta.still_valid()
            choices.append((turn.speaker,turn.channel,turn.instruction,meta.topic,meta.outreach_item))
            return []
        opener.app.engine.speak_meta=capture
        for _ in range(4):
            await due(opener,clock)
            await clock.advance(121,121)
        await opener.close()
        return choices
    assert run(sequence(False))==run(sequence(True))


def test_off_keeps_seeded_game_topic_choices_with_retained_items(rig,tmp_path,monkeypatch):
    plan(rig)
    other_path=tmp_path/'off'
    other_path.mkdir()
    (other_path/'mind.toml').write_text('[outreach]\nenabled=false\n',encoding='utf-8')
    assert attach(SimpleNamespace(data_dir=other_path)) is None
    other=make_rig(rig.store,engine_clock(),other_path,seed=1)
    def choices(engine):
        selected=[]
        engine._queue=selected.extend
        for guid in (101,102,103,101,104):
            engine._queue_whisper(guid,'start',engine.clock.now())
        return [(t.speaker,t.delay,t.instruction,m.topic,m.outreach_item) for t,m in selected]
    try:
        assert choices(rig.engine)==choices(other.engine)
        assert all(m is None for *_,m in choices(other.engine))
    finally:
        other.close()


@pytest.mark.parametrize('config',['[outreach]\nenabled=false\n','[outreach]\nenabled="invalid"\n'])
def test_off_preserves_golden_prompts_and_golden_file(store,clock,tmp_path,config):
    (tmp_path/'mind.toml').write_text(config,encoding='utf-8')
    assert attach(SimpleNamespace(data_dir=tmp_path)) is None
    before=hashlib.sha256(GOLDEN.read_bytes()).digest()
    assert record(store,clock,tmp_path)==golden()
    assert hashlib.sha256(GOLDEN.read_bytes()).digest()==before
