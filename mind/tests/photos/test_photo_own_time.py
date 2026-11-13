import asyncio
from dataclasses import replace
from datetime import datetime
from zoneinfo import ZoneInfo
from conftest import run
from photos.test_send import setup, stock, ON
from photos.test_pipeline_photo import finish


def test_deferred_reply_reserves_only_when_it_actually_speaks(tmp_path):
    async def go():
        app=setup(tmp_path,ON.replace('selfies=3','selfies=3\ndm_own_time=true'))
        app.clock._start=datetime(2026,10,30,3,tzinfo=ZoneInfo('America/New_York'))
        app.clock._t=app.clock._start.timestamp()
        original=app.personas.get
        p=original(101)
        p=replace(p,identity=replace(p.identity,region='US'),real_life=replace(p.real_life,night_owl=False))
        app.personas.get=lambda guid:p if guid==101 else original(guid)
        stock(app)
        queued=[]
        app.engine._queue=lambda turns:queued.extend(turns)
        app.llm._script=lambda *a:'my little evening expedition went well'
        try:
            posted=await app.discord.handle({'action':'post','channel':'dm:101','text':'how was your walk?'})
            assert posted['ok'] and app.discord_talk.owes(101)
            assert not queued and not app.photos._in_flight
            assert app.store.conn.execute("SELECT status FROM selfie_snap").fetchone()[0]=='ready'
            await app.clock.advance(17*3600,17*3600)
            await app.server.lease.tick(app.clock.now())
            await app.server.lease.handback()
            app.discord_talk.tick(app.clock.now())
            assert len(queued)==1 and not app.photos._in_flight
            assert await finish(app,asyncio.create_task(app.engine.speak_meta(*queued[0])))
            assert app.store.conn.execute('SELECT count(*) FROM discord_attachment').fetchone()[0]==1
            assert not app.photos._in_flight
        finally:await app.stop()
    run(go())
