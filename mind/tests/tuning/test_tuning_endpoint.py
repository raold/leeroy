"""T6: the `tune` op on the mind's control endpoint: strict arguments in raid_args' style, one
change at a time, effective at once in the running mind, content-free logs."""
import asyncio
import contextlib
import json
import logging
from datetime import datetime, timezone

import pytest

from conftest import HUMAN, run, seed_test_cast
from leeroy_mind import endpoint
from leeroy_mind.app import MindApp
from leeroy_mind.config import load_config
from leeroy_mind.endpoint import BadRequest, ControlCfg, ControlEndpoint, parse_request, tune_args
from leeroy_mind.personas.io import load_persona
from leeroy_mind.protocol import Member
from dbtemplate import open_migrated
from leeroy_mind.storage.store import Store
from leeroy_mind.testing.fake_clock import FakeClock
from leeroy_mind.testing.fake_llm import FakeLLM
from leeroy_mind.tuning.effective import dials_of

KEY = "K" * 43
CLOCK = datetime(2026, 10, 29, 20, 0, tzinfo=timezone.utc)


def line(**body) -> bytes:
    return (json.dumps(body) + "\n").encode()


SET = {"action": "set", "scope": "member", "target": "102", "dial": "talkative", "value": 8,
       "previewed": False}


def test_tune_args_take_each_action_in_its_one_strict_form():
    assert tune_args({"action": "status"}) == {"action": "status"}
    assert tune_args(SET) == SET
    assert tune_args({**SET, "value": None})["value"] is None
    assert tune_args({"action": "undo", "change": 3}) == {"action": "undo", "change": 3}
    assert tune_args({"action": "revert", "to": 0}) == {"action": "revert", "to": 0}
    group = {"action": "group", "id": None, "name": "women", "where": {"gender": ["female"]}}
    assert tune_args(group) == group
    assert tune_args({"action": "retire", "id": 2}) == {"action": "retire", "id": 2}
    preview = {"action": "preview", "scope": "world", "target": "", "dial": "chattiness",
               "value": 7}
    assert tune_args(preview) == preview
    assert parse_request(line(v=1, key=KEY, op="tune", args={"action": "status"}), KEY) == (
        "tune", {"action": "status"})


@pytest.mark.parametrize("args, words", [
    (None, "an action"),
    ({"action": "write"}, "an action"),
    ({"action": "status", "x": 1}, "takes no arguments"),
    ({**SET, "value": 8.0}, "whole number"),
    ({**SET, "value": True}, "whole number"),
    ({**SET, "value": 11}, "whole number"),
    ({**SET, "scope": "guild"}, "scope"),
    ({**SET, "target": 102}, "target"),
    ({**SET, "target": "x" * 11}, "target"),
    ({**SET, "dial": "Talk"}, "dial"),
    ({**SET, "previewed": "no"}, "previewed"),
    ({k: v for k, v in SET.items() if k != "previewed"}, "exactly"),
    ({"action": "undo", "change": 0}, "change"),
    ({"action": "undo", "change": "3"}, "change"),
    ({"action": "revert", "to": -1}, "to"),
    ({"action": "group", "id": None, "name": "x" * 41, "where": {}}, "name"),
    ({"action": "group", "id": None, "name": "w", "where": []}, "where"),
    ({"action": "group", "id": None, "name": "w", "where": {"a": [{"b": 1}]}}, "where"),
    ({"action": "retire", "id": None}, "id"),
    ({"action": "preview", "scope": "world", "target": "", "dial": "chattiness"}, "exactly"),
])
def test_tune_args_refuse_everything_else(args, words):
    with pytest.raises(BadRequest, match=words):
        tune_args(args)


# -- the endpoint with a fake service -----------------------------------------------------------

class FakeTuning:
    def __init__(self):
        self.calls = []
        self.gate = None

    async def handle(self, args):
        self.calls.append(args)
        if self.gate is not None:
            await self.gate.wait()
        return {"ok": True, "message": f"did {args['action']}"}


class FakeApp:
    def __init__(self, data_dir):
        self.data_dir = data_dir
        self.stopping = False
        self.guild = None
        self.tuning = FakeTuning()


