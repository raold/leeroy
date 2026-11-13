import asyncio
import logging
from dataclasses import replace
from types import SimpleNamespace
import pytest

from conftest import run
from selfies.fakes import mind_app, ready, ready_snap
from selfies.test_scene_choices import PICKS
from leeroy_mind.discord import store as dstore
from leeroy_mind.discord.transport import DiscordTarget
from leeroy_mind.photos.send import MAX_IN_FLIGHT, photo_seeds, channel_count
from leeroy_mind.selfies import files, snaps, ration
from leeroy_mind.selfies.scene_choices import compose_choices
from leeroy_mind.selfies.store import current, pick
from leeroy_mind.talk.pipeline import TurnMeta
from leeroy_mind.talk.turns import Turn, TurnKind
from photos.test_pipeline_photo import finish

ON = '[photos]\nsend=true\n[selfies]\nenabled=true\nsnaps=true\n[server]\nenabled=true\n[talk]\nselfies=3\n'


def setup(tmp_path, toml=ON):
    app = mind_app(tmp_path,toml=toml)
    if app.photos is not None:
        app.photos.rng = SimpleNamespace(random=lambda:0)
    return app


def stock(app,guid=101,*,audience='him',scene=None,**over):
    if current(app.store,guid) is None:
        ready(app.store,app.data_dir,guid,1)
        pick(app.store,guid,1,0,source='console',now=2)
    data = compose_choices({**PICKS,'when':'evening','audience':audience}) if scene is None else {**scene,'audience':audience}
    return ready_snap(app.store,app.data_dir,guid,audience=audience,scene=data,
        pick=current(app.store,guid).pick,looks=snaps.looks_hash(app.personas.get(guid).appearance),**over)


def reply(app,guid=101,*,channel=None):
    channel = channel or f'dm:{guid}'
    mid=dstore.post(app.store,channel=channel,author=app.human_guid,origin='human',text='how is your day?',now=app.clock.now())
    return Turn(TurnKind.REPLY_HUMAN,guid,channel,to_guid=app.human_guid,addressee=app.human_guid), \
        TurnMeta('',why='discord:reply',discord=DiscordTarget(mid,None))


def test_dm_delivery_has_one_caption_real_image_and_private_budget(tmp_path):
    async def go():
        app=setup(tmp_path); sid=stock(app)
        app.llm._script=lambda *a:'the dog has appointed himself tour guide'
        before={p.relative_to(tmp_path):p.read_bytes() for p in tmp_path.rglob('*.jpg')}
        try:
            turn,meta=reply(app)
            assert await finish(app,asyncio.create_task(app.engine.speak_meta(turn,meta)))
            row=app.store.conn.execute('SELECT s.channel,s.status,s.line_id,a.path,m.text FROM selfie_snap s '
                'JOIN discord_attachment a ON a.snap=s.id JOIN discord_message m ON m.id=a.message').fetchone()
            assert row['channel']=='dm' and row['status']=='sent'
            assert len(app.photos._in_flight)==0
            assert snaps.sent_count(app.store,app.clock.now()-86400,to_him=True)==1
            assert channel_count(app.store.conn,app.clock.local().date().isoformat())==0
            assert app.store.conn.execute('SELECT to_guid FROM chat_line WHERE id=?',(row['line_id'],)).fetchone()[0]==app.human_guid
            assert {p.relative_to(tmp_path):p.read_bytes() for p in tmp_path.rglob('*.jpg')}==before
        finally:await app.stop()
    run(go())


@pytest.mark.parametrize('toml,needed',[
    ('',''),('[photos]\nsend=true\n','selfies.enabled'),
    ('[photos]\nsend=true\n[selfies]\nenabled=true\n','selfies.snaps'),
    ('[photos]\nsend=true\n[selfies]\nenabled=true\nsnaps=true\n','server.enabled')])
def test_off_or_missing_dependency_registers_no_sender(tmp_path,caplog,toml,needed):
    caplog.set_level(logging.INFO)
    app=setup(tmp_path,toml)
    try:
        assert app.photos is None and not getattr(app.engine,'photos_send',False)
        assert not getattr(app.engine,'_photo_offer',None)
        assert ('photos_send_needs' in caplog.text)==bool(needed)
        if needed:assert needed in caplog.text
    finally:app.store.close()


@pytest.mark.parametrize('channel',['discord:general','discord:officers','guild','whisper','party','dm:102'])
def test_private_snap_never_offered_outside_her_human_dm(tmp_path,channel):
    app=setup(tmp_path);stock(app)
    try:
        turn,meta=reply(app,channel=channel)
        assert app.photos.offer(turn,meta,app.clock.now()) is None
        assert app.store.conn.execute("SELECT status FROM selfie_snap").fetchone()[0]=='ready'
    finally:app.store.close()


