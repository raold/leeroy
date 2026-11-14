"""Requested photos run through real Discord storage and ordinary guarded replies."""
import asyncio

import pytest

from conftest import run
from nightly.conftest import FakeAdmin
from photos.test_images import jpeg
from selfies.fakes import mind_app, ready, known_offline
from leeroy_mind.protocol import Member
from leeroy_mind.discord import store as discord
from leeroy_mind.photos import request_store as records
from leeroy_mind.photos.request_words import request_target
from leeroy_mind.selfies import files, snaps
from leeroy_mind.selfies.store import pick

ON = ('[photos]\nsend=true\nrequests=true\n[selfies]\nenabled=true\nsnaps=true\n'
      '[server]\nenabled=true\nconversations_per_hour=0\ndms_per_day=0\nreactions=false\n'
      '[talk]\nselfies=3\nchattiness=0\n')


def make_app(tmp_path, toml=ON):
    def prepare(store):
        store.conn.execute('DELETE FROM cast_member WHERE guid=100')
        store.upsert_cast([Member(guid=1,name='Testowner',race=2,class_=1,gender=0,level=80,
            rank=0,online=False,map=0,zone=0,spec_tab=-1)], store.clock.now())
        store.set_meta('human_guid','1')
        for guid in (101,102,103):
            ready(store,tmp_path,guid,1)
            for index in range(4):
                for thumb in (False,True):
                    files.candidate(tmp_path,guid,1,index,thumb=thumb).write_bytes(
                        jpeg()+f'/{guid}/{index}/{thumb}'.encode())
            chosen = pick(store,guid,1,0,source='console',now=store.clock.now())
            path = files.history(tmp_path,guid,chosen)
            path.parent.mkdir(parents=True,exist_ok=True)
            path.write_bytes(files.candidate(tmp_path,guid,1,0).read_bytes())
    app = mind_app(tmp_path,toml=toml,prepare=prepare)
    app.arbiter.admin = FakeAdmin([app.cfg.models.live,app.cfg.models.embedding])
    app.llm.set_models({app.cfg.models.live,app.cfg.models.embedding})
    app.llm._script = lambda *args: 'the quiet afternoon made a good excuse for a photo'
    return app


async def offline(app):
    known_offline(app)
    await app.photos.requests._presence(app.hello)


async def drive(app, predicate):
    # Real worker/admin I/O must settle before fake time can age a turn.
    async with asyncio.timeout(15):
        while not predicate():
            await asyncio.sleep(.005)
            if not getattr(getattr(app,'live_borrow',None),'blocked',False):
                await app.clock.advance(1,1)


def post(app, channel='dm:101', text='send me a selfie', reply_to=None):
    return discord.post(app.store,channel=channel,author=1,origin='human',text=text,
        reply_to=reply_to,now=app.clock.now())


def test_target_is_dm_peer_or_first_named_and_never_random(tmp_path):
    app = make_app(tmp_path)
    try:
        assert request_target(app,post(app)) == 101
        assert request_target(app,post(app,'general','selfie?')) is None
        a,b = [app.world.discord_members()[g].name for g in (101,102)]
        assert request_target(app,post(app,'general',f'@{b} @{a} send a selfie')) == 102
        parent = discord.post(app.store,channel='general',author=101,origin='mind',text='A quiet moment.',now=app.clock.now())
        assert request_target(app,post(app,'general',f'@{b} selfie?',parent)) == 101
        assert request_target(app,post(app,'dm:101','nice pic')) is None
    finally:
        run(app.stop())


def test_target_subject_must_be_her_own_name_or_self(tmp_path):
    app = make_app(tmp_path)
    try:
        a,b = [app.world.discord_members()[g].name for g in (101,102)]
        real = app.personas.get(101).identity.real_name.split()[0]
        assert request_target(app,post(app,'dm:101',f'send me a pic of {a}')) == 101
        assert request_target(app,post(app,'dm:101',f'send me a pic of {real}')) == 101
        assert request_target(app,post(app,'dm:101',f'send me a pic of {b}')) is None
        assert request_target(app,post(app,'general',f'@{a} send me a pic of @{a}')) == 101
        assert request_target(app,post(app,'general',f'@{a} send me a pic of @{b}')) is None
        assert request_target(app,post(app,'dm:101','got it, nice pic')) is None
        assert request_target(app,post(app,'dm:101',"I'll send you a pic later")) is None
    finally:
        run(app.stop())


