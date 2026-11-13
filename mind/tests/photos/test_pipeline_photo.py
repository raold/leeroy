import asyncio
from types import SimpleNamespace
from unittest.mock import patch
import pytest

from conftest import run
from selfies.fakes import mind_app, ready_snap
from leeroy_mind.discord.store import AttachmentIn
from leeroy_mind.photos.offer import PhotoOffer
from leeroy_mind.selfies import files, snaps
from leeroy_mind.talk.pipeline import TurnMeta
from leeroy_mind.talk.turns import Turn, TurnKind


async def finish(app, task):
    original_to_thread=asyncio.to_thread
    idle=asyncio.Event()
    idle.set()
    working=0
    async def tracked_work(*args,**kwargs):
        nonlocal working
        working+=1
        idle.clear()
        try:
            return await original_to_thread(*args,**kwargs)
        finally:
            working-=1
            if not working:
                idle.set()
    try:
        with patch('asyncio.to_thread',tracked_work):
            async with asyncio.timeout(10):
                for _ in range(100):
                    # Settle real workers before spending fake time. A yield(0)
                    # alone can exhaust a turn's deadline while SQLite waits for CPU.
                    await asyncio.sleep(.001)
                    while working:
                        await idle.wait()
                    if task.done():
                        return await task
                    await app.clock.advance(2,2)
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task,return_exceptions=True)
    pytest.fail('photo turn did not finish within its fake-clock deadline')


def test_finish_gives_a_real_worker_time_before_spending_fake_deadline():
    import time
    from datetime import datetime, timezone
    from leeroy_mind.testing.fake_clock import FakeClock
    async def go():
        clock=FakeClock(datetime(2026,11,12,tzinfo=timezone.utc),yields=2)
        app=SimpleNamespace(clock=clock)
        async def turn():
            await asyncio.to_thread(time.sleep,.05)
            await clock.sleep(4)
            return 'finished'
        before=clock.now()
        assert await finish(app,asyncio.create_task(turn()))=='finished'
        assert clock.now()-before==4  # no fake time passes while real work is pending
    run(go())


def offer(app):
    sid = ready_snap(app.store, app.data_dir, 101, audience='him')
    snap = snaps.ready_for(app.store,101,now=app.clock.now(),phase='evening')[0]
    path = files.snap(app.data_dir,101,sid)
    called = SimpleNamespace(sent=[], released=0, asked=0)
    def sent(line, mid):
        called.sent.append((line,mid))
        assert app.store.in_unit
        assert snaps.mark_sent(app.store,sid,sender=101,channel='dm',line_id=line,now=app.clock.now())
    def released():
        called.released += 1
    photo = PhotoOffer(snap, 'You are posting a photo of an invented garden. Write a short caption.',
        sent, released, AttachmentIn('snap',path.relative_to(app.data_dir).as_posix(),832,1216,path.stat().st_size,sid))
    def attach(turn,meta,now):
        called.asked += 1
        return photo
    app.engine.attach_photos(attach)
    return photo, called


def test_offer_makes_one_line_and_commits_photo_with_chat(tmp_path):
    async def go():
        app=mind_app(tmp_path)
        photo,called=offer(app)
        app.llm._script=lambda *a:'the garden has claimed my afternoon'
        turn=Turn(TurnKind.REPLY_HUMAN,101,'dm:101',to_guid=app.human_guid,addressee=app.human_guid)
        meta=TurnMeta('test-photo',why='discord:reply')
        try:
            lines=await finish(app,asyncio.create_task(app.engine.speak_meta(turn,meta)))
            assert len(lines)==1 and called.asked==1 and called.released==0
            assert len(called.sent)==1 and meta.photo_finished
            row=app.store.conn.execute('SELECT m.text,a.snap,s.status,s.line_id FROM discord_message m '
                'JOIN discord_attachment a ON a.message=m.id JOIN selfie_snap s ON s.id=a.snap').fetchone()
            assert row['text']==lines[0] and row['snap']==photo.snap.id and row['status']=='sent'
            assert app.store.conn.execute('SELECT text FROM chat_line WHERE id=?',(row['line_id'],)).fetchone()[0]==lines[0]
            assert 'invented garden' in str(app.llm.calls)
        finally:await app.stop()
    run(go())