def test_reserved_slot_blocks_other_member_and_release_spends_nothing(tmp_path):
    app=setup(tmp_path);stock(app);stock(app,102)
    try:
        first=app.photos.offer(*reply(app),app.clock.now())
        assert first and len(app.photos._in_flight)==MAX_IN_FLIGHT==1
        assert app.photos.offer(*reply(app,102),app.clock.now()) is None
        first.release()
        assert app.photos.offer(*reply(app,102),app.clock.now())
        assert snaps.sent_count(app.store,0)==0
    finally:app.store.close()


@pytest.mark.parametrize('reason',['pick','looks','nsfw','missing','phase','dial','stopped','parent'])
def test_revalidation_rejects_changed_state_and_keeps_files(tmp_path,reason):
    app=setup(tmp_path);sid=stock(app,phases=('evening',))
    try:
        turn,meta=reply(app);photo=app.photos.offer(turn,meta,app.clock.now());assert photo
        if reason=='pick':pick(app.store,101,1,1,source='console',now=app.clock.now())
        if reason=='looks':
            p=app.personas.get(101)
            app.personas.get=lambda guid:replace(p,appearance=replace(p.appearance,hair_colour='different'))
        if reason=='nsfw':app.selfies.cfg=replace(app.selfies.cfg,nsfw_max=.005)
        if reason=='missing':files.snap(app.data_dir,101,sid).rename(files.snap(app.data_dir,101,sid).with_suffix('.kept'))
        if reason=='phase':
            app.clock._t += 12*3600
        if reason=='dial':app.cfg=replace(app.cfg,talk=replace(app.cfg.talk,selfies=0))
        if reason=='stopped':app._stopping=True
        if reason=='parent':dstore.delete(app.store,meta.discord.reply_to,author=app.human_guid,now=app.clock.now())
        assert not photo.valid()
        photo.release()
        assert snaps.sent_count(app.store,0)==0
        assert files.snap(app.data_dir,101,sid,thumb=True).exists()
    finally:app.store.close()


def test_channel_seed_owner_only_and_independent_budget(tmp_path):
    app=setup(tmp_path);sid=stock(app,audience='anyone');stock(app,102)
    try:
        seeds=photo_seeds(app.store.conn,now=app.clock.now(),local=app.clock.local(),dial=3)
        assert len(seeds)==1 and seeds[0].key==f'photo:{sid}' and seeds[0].openers==(101,)
        assert seeds[0].channels==('pets',)
        turn=Turn(TurnKind.OPEN,101,'discord:pets')
        meta=TurnMeta('',why='server:photo',photo_seed=sid)
        photo=app.photos.offer(turn,meta,app.clock.now());assert photo
        # Exercise the sender's atomic hook with the same durable rows as the pipeline.
        with app.store.transaction():
            mid=dstore.post(app.store,channel='pets',author=101,origin='mind',text='the dog leads',now=app.clock.now(),attachment=photo.attachment)
            app.store.conn.execute("INSERT INTO chat_line(ts,from_guid,from_name,to_guid,channel,text,origin) VALUES(?,101,'Invented',0,'discord:pets','the dog leads','mind')",(app.clock.now(),))
            line=app.store.conn.execute('SELECT max(id) FROM chat_line').fetchone()[0]
            photo.on_sent(line,mid)
        assert channel_count(app.store.conn,app.clock.local().date().isoformat())==1
        assert snaps.sent_count(app.store,0,to_him=True)==0
        assert app.photos.offer(*reply(app,102),app.clock.now())
        assert photo_seeds(app.store.conn,now=app.clock.now(),local=app.clock.local(),dial=3)==[]
    finally:app.store.close()


def test_channel_malformed_budget_and_lust_scene_fail_closed(tmp_path):
    app=setup(tmp_path)
    stock(app,audience='anyone',scene={**compose_choices(PICKS),'style':'lust'})
    try:
        assert not photo_seeds(app.store.conn,now=app.clock.now(),local=app.clock.local(),dial=10)
        app.store.set_meta(ration.CHANNEL_KEY.format(date=app.clock.local().date().isoformat()),'bad')
        assert not photo_seeds(app.store.conn,now=app.clock.now(),local=app.clock.local(),dial=10)
    finally:app.store.close()


@pytest.mark.parametrize('limit',['day','week','member','dial','chance'])
def test_private_ration_and_member_gap_survive_new_sender(tmp_path,limit):
    from leeroy_mind.photos.send import PhotoSender
    app=setup(tmp_path);stock(app)
    try:
        now=app.clock.now()
        if limit in ('day','week','member'):
            count=3 if limit=='week' else 1
            for index in range(count):
                sid=stock(app,101 if limit=='member' else 102)
                assert snaps.mark_sent(app.store,sid,sender=101 if limit=='member' else 102,
                    channel='dm',line_id=index+1,now=now-(2*86400 if limit=='week' else 86401 if limit=='member' else 1))
        if limit=='dial':app.cfg=replace(app.cfg,talk=replace(app.cfg.talk,selfies=0))
        if limit=='chance':app.photos.rng=SimpleNamespace(random=lambda:1)
        again=PhotoSender(app,app.photos.config,app.photos.rng)
        assert again.offer(*reply(app),now) is None
        assert not again._in_flight
    finally:app.store.close()


