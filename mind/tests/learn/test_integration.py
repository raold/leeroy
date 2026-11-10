import asyncio
from dataclasses import replace
import pytest

from conftest import HUMAN
from test_app_lifecycle import App, CAST, fake_clock, hello, seed_db, toml
from leeroy_mind.app import MindApp
from leeroy_mind.config import load_config
from leeroy_mind.llm.scheduler import Priority
from leeroy_mind.learn.recipe import describe
from leeroy_mind.talk.pipeline import TurnMeta
from leeroy_mind.talk.turns import Turn, TurnKind
from leeroy_mind.testing.fake_bridge import FakeBridge
from leeroy_mind.testing.fake_llm import FakeLLM


@pytest.mark.parametrize("setting", ["true", "false", "1"], ids=["enabled", "disabled", "invalid"])
def test_fakebridge_delivery_recipe_and_app_stage(tmp_path, setting):
    # Parametrization supplies one scalar setting; strict parsing decides the expected state.
    async def run():
        clock = fake_clock()
        bridge = FakeBridge([hello(HUMAN), CAST])
        port = await bridge.start()
        (tmp_path / "mind.toml").write_text(toml(port) + "\n[talk]\nchattiness=0\n"
                                          + "[learn]\nenabled=" + setting + "\n")
        seed_db(tmp_path / "mind.db", clock)
        cfg = load_config(tmp_path / "mind.toml")
        def answer(*_):
            # Tuning can change while the model works. Attribution must retain the
            # prompt's old dial values, not the settings visible at acknowledgement.
            app.engine.cfg = replace(app.engine.cfg, romance=replace(app.engine.cfg.romance, dial=1))
            return "the invented cart wheel needs another spoke"
        llm = FakeLLM(answer,
                      models={cfg.models.live, cfg.models.embedding, cfg.models.reflection})
        app = MindApp(cfg, tmp_path, llm=llm, clock=clock, rng_seed=1)
        task = asyncio.create_task(app.run())
        r = App(app, bridge, llm, clock)
        try:
            await r.wait(lambda: app.world.human_online and 101 in app.world.members)
            turn = Turn(TurnKind.REPLY_HUMAN, 101, "whisper", to_guid=HUMAN,
                        addressee=HUMAN, priority=Priority.REPLY_HUMAN, max_lines=1)
            meta = TurnMeta(app.engine._new_id(), answers_human=True, whisper_reply=True)
            expected_hash = describe(turn, app.personas.get(101), app.engine.cfg).dials_hash
            sent = asyncio.create_task(app.engine._speak(turn, meta))
            await r.until(sent.done)
            assert await sent
            expected = setting == "true"
            assert ("rewards" in [s.name for s in app.night_runner.stages]) is expected
            count = app.store.conn.execute("SELECT count(*) FROM talk_recipe").fetchone()[0]
            assert count == int(expected)
            if expected:
                actual = app.store.conn.execute("SELECT dials_hash FROM talk_recipe").fetchone()[0]
                assert actual == expected_hash
                assert actual != describe(turn, app.personas.get(101), app.engine.cfg).dials_hash
            assert app.store.conn.execute("SELECT count(*) FROM chat_line WHERE origin='mind'").fetchone()[0] == 1
        finally:
            await app.stop()
            await asyncio.wait_for(task, 5)
            await bridge.stop()
    asyncio.run(run())