def test_off_adds_no_request_hooks_or_admission(tmp_path):
    app = make_app(tmp_path,ON.replace('requests=true','requests=false'))
    try:
        assert not hasattr(app.photos,'requests') and not hasattr(app,'live_borrow')
        assert not any(getattr(fn,'__module__','').endswith('photos.requests') for fn in app._background)
    finally:
        run(app.stop())


def test_known_offline_needs_current_connection_hello_and_resets_grace(tmp_path):
    async def scenario():
        app=make_app(tmp_path)
        service=app.photos.requests
        try:
            assert not service.known_offline()
            await offline(app)
            assert service.known_offline()
            first=service._offline_at
            await service._connection(False)
            assert not service.known_offline() and service._offline_at is None
            await app.clock.advance(30,30)
            await service._connection(True)
            assert not service.known_offline()
            await service._presence(app.hello)
            assert service.known_offline() and service._offline_at > first
        finally:
            await app.stop()
    run(scenario())


@pytest.mark.parametrize('channel', ['dm:101','general'])
def test_online_request_answers_with_gallery_copy_and_keeps_fresh_plan(tmp_path,monkeypatch,channel):
    from leeroy_mind.selfies.scene import scene_from
    from leeroy_mind.selfies.scene_choices import compose_choices
    from selfies.test_scene_choices import PICKS
    async def scenario():
        app=make_app(tmp_path)
        try:
            async def scene(app,guid,words,audience):
                return scene_from(compose_choices({**PICKS,'audience':audience,'when':'any'}))
            monkeypatch.setattr('leeroy_mind.selfies.request_scene.choose_scene',scene)
            app.night_scheduler.presence.online=True
            name=app.world.discord_members()[101].name
            before={p:p.read_bytes() for p in tmp_path.rglob('*.jpg')}
            sent=await app.discord.handle({'action':'post','channel':channel,'text':f'@{name} send me a selfie'})
            assert sent['ok']
            message=sent['id']
            await drive(app,lambda: bool((r:=records.get(app.store,message)) and r.initial_message))
            request=records.get(app.store,message)
            assert request.fallback_message == request.initial_message
            assert records.snap(app.store,request.fresh_snap).status=='planned'
            assert records.snap(app.store,request.fallback_snap).status=='sent'
            assert not app.store.conn.execute('SELECT 1 FROM selfie_request_borrow').fetchone()
            assert all(path.read_bytes()==data for path,data in before.items())
            assert snaps.sent_count(app.store,0)==0 and snaps.last_sent(app.store,101) is None
            if channel=='general':
                from leeroy_mind.photos.notes import by_messages
                assert request.initial_message in by_messages(app.store.conn,[request.initial_message],members=True)
            app.llm._script = lambda *args: 'give me a little longer before another one'
            await app.clock.advance(15,15)  # keep the ordinary public answer interval
            again=await app.discord.handle({'action':'post','channel':channel,'text':f'@{name} selfie?'})
            await drive(app,lambda: app.store.conn.execute('SELECT 1 FROM discord_message WHERE reply_to=? AND origin=\'mind\'',(again['id'],)).fetchone())
            assert records.get(app.store,again['id']) is None
            assert app.store.conn.execute('SELECT count(*) FROM discord_attachment').fetchone()[0]==1
            # Just past the member's two-minute window, a new ask is a request again.
            await app.clock.advance(request.created+records.MEMBER_GAP+1-app.clock.now(),1)
            app.llm._script = lambda *args: 'fine, the window light was decent anyway'
            third=await app.discord.handle({'action':'post','channel':channel,'text':f'@{name} pic?'})
            await drive(app,lambda: bool((r:=records.get(app.store,third['id'])) and r.initial_message))
            assert records.get(app.store,third['id']).guid==101
        finally:
            await app.stop()
    run(scenario())
