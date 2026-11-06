"""P2 Task 12: the `selfie` op on the mind's control endpoint: strict arguments in tune_args'
style, a pick, undo or "4 more" one at a time with the lock held until the member's files are
written, status at any time, answers carrying the member's guid, content-free logs. Every person
here is invented."""
import asyncio
import contextlib
import json
import logging
import threading

import pytest

from conftest import TEST_CAST, run
from leeroy_mind import endpoint
from leeroy_mind.endpoint import BadRequest, ControlCfg, ControlEndpoint, parse_request, selfie_args
from leeroy_mind.selfies import files
from leeroy_mind.selfies.files import candidate, chosen
from leeroy_mind.selfies.store import queued_sets

from .fakes import mind_app, ready

KEY = "K" * 43
ON = "[selfies]\nenabled = true\n"


def line(**body) -> bytes:
    return (json.dumps(body) + "\n").encode()


def _pick(guid, set_no, idx):
    return {"action": "pick", "guid": guid, "set": set_no, "index": idx}


def test_selfie_args_take_each_action_in_its_one_strict_form():
    assert selfie_args({"action": "pick", "guid": 101, "set": 2, "index": 3}) == {"action": "pick", "guid": 101, "set": 2, "index": 3}
    assert selfie_args({"action": "undo", "pick": 7}) == {"action": "undo", "pick": 7}
    assert selfie_args({"action": "status"}) == {"action": "status"}
    assert selfie_args({"action": "more", "guid": 101}) == {"action": "more", "guid": 101}
    edge = {"action": "pick", "guid": (1 << 32) - 1, "set": (1 << 31) - 1, "index": 0}
    assert selfie_args(edge) == edge


@pytest.mark.parametrize("args,words", [({"action": "pick", "guid": 101, "set": 1, "index": 4}, "index"),
    ({"action": "pick", "guid": "101", "set": 1, "index": 0}, "guid"), ({"action": "more"}, "exactly guid"),
    ({"action": "status", "x": 1}, "takes no arguments"), ({"action": "erase"}, "one of")])
def test_selfie_args_refuse_everything_else(args, words):
    with pytest.raises(BadRequest, match=words):
        selfie_args(args)


@pytest.mark.parametrize("args,words", [
    (None, "an action"),
    ([], "an action"),
    ({"guid": 101}, "an action"),
    ({"action": "pick", "guid": 0, "set": 1, "index": 0}, "guid"),
    ({"action": "pick", "guid": 1 << 32, "set": 1, "index": 0}, "guid"),
    ({"action": "pick", "guid": True, "set": 1, "index": 0}, "guid"),
    ({"action": "pick", "guid": 101.0, "set": 1, "index": 0}, "guid"),
    ({"action": "pick", "guid": 101, "set": 0, "index": 0}, "set"),
    ({"action": "pick", "guid": 101, "set": 1 << 31, "index": 0}, "set"),
    ({"action": "pick", "guid": 101, "set": "1", "index": 0}, "set"),
    ({"action": "pick", "guid": 101, "set": 1, "index": -1}, "index"),
    ({"action": "pick", "guid": 101, "set": 1, "index": False}, "index"),
    ({"action": "pick", "guid": 101, "set": 1}, "exactly guid, index, set"),
    ({"action": "pick", "guid": 101, "set": 1, "index": 0, "name": "x"}, "exactly"),
    ({"action": "more", "guid": None}, "guid"),
    ({"action": "more", "guid": 101, "set": 1}, "exactly guid"),
    ({"action": "undo", "pick": 0}, "pick"),
    ({"action": "undo", "pick": "7"}, "pick"),
    ({"action": "undo", "pick": 1 << 63}, "pick"),
    ({"action": "undo"}, "exactly pick"),
])
def test_selfie_args_refuse_every_other_shape_and_value(args, words):
    with pytest.raises(BadRequest, match=words):
        selfie_args(args)


