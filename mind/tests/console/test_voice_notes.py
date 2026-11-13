from pathlib import Path
import os
import pytest
from conftest import run,seed_test_cast
from console.rig import http,FakeMind
from console.test_console_discord import build,served,FakeLink
from leeroy_mind.discord import store as ds
from leeroy_mind.voice_notes import files
from voice_notes.test_files import wav,inventory

HEADERS={'Content-Type':'audio/wav','Sec-Fetch-Site':'same-origin'}


@pytest.fixture
def data(store):
    root=Path(store.conn.execute('PRAGMA database_list').fetchone()['file']).parent
    (root/'mind.toml').write_text('[voice_notes]\nenabled=true')
    return root


def upload(app,data,**kw):
    return http(app,'POST','/api/discord/voice',body=data,origin=True,headers=kw.pop('headers',HEADERS),**kw)


def test_upload_serve_delete_preserves_audio(data,store,clock):
    seed_test_cast(store)
    async def go():
        async with served(build(data,link=FakeLink())) as app:
            result=await upload(app,wav()); assert result.status==201,result.body
            token=result.json()['token']; path,ms,size=files.adopt(data,token)
            with store.transaction():
                mid=ds.post(store,channel='dm:101',author=101,origin='mind',text='An invented note.',now=clock.now())
                store.conn.execute('INSERT INTO discord_voice VALUES (?,?,?,?,?,1)',(mid,'mind',path,ms,size))
            view=(await http(app,'GET','/api/discord?channel=dm:101')).json()
            assert view['voice_notes'] is True and view['messages'][0]['voice_note']['transcript_ok'] is True
            audio=await http(app,'GET',f'/api/discord/voice?id={mid}')
            assert audio.status==200 and audio.body==wav()
            assert audio.header('Cache-Control')=='private, max-age=60'
            assert (await http(app,'GET',f'/api/discord/voice?id={mid}',key=False)).status==403
            before=inventory(data/'discord')
            ds.delete(store,mid,author=101,now=clock.now());store.conn.commit()
            assert (await http(app,'GET',f'/api/discord/voice?id={mid}')).status==404
            assert inventory(data/'discord')==before
    run(go())


@pytest.mark.parametrize('mode',['off','control','origin','site','key','type','stopped'])
def test_voice_upload_gates(data,mode):
    async def go():
        if mode=='off':(data/'mind.toml').write_text('')
        app=build(data,link=FakeLink(),**({'mind':FakeMind(data)} if mode=='stopped' else {}))
        if mode=='control':app.controls.enabled=False
        headers=dict(HEADERS)
        if mode=='site':headers.pop('Sec-Fetch-Site')
        if mode=='type':headers['Content-Type']='application/json'
        async with served(app):
            response=await http(app,'POST','/api/discord/voice',body=wav(),origin=mode!='origin',
                key=mode!='key',headers=headers)
            assert response.status==(415 if mode=='type' else 409 if mode=='stopped' else 403)
            assert not (data/files.INCOMING).exists()
            if mode=='off':assert (await http(app,'GET','/api/discord')).json()['voice_notes'] is False
    run(go())


def test_minute_and_restart_day_quota(data):
    async def go():
        async with served(build(data,link=FakeLink())) as app:
            for _ in range(4):assert (await upload(app,wav())).status==201
            assert (await upload(app,wav())).status==429
        for _ in range(96):files.stage(data,wav())
        before=inventory(data/'discord')
        # Build uses a fixed fake wall clock; retained staging mtimes match that day.
        from console.test_console_discord import NOW
        for path in (data/files.INCOMING).glob('*.wav'):os.utime(path,(NOW,NOW))
        async with served(build(data,link=FakeLink())) as app:
            assert (await upload(app,wav())).status==429
        assert inventory(data/'discord')==before
    run(go())


@pytest.mark.parametrize('body',[b'not RIFF',wav(channels=2),wav(rate=44100),wav(seconds=61),None])
def test_upload_validation_precedes_any_write(data,body):
    async def go():
        before=inventory(data/'discord')
        async with served(build(data,link=FakeLink())) as app:
            result=await upload(app,body,headers={**HEADERS,'Content-Length':'2097153'}) if body is None else await upload(app,body)
            assert result.status in (400,413)
        assert inventory(data/'discord')==before
    run(go())


def test_audio_serve_refuses_wrong_identity_and_link(data,store,clock,monkeypatch):
    async def go():
        token,_,_=files.stage(data,wav());path,ms,size=files.adopt(data,token)
        with store.transaction():
            mid=ds.post(store,channel='dm:101',author=101,origin='mind',text='An invented note.',now=clock.now())
            store.conn.execute('INSERT INTO discord_voice VALUES (?,?,?,?,?,1)',(mid,'mind',path,ms,size))
        before=inventory(data/'discord')
        async with served(build(data,link=FakeLink())) as app:
            original=files.os.fstat
            def wrong(fd):
                from types import SimpleNamespace
                st=original(fd)
                return SimpleNamespace(st_dev=st.st_dev,st_ino=st.st_ino+1)
            with monkeypatch.context() as patch:
                patch.setattr(files.os,'fstat',wrong)
                assert (await http(app,'GET',f'/api/discord/voice?id={mid}')).status==404
            assert (await http(app,'GET',f'/api/discord/voice?id={mid}')).status==200
            # A DB path outside the one allowed audio directory is never followed.
            with store.transaction():store.conn.execute('UPDATE discord_voice SET path=? WHERE message=?',('../outside.wav',mid))
            assert (await http(app,'GET',f'/api/discord/voice?id={mid}')).status==404
        assert inventory(data/'discord')==before
    run(go())
