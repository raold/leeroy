import asyncio
from conftest import run
from photos.test_send import setup, stock, reply
from photos.test_pipeline_photo import finish
from leeroy_mind.nightly.romance import picture_available, strong_evidence


def test_picture_stage_only_unlocks_with_sender_and_pair_delivery(tmp_path):
    async def go():
        app=setup(tmp_path)
        stock(app)
        app.llm._script=lambda *a:'this seemed worth sharing'
        try:
            assert picture_available(app.store)
            assert not strong_evidence(app.store,101,app.human_guid,'picture',0,app.clock.now(),None)
            assert await finish(app,asyncio.create_task(app.engine.speak_meta(*reply(app))))
            now=app.clock.now()
            assert strong_evidence(app.store,101,app.human_guid,'picture',0,now,None)
            assert not strong_evidence(app.store,102,app.human_guid,'picture',0,now,None)
            assert not strong_evidence(app.store,101,999,'picture',0,now,None)
            assert not strong_evidence(app.store,101,app.human_guid,'picture',0,now+7*86400+1,None)
        finally:await app.stop()
    run(go())


def test_picture_stage_is_disabled_with_sender_off(tmp_path):
    app=setup(tmp_path,'')
    try:assert not picture_available(app.store)
    finally:app.store.close()