def test_dm_opener_is_eligible_while_human_offline(tmp_path):
    app=setup(tmp_path);stock(app)
    try:
        assert not app.world.human_online
        turn=Turn(TurnKind.OPEN,101,'dm:101',to_guid=app.human_guid,addressee=app.human_guid)
        photo=app.photos.offer(turn,TurnMeta('',why='server:dm'),app.clock.now())
        assert photo
        photo.release();photo.release()
        assert not app.photos._in_flight and snaps.sent_count(app.store,0)==0
    finally:app.store.close()


def test_conflicting_scene_audience_is_not_a_public_seed_or_offer(tmp_path):
    app=setup(tmp_path);sid=stock(app,audience='anyone')
    before={p:p.read_bytes() for p in (tmp_path/'selfies').rglob('*') if p.is_file()}
    try:
        with app.store.transaction():
            app.store.conn.execute("UPDATE selfie_snap SET scene=json_set(scene,'$.audience','him') WHERE id=?",(sid,))
        assert not photo_seeds(app.store.conn,now=app.clock.now(),local=app.clock.local(),dial=3)
        assert app.photos.offer(Turn(TurnKind.OPEN,101,'discord:pets'),
            TurnMeta('',why='server:photo',photo_seed=sid),app.clock.now()) is None
        assert app.store.conn.execute('SELECT ended FROM selfie_snap WHERE id=?',(sid,)).fetchone()[0]=='flagged'
        assert before=={p:p.read_bytes() for p in (tmp_path/'selfies').rglob('*') if p.is_file()}
    finally:app.store.close()


@pytest.mark.parametrize('age,style,allowed',[(24,'plain',False),(35,'lust',True)])
def test_adult_floor_and_private_lust_style_keep_existing_content_gate(tmp_path,age,style,allowed):
    app=setup(tmp_path)
    p=app.personas.get(101)
    p=replace(p,identity=replace(p.identity,player_gender='female',age=age),
              romance=replace(p.romance,stance='lust'))
    original=app.personas.get
    app.personas.get=lambda guid:p if guid==101 else original(guid)
    sid=stock(app,scene={**compose_choices(PICKS),'style':style})
    try:
        photo=app.photos.offer(*reply(app),app.clock.now())
        assert bool(photo)==allowed
        if photo:photo.release()
        assert files.snap(tmp_path,101,sid).exists() and files.snap(tmp_path,101,sid,thumb=True).exists()
    finally:app.store.close()


@pytest.mark.parametrize('cause',['caption','stale'])
def test_his_dm_answer_is_sent_without_a_photo_it_cannot_carry(tmp_path,cause):
    """An answer owed to his DM never dies with its photo: the photo is released (or expired
    when it went stale) and the answer is asked again without it."""
    async def go():
        app=setup(tmp_path);sid=stock(app)
        before={p.relative_to(tmp_path):p.read_bytes() for p in tmp_path.rglob('*.jpg')}
        def script(model,messages):
            if "You're posting a photo of yourself" not in str(messages):
                return 'the dog has appointed himself tour guide'
            if cause=='stale':  # her profile photo changes while she types the caption
                pick(app.store,101,1,1,source='console',now=app.clock.now())
                return 'the dog has appointed himself tour guide'
            return 'my pajamas and the dog, an evening'
        app.llm._script=script
        try:
            turn,meta=reply(app)
            meta.answers_human=True
            lines=await finish(app,asyncio.create_task(app.engine.speak_meta(turn,meta)))
            assert len(lines)==1 and 'pajamas' not in lines[0]
            rows=app.store.conn.execute("SELECT text FROM discord_message WHERE origin='mind' AND channel='dm:101'").fetchall()
            assert [r[0] for r in rows]==lines
            assert app.store.conn.execute('SELECT count(*) FROM discord_attachment').fetchone()[0]==0
            status,ended=app.store.conn.execute('SELECT status,ended FROM selfie_snap WHERE id=?',(sid,)).fetchone()
            assert (status,ended)==(('expired','photo_changed') if cause=='stale' else ('ready',None))
            assert not app.photos._in_flight and snaps.sent_count(app.store,0,to_him=True)==0
            assert {p.relative_to(tmp_path):p.read_bytes() for p in tmp_path.rglob('*.jpg')}==before
        finally:await app.stop()
    run(go())


def test_offer_that_cannot_be_built_reserves_nothing(tmp_path):
    app=setup(tmp_path);stock(app,scene={**compose_choices(PICKS),'shows':['not','text']})
    try:
        assert app.photos.offer(*reply(app),app.clock.now()) is None
        assert not app.photos._in_flight
    finally:app.store.close()
