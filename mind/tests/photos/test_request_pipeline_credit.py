"""An ordinary real Discord turn survives the image borrow's full fake-time wait."""
import asyncio

import pytest

from conftest import run
from nightly.conftest import FakeAdmin
from photos.test_pipeline_photo import finish
from selfies.fakes import mind_app
from leeroy_mind.nightly.wiring import enable_borrow
from leeroy_mind.llm.scheduler import Priority
from leeroy_mind.protocol import Member
from leeroy_mind.talk.pipeline import TurnMeta
from leeroy_mind.talk.turns import Turn, TurnKind


@pytest.mark.parametrize('explicit_deadline', [False, True])
def test_real_turn_waits_180_seconds_for_borrow_then_speaks(tmp_path, explicit_deadline):
    def prepare(store):
        store.conn.execute('DELETE FROM cast_member WHERE guid=100')
        store.upsert_cast([Member(guid=1,name='Testowner',race=2,class_=1,gender=0,level=80,
            rank=0,online=False,map=0,zone=0,spec_tab=-1)],store.clock.now())
        store.set_meta('human_guid','1')
    async def go():
        app=mind_app(tmp_path,toml='[server]\nenabled=true\n',prepare=prepare)
        app.arbiter.admin=FakeAdmin([app.cfg.models.live,app.cfg.models.embedding])
        gate=enable_borrow(app)
        app.llm._script=lambda *a:'the kettle has claimed the afternoon'
        borrowed,release,waiting=asyncio.Event(),asyncio.Event(),asyncio.Event()
        original=app.engine.live_admission
        async def admission():
            waiting.set()
            await original()
        app.engine.live_admission=admission
        async def render():
            async with app.arbiter.borrow('request:101'):
                borrowed.set()
                await release.wait()
        borrow=asyncio.create_task(render())
        turn_task=None
        try:
            await borrowed.wait()
            turn=Turn(TurnKind.REPLY_HUMAN,101,'dm:101',to_guid=1,addressee=1,
                deadline=app.clock.now()+20 if explicit_deadline else 0,max_lines=1)
            meta=TurnMeta('borrowed-turn',why='discord:reply',answers_human=True)
            turn_task=asyncio.create_task(app.engine.speak_meta(turn,meta))
            async with asyncio.timeout(5):
                await waiting.wait()
            active=app.engine._current[turn_task]  # speak_meta owns a copy of the supplied Turn
            base=active.deadline
            await app.clock.advance(180,180)
            assert gate.blocked and not turn_task.done()
            assert not [c for c in app.llm.calls if 'messages' in c]
            assert meta.borrow_credit.seconds==180
            assert app.engine._deadline(active,meta)==base+180
            release.set()
            await borrow
            lines=await finish(app,turn_task)
            assert lines and meta.said==1
            post=app.store.conn.execute("SELECT text FROM discord_message WHERE turn='borrowed-turn'").fetchone()
            assert post is not None and post[0]==lines[0]
            assert not app.engine.scheduler.expired(app.engine._deadline(active,meta))
        finally:
            release.set()
            for task in (turn_task,borrow):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(*(t for t in (turn_task,borrow) if t is not None),return_exceptions=True)
            await app.stop()
    run(go())


def test_real_embedder_runs_while_all_live_slots_wait_for_active_borrow(tmp_path):
    """Exercise real live_ready recovery, PauseGate child tasks and the shared Embedder."""
    async def go():
        app=mind_app(tmp_path)
        app.arbiter.admin=FakeAdmin([app.cfg.models.live,app.cfg.models.embedding])
        gate=enable_borrow(app)
        borrowed,release=asyncio.Event(),asyncio.Event()
        async def render():
            async with app.arbiter.borrow('request:101'):
                borrowed.set()
                await release.wait()
        borrow=asyncio.create_task(render())
        chats=[]
        async def chat(llm):
            await app.engine.live_admission()
            return await llm.chat(app.cfg.models.live,[],options={})
        try:
            await borrowed.wait()
            chats=[asyncio.create_task(app.scheduler.submit(Priority.REPLY_HUMAN,chat))
                   for _ in range(app.scheduler._max)]
            for _ in range(30):
                await asyncio.sleep(0)
            assert len(app.scheduler._running)==app.scheduler._max
            assert all(e.parked for e in app.scheduler._running)
            async with asyncio.timeout(3):
                vectors=await app.embedder.embed(['an invented kitchen memory'],Priority.EXTRACTION)
            assert len(vectors)==1 and gate.blocked
            assert not [c for c in app.llm.calls if 'messages' in c]
            release.set()
            await borrow
            assert all(result.text for result in await asyncio.gather(*chats))
            assert not gate.blocked
        finally:
            release.set()
            for task in chats+[borrow]:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*chats,borrow,return_exceptions=True)
            await app.stop()
    run(go())