@pytest.mark.parametrize('reason',['photo_words','guard','record','cancel','reaction'])
def test_failed_photo_turn_releases_once_without_spending_snap(tmp_path,reason):
    async def go():
        app=mind_app(tmp_path)
        photo,called=offer(app)
        app.llm._script=lambda *a:'my pajamas survived' if reason=='photo_words' else 'the garden has claimed my afternoon'
        turn=Turn(TurnKind.REPLY_HUMAN,101,'dm:101',to_guid=app.human_guid,addressee=app.human_guid)
        # an opener loses its turn with its photo; an answer owed to him never does (below)
        meta=TurnMeta('test-photo',why='server:dm' if reason=='photo_words' else 'discord:reply')
        if reason=='guard':app.engine._blocked=lambda *a,**k:'test_guard'
        if reason=='record':
            original=app.engine._record
            def fail(*args,**kwargs):
                original(*args,**kwargs)
                raise RuntimeError('synthetic rollback')
            app.engine._record=fail
        if reason=='reaction':
            async def react(turn,meta,*a,**k):
                await app.engine._guard_react(turn,meta,'👍')
                return []
            app.engine._guarded_lines=react
        if reason=='cancel':app.llm._gate=asyncio.Event()
        try:
            task=asyncio.create_task(app.engine.speak_meta(turn,meta))
            if reason=='cancel':
                while called.asked==0:await asyncio.sleep(0)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):await task
            else:assert await finish(app,task)==[]
            assert called.asked==1 and called.released==1
            assert app.store.conn.execute('SELECT status FROM selfie_snap WHERE id=?',(photo.snap.id,)).fetchone()[0]=='ready'
            assert app.store.conn.execute('SELECT count(*) FROM discord_attachment').fetchone()[0]==0
            assert app.store.conn.execute('SELECT count(*) FROM discord_message').fetchone()[0]==0
            assert files.snap(app.data_dir,101,photo.snap.id).exists()
            assert meta.said==0
            if reason=='photo_words':
                assert app.store.conn.execute('SELECT dropped FROM utterance ORDER BY id DESC LIMIT 1').fetchone()[0]=='photo_words'
        finally:await app.stop()
    run(go())


def test_game_and_template_turns_never_ask_for_photo(tmp_path):
    async def go():
        app=mind_app(tmp_path);photo,called=offer(app)
        app.engine._blocked=lambda *a,**k:'test_guard'
        try:
            await app.engine.speak_meta(Turn(TurnKind.OPEN,101,'guild'),TurnMeta('game'))
            await app.engine.speak_meta(Turn(TurnKind.REPLY_HUMAN,101,'dm:101',template='hello'),TurnMeta('template'))
            assert called.asked==called.released==0
        finally:await app.stop()
    run(go())


PHOTO_ASK = 'invented garden'


def _no_photo_files_lost(app, sid):
    return files.snap(app.data_dir,101,sid).exists() and files.snap(app.data_dir,101,sid,thumb=True).exists()


@pytest.mark.parametrize('cause',['words','words_at_send','stale_at_send','stale_at_store'])
def test_owed_dm_answer_loses_only_its_photo(tmp_path,cause,monkeypatch):
    """His DM is always answered: a caption the screens refuse or a photo gone stale at send
    releases the photo, and the answer is asked once more without it."""
    async def go():
        app=mind_app(tmp_path)
        photo,called=offer(app)
        asked=[]
        def script(model,messages):
            with_photo=PHOTO_ASK in str(messages)
            asked.append(with_photo)
            return 'my pajamas survived' if with_photo and cause=='words' else 'the garden has claimed my afternoon'
        app.llm._script=script
        if cause=='words_at_send':  # the typed line, not the model's words, trips the screen
            import leeroy_mind.talk.pipeline as pipeline
            checks=iter([True,False])
            monkeypatch.setattr(pipeline,'caption_ok',lambda text:next(checks))
        if cause.startswith('stale'):
            checks=iter([True] if cause=='stale_at_store' else [])
            photo.valid=lambda:next(checks,False)
        turn=Turn(TurnKind.REPLY_HUMAN,101,'dm:101',to_guid=app.human_guid,addressee=app.human_guid)
        meta=TurnMeta('test-photo',why='discord:reply',answers_human=True)
        try:
            lines=await finish(app,asyncio.create_task(app.engine.speak_meta(turn,meta)))
            assert len(lines)==1 and 'pajamas' not in lines[0]
            assert asked==[True,False]
            assert called.asked==1 and called.released==1 and called.sent==[]
            assert meta.photo is None and meta.said==1
            assert [r[0] for r in app.store.conn.execute('SELECT text FROM discord_message')]==lines
            assert app.store.conn.execute('SELECT count(*) FROM discord_attachment').fetchone()[0]==0
            assert app.store.conn.execute('SELECT status FROM selfie_snap WHERE id=?',(photo.snap.id,)).fetchone()[0]=='ready'
            assert not app.store.conn.execute("SELECT count(*) FROM utterance WHERE dropped IS NOT NULL").fetchone()[0]
            assert _no_photo_files_lost(app,photo.snap.id)
        finally:await app.stop()
    run(go())


@pytest.mark.parametrize('cause',['photo_words','photo_stale'])
def test_opener_still_drops_with_its_photo(tmp_path,cause):
    async def go():
        app=mind_app(tmp_path)
        photo,called=offer(app)
        app.llm._script=lambda *a:'my pajamas survived' if cause=='photo_words' else 'the garden has claimed my afternoon'
        if cause=='photo_stale':photo.valid=lambda:False
        turn=Turn(TurnKind.OPEN,101,'dm:101',to_guid=app.human_guid,addressee=app.human_guid)
        meta=TurnMeta('test-opener',why='server:dm',initiates=True)
        try:
            assert await finish(app,asyncio.create_task(app.engine.speak_meta(turn,meta)))==[]
            assert len([c for c in app.llm.calls if 'messages' in c])==1
            assert called.released==1 and called.sent==[]
            assert app.store.conn.execute('SELECT count(*) FROM discord_message').fetchone()[0]==0
            assert app.store.conn.execute('SELECT dropped FROM utterance ORDER BY id DESC LIMIT 1').fetchone()[0]==cause
            assert _no_photo_files_lost(app,photo.snap.id)
        finally:await app.stop()
    run(go())
