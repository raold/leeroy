"""Full attachment and socket transport with the FakeBridge; all data is synthetic."""
import asyncio
from contextlib import asynccontextmanager

import pytest

from conftest import HUMAN
from test_app_lifecycle import App, CAST, chat, fake_clock, hello, seed_db, toml
from leeroy_mind.app import MindApp
from leeroy_mind.config import load_config
from leeroy_mind.testing.fake_bridge import FakeBridge
from leeroy_mind.testing.fake_llm import FakeLLM


@asynccontextmanager
async def mind(tmp_path,enabled=True):
    clock=fake_clock()
    bridge=FakeBridge([hello(HUMAN),CAST])
    port=await bridge.start()
    (tmp_path/"mind.toml").write_text(toml(port)+"\n[talk]\nchattiness=4\nwhispers_per_hour=0\n"
        "openers_per_hour=0\nrealism=true\n[banter]\nenabled="+str(enabled).lower()+"\n")
    seed_db(tmp_path/"mind.db",clock)
    cfg=load_config(tmp_path/"mind.toml")
    replies=iter(["the repair bill needs its own banker", "a helmet for the wallet next time",
        "the armour smith owes us a loyalty discount", "i brought spare bootlaces",
        "at least the road home is familiar", "the kettle will still be warm", "i will save you a biscuit"])
    llm=FakeLLM(lambda *_:next(replies,"the kettle is still warm"),
        models={cfg.models.live,cfg.models.embedding,cfg.models.reflection})
    app=MindApp(cfg,tmp_path,llm=llm,clock=clock,rng_seed=4)
    task=asyncio.create_task(app.run())
    r=App(app,bridge,llm,clock)
    try:
        await r.wait(lambda: app.world.human_online and 101 in app.world.members)
        yield r
    finally:
        await app.stop()
        await asyncio.wait_for(task,5)
        await bridge.stop()


def banter_rows(r):
    return r.app.store.conn.execute("SELECT * FROM utterance WHERE why LIKE 'banter:%' AND dropped IS NULL").fetchall()


def test_enabled_event_through_fakebridge_answers_owner_interruption(tmp_path,caplog):
    async def go():
        async with mind(tmp_path) as r:
            assert r.app.banter is not None
            assert "banter_jokes" in {s.name for s in r.app.night_runner.stages}
            await r.bridge.push({"type":"death","guid":HUMAN,"map":0,"killer_entry":42})
            await r.wait(lambda: bool(r.app.banter.pending))
            await r.until(lambda: bool(banter_rows(r)),timeout=10)
            before=len(banter_rows(r))
            await r.bridge.push(chat("Arkon, are you there?"))
            await r.wait(lambda: r.app.store.conn.execute("SELECT 1 FROM chat_line WHERE origin='human'").fetchone())
            await r.until(lambda: not r.app.banter.active,timeout=10)
            assert len(banter_rows(r))==before
            await r.until(lambda: r.app.store.conn.execute("SELECT 1 FROM utterance "
                "WHERE kind='reply_human' AND dropped IS NULL").fetchone(),timeout=10)
            assert r.bridge.commands
    asyncio.run(go())
    assert not any("repair bill" in row.getMessage() for row in caplog.records if "banter" in row.name)


def test_switch_off_has_no_attachment_hooks_or_night_work(tmp_path):
    async def go():
        async with mind(tmp_path,False) as r:
            assert r.app.banter is None
            assert "banter_jokes" not in {s.name for s in r.app.night_runner.stages}
            assert not r.app.store.conn.execute("SELECT 1 FROM meta WHERE key LIKE 'banter:%'").fetchone()
    asyncio.run(go())