@contextlib.asynccontextmanager
async def served(tmp_path, app=None):
    app = app or FakeApp(tmp_path)
    ep = ControlEndpoint(app, ControlCfg(), key=KEY)
    task = asyncio.create_task(ep.serve())
    await asyncio.wait_for(ep.listening.wait(), 5)
    try:
        yield app, ep
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def ask(ep, op, args=None):
    return await asyncio.to_thread(endpoint.request, ep.port, KEY, op, args, timeout=10)


def test_tune_reaches_the_service_one_change_at_a_time(tmp_path, caplog):
    async def go():
        async with served(tmp_path) as (app, ep):
            assert await ask(ep, "tune", {"action": "status"}) == {"ok": True, "message": "did status"}
            app.tuning.gate = asyncio.Event()
            first = asyncio.create_task(ask(ep, "tune", SET))
            while len(app.tuning.calls) < 2:
                await asyncio.sleep(0.01)
            assert await ask(ep, "tune", {"action": "undo", "change": 1}) == {
                "ok": False, "error": "another tuning change is still being written"}
            app.tuning.gate.set()
            assert (await first)["ok"] is True
            app.tuning = None
            assert await ask(ep, "tune", {"action": "status"}) == {
                "ok": False, "error": "tuning is not running in this mind"}
            app.stopping = True
            assert (await ask(ep, "tune", {"action": "status"}))["error"] == "the mind is stopping"
    with caplog.at_level(logging.INFO, logger="leeroy_mind"):
        run(go())
    assert "op=tune" in caplog.text or '"op": "tune"' in caplog.text


# -- the real service in a real app -------------------------------------------------------------

def _app(root):
    (root / "mind.toml").write_text("[talk]\nchattiness = 4\n", encoding="utf-8")
    s = Store(open_migrated(root / "mind.db"), clock=FakeClock(CLOCK))
    try:
        seed_test_cast(s)
        s.upsert_cast([Member(guid=HUMAN, name="Testmage", race=5, class_=8, gender=0, level=80,
                              rank=0, online=False, map=0, zone=0, spec_tab=-1)], 1.0)
        s.set_meta("human_guid", str(HUMAN))
    finally:
        s.close()
    return MindApp(load_config(root / "mind.toml"), root, llm=FakeLLM(lambda m, msg: "ok"),
                   clock=FakeClock(CLOCK))


def test_a_change_is_live_from_the_next_read_and_undo_takes_it_back(tmp_path, caplog):
    async def go():
        app = _app(tmp_path)
        try:
            svc = app.tuning
            r = await svc.handle({**SET, "target": "103"})
            assert r["ok"] and r["change"] == 1 and r["reach"] == 1
            assert dials_of(app.personas.get(103)).values == {"talkative": 8}
            r = await svc.handle({"action": "set", "scope": "world", "target": "",
                                  "dial": "chattiness", "value": 9, "previewed": True})
            assert r["ok"] and app.cfg.talk.chattiness == 9 and app.engine.budget._c == 9
            assert app.file_cfg.talk.chattiness == 4
            r = await svc.handle({"action": "set", "scope": "group", "target": "everyone",
                                  "dial": "humour", "value": 2, "previewed": False})
            assert r["reach"] == 5
            assert (await svc.handle({"action": "undo", "change": 2}))["ok"]
            assert app.cfg.talk.chattiness == 4 and app.engine.budget._c == 4
            assert app.store.conn.execute(
                "SELECT previewed FROM tuning_change WHERE id=2").fetchone()[0] == 1
            refused = await svc.handle({**SET, "target": str(HUMAN)})
            assert refused == {"ok": False, "error": "your own character is never tuned"}
            refused = await svc.handle({**SET, "target": "101", "dial": "forward"})
            assert not refused["ok"] and "Men stay friends" in refused["error"]
            st = await svc.handle({"action": "status"})
            assert st["ok"] and st["version"] == 4 and st["pinned_members"] == 1
        finally:
            app.store.close()
    with caplog.at_level(logging.INFO, logger="leeroy_mind"):
        run(go())
    text = caplog.text
    for g in (101, 102, 103):
        assert load_persona(Store(open_migrated(tmp_path / "mind.db"), clock=FakeClock(CLOCK)),
                            g).character_name not in text