def test_parse_request_needs_args_for_selfie():
    with pytest.raises(BadRequest, match="selfie needs args"):
        parse_request(line(v=1, key=KEY, op="selfie"), KEY)
    assert parse_request(line(v=1, key=KEY, op="selfie", args={"action": "status"}), KEY) == (
        "selfie", {"action": "status"})
    assert parse_request(line(v=1, key=KEY, op="selfie", args=_pick(101, 2, 3)), KEY) == (
        "selfie", _pick(101, 2, 3))
    with pytest.raises(BadRequest, match="index"):
        parse_request(line(v=1, key=KEY, op="selfie", args=_pick(101, 2, 9)), KEY)
    with pytest.raises(BadRequest, match="op is one of .*selfie"):
        parse_request(line(v=1, key=KEY, op="photo", args={"action": "status"}), KEY)


# -- the endpoint with a fake service -----------------------------------------------------------

class FakeSelfies:
    def __init__(self):
        self.calls = []
        self.gate = None

    async def handle(self, args):
        self.calls.append(args)
        if self.gate is not None and args["action"] != "status":
            await self.gate.wait()
        return {"ok": True, "message": f"did {args['action']}"}


class FakeApp:
    def __init__(self, data_dir):
        self.data_dir = data_dir
        self.stopping = False
        self.guild = None
        self.tuning = None
        self.selfies = FakeSelfies()


@contextlib.asynccontextmanager
async def served(app):
    ep = ControlEndpoint(app, ControlCfg(), key=KEY)
    task = asyncio.create_task(ep.serve())
    await asyncio.wait_for(ep.listening.wait(), 5)
    try:
        yield ep
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def ask(ep, args):
    return await asyncio.to_thread(endpoint.request, ep.port, KEY, "selfie", args, timeout=10)


BUSY = {"ok": False, "error": "another photo change is still being written"}


def test_photo_changes_go_one_at_a_time_and_status_answers_meanwhile(tmp_path):
    async def go():
        app = FakeApp(tmp_path)
        async with served(app) as ep:
            app.selfies.gate = asyncio.Event()
            first = asyncio.create_task(ask(ep, _pick(101, 1, 0)))
            while not app.selfies.calls:
                await asyncio.sleep(0.01)
            for args in (_pick(102, 1, 0), {"action": "undo", "pick": 1},
                         {"action": "more", "guid": 101}):
                assert await ask(ep, args) == BUSY  # a second page, a double click
            assert await ask(ep, {"action": "status"}) == {"ok": True, "message": "did status"}
            assert len(app.selfies.calls) == 2  # the refused ones never reached the service
            app.selfies.gate.set()
            assert await first == {"ok": True, "message": "did pick"}
            assert await ask(ep, {"action": "more", "guid": 101}) == {"ok": True,
                                                                     "message": "did more"}
            app.selfies = None
            assert await ask(ep, {"action": "status"}) == {
                "ok": False, "error": "photos are not running in this mind"}
            app.selfies = FakeSelfies()
            app.stopping = True
            for args in ({"action": "status"}, _pick(101, 1, 0)):
                assert await ask(ep, args) == {"ok": False, "error": "the mind is stopping"}
            assert app.selfies.calls == []
    run(go())


# -- the real service in a real mind ------------------------------------------------------------

def _names(app):
    personas = [app.personas.base(g) for g in TEST_CAST]
    return [p.character_name for p in personas] + [p.identity.real_name for p in personas]


def test_a_pick_through_the_endpoint_answers_and_logs_content_free(tmp_path, caplog):
    async def go():
        app = mind_app(tmp_path, toml=ON)
        try:
            data = app.data_dir
            ready(app.store, data, 101, 1)
            async with served(app) as ep:
                got = await ask(ep, _pick(101, 1, 2))
                n = got["pick"]
                assert got == {"ok": True, "pick": n, "guid": 101,
                               "message": f"Photo set (change #{n})."}
                assert chosen(data, 101).read_bytes() == candidate(data, 101, 1, 2).read_bytes()
                st = await ask(ep, {"action": "status"})
                assert (st["ok"], st["enabled"], st["chosen"], st["waiting"]) == (True, True, 1, 0)
                assert "guid" not in st  # status broadcasts nothing
                assert [(r["id"], r["guid"], r["undoable"]) for r in st["recent"]] == [
                    (n, 101, True)]
                back = await ask(ep, {"action": "undo", "pick": n})
                assert back == {"ok": True, "pick": back["pick"], "guid": 101,
                                "message": f"Undone (change #{back['pick']})."}
                assert not chosen(data, 101).exists()
                more = await ask(ep, {"action": "more", "guid": 101})
                assert more == {"ok": True, "set": 2, "guid": 101,
                                "message": "Four more will be rendered tonight."}
                assert await ask(ep, _pick(104, 1, 0)) == {
                    "ok": False, "error": "set 1 is not ready for this member"}
            names = _names(app)
        finally:
            app.store.close()
        return names
    with caplog.at_level(logging.INFO, logger="leeroy_mind"):
        names = run(go())
    text = caplog.text
    assert "control_request: op=selfie action=pick outcome=ok" in text
    for said in ("action=status outcome=ok", "action=undo outcome=ok", "action=more outcome=ok",
                 "action=pick outcome=refused"):
        assert f"control_request: op=selfie {said}" in text
    for name in names:
        assert name not in text
    assert "not ready for this member" not in text


def test_the_lock_is_held_until_the_photo_file_is_written(tmp_path, monkeypatch):
    real = files.sync_chosen
    entered, release = threading.Event(), threading.Event()

    def slow(data_dir, guid, cur):  # the console's face endpoint holds chosen.jpg a while
        if guid == 101:
            entered.set()
            release.wait(10)
        return real(data_dir, guid, cur)

    async def go():
        app = mind_app(tmp_path, toml=ON)
        try:
            data = app.data_dir
            ready(app.store, data, 101, 1)
            ready(app.store, data, 102, 1)
            monkeypatch.setattr(files, "sync_chosen", slow)
            monkeypatch.setattr(endpoint, "PREVIEW_S", 0.05)  # no timeout of any kind applies
            async with served(app) as ep:
                ep.raid_s = 0.05
                first = asyncio.create_task(ask(ep, _pick(101, 1, 0)))
                assert await asyncio.to_thread(entered.wait, 10)
                # committed, its file still being written off the loop: the next change waits
                assert await ask(ep, _pick(102, 1, 0)) == BUSY
                assert (await ask(ep, {"action": "status"}))["chosen"] == 1
                await asyncio.sleep(0.2)
                assert not first.done() and not chosen(data, 101).exists()
                release.set()
                got = await first
                assert got["ok"] and got["message"] == f"Photo set (change #{got['pick']})."
                assert chosen(data, 101).read_bytes() == candidate(data, 101, 1, 0).read_bytes()
                assert (await ask(ep, _pick(102, 1, 0)))["ok"] is True
        finally:
            release.set()
            app.store.close()
    run(go())


def test_photos_still_pick_and_undo_while_rendering_is_off(tmp_path, caplog):
    async def go():
        app = mind_app(tmp_path)  # no [selfies] table: rendering off, the default
        try:
            assert app.selfies.cfg.enabled is False
            data = app.data_dir
            ready(app.store, data, 101, 1)  # a set an earlier night rendered
            async with served(app) as ep:
                got = await ask(ep, _pick(101, 1, 1))
                assert got["ok"] and got["guid"] == 101
                assert chosen(data, 101).read_bytes() == candidate(data, 101, 1, 1).read_bytes()
                back = await ask(ep, {"action": "undo", "pick": got["pick"]})
                assert back["ok"] and back["guid"] == 101 and not chosen(data, 101).exists()
                assert await ask(ep, {"action": "more", "guid": 101}) == {
                    "ok": False,
                    "error": "rendering is off in mind.toml ([selfies] enabled = false)"}
                assert queued_sets(app.store) == []
                st = await ask(ep, {"action": "status"})
                assert st["ok"] is True and st["enabled"] is False and st["waiting"] == 1
        finally:
            app.store.close()
    with caplog.at_level(logging.INFO, logger="leeroy_mind"):
        run(go())
    assert "control_request: op=selfie action=more outcome=refused" in caplog.text
