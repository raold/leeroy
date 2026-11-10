"""Real console HTTP and mind control socket; synthetic SQLite and process boundaries."""
import asyncio
import json
import os
import sys
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import run
from console.rig import http, make_app, running
from leeroy_mind.console.mindlink import MindLink
from leeroy_mind.console.netinfo import Conn, LISTEN
from leeroy_mind.lifecycle import Outcome
from leeroy_mind.realm.action_store import ActionStore
from selfies.fakes import until
from test_opscontrol import app, ep, add_root, ROOT_ID

CASES = [{"action":"release","id":ROOT_ID,"reason":"PRIVATE_REASON"},
         {"action":"offline_stop","id":ROOT_ID}, {"action":"render"},
         {"action":"snapshot","label":"review"}]


@pytest.fixture
def cli_process(monkeypatch):
    """Replace only OS launch; execute the exact Realm CLI on its scratch data dir."""
    import contextlib
    import io
    from leeroy_mind.realm.cli import realm_main
    from leeroy_mind.console import opsactions
    def execute(argv, **kwargs):
        assert argv[1:4] == ["-m","leeroy_mind","realm"]
        assert kwargs["stdin"] == kwargs["stdout"] == kwargs["stderr"] == -3
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
            code = realm_main(argv[4:])
        if code:
            raise ValueError(output.getvalue().splitlines()[-1])
        return SimpleNamespace(returncode=code)
    monkeypatch.setattr(opsactions.subprocess,"run",execute)


@asynccontextmanager
async def served(mind):
    endpoint = ep(mind)
    task = asyncio.create_task(endpoint.serve())
    await asyncio.wait_for(endpoint.listening.wait(), 5)
    handle = running(mind.data_dir)
    handle.found = [SimpleNamespace(pid=os.getpid())]
    link = MindLink(mind.data_dir, tcp=lambda: [Conn("127.0.0.1",endpoint.port,"0.0.0.0",0,LISTEN,os.getpid())])
    console = make_app(mind.data_dir, mind=handle, link=link, ops_actions=True)
    await console.start()
    try:
        yield console, endpoint
    finally:
        await console.close()
        task.cancel()
        with suppress(asyncio.CancelledError): await task


async def post(console, args, **options):
    return await http(console,"POST","/api/ops/action",body=args,origin=options.pop("origin", True),**options)


@pytest.mark.parametrize("args", CASES)
@pytest.mark.parametrize("gate", ["key", "origin", "wrong_origin", "no_control", "off", "busy"])
def test_every_ops_action_uses_console_gates(app, args, gate):
    async def go():
        async with served(app) as (console, endpoint):
            options = {}
            if gate == "key": options["key"] = False
            if gate == "origin": options["origin"] = False
            if gate == "wrong_origin": options["origin"] = "https://evil.invalid"
            if gate == "no_control": console.controls.enabled = False
            if gate == "off": console.s.ops_actions = False
            if gate == "busy": console.controls.busy = ("mind","restart")
            reply = await post(console, {**args,"phase":"preview"}, **options)
            assert reply.status == (409 if gate == "busy" else 403)
            assert not endpoint.ops.busy and not console._ops_actions.busy
            console.controls.busy = None
    run(go())


def test_confirmation_binding_exact_id_expiry_and_current_state(app, monkeypatch):
    add_root(app.store.conn)
    async def go():
        async with served(app) as (c, endpoint):
            args = CASES[0]
            preview = (await post(c,{**args,"phase":"preview"})).json()
            assert preview["ok"] and preview["details"]["id"] == ROOT_ID
            wrong = await post(c,{**args,"phase":"confirm","token":preview["token"],"typed_id":"wrong"})
            assert wrong.status == 409 and not c.mind.acts
            preview = (await post(c,{**args,"phase":"preview"})).json()
            changed = await post(c,{**args,"reason":"changed","phase":"confirm","token":preview["token"],"typed_id":ROOT_ID})
            assert changed.status == 409
            preview = (await post(c,{**args,"phase":"preview"})).json()
            token, _, saved = c._ops_actions.preview
            c._ops_actions.preview = (token, 0, saved)
            expired = await post(c,{**args,"phase":"confirm","token":preview["token"],"typed_id":ROOT_ID})
            assert expired.status == 409
            preview = (await post(c,{**args,"phase":"preview"})).json()
            app.world.human_online = True
            refused = await post(c,{**args,"phase":"confirm","token":preview["token"],"typed_id":ROOT_ID})
            assert refused.status == 409 and "in game" in refused.json()["error"]
            assert not c.mind.acts
    run(go())


def test_snapshot_confirmed_once_and_status_survives_caller(app, caplog):
    async def go():
        async with served(app) as (c, endpoint):
            args = CASES[3]
            assert (await post(c,{**args,"phase":"confirm","token":"invented"})).status == 409
            preview = (await post(c,{**args,"phase":"preview"})).json()
            request = {**args,"phase":"confirm","token":preview["token"]}
            with caplog.at_level("INFO"):
                reply = await post(c,request)
                assert reply.status == 202
                assert (await http(c,"POST","/api/control",origin=True,body={"service":"mind","action":"stop"})).status == 409
                await c._ops_actions.task
            state = (await http(c,"GET","/api/ops/action")).json()
            assert state["operation"]["state"] == "done" and state["enabled"]
            assert (await post(c,request)).status == 409
            assert len(list((app.data_dir/"snapshots").glob("*.db"))) == 1
            assert sum("ops_action action=" in r.message for r in caplog.records) == 1
    run(go())


@pytest.mark.parametrize("fails", ["none", "cli", "stop", "restart", "both"])
def test_release_uses_cli_and_restarts_after_refusal(app, fails, caplog, cli_process):
    journal = add_root(app.store.conn)
    async def go():
        async with served(app) as (c, endpoint):
            c.s.root = Path(__file__).resolve().parents[3]
            c.mind.python = Path(sys.executable)
            def stop():
                c.mind.acts.append("clean_stop")
                if fails == "stop": raise ValueError("clean stop refused")
                return Outcome(0,"stopped","stopped")
            c.mind.clean_stop = stop
            if fails in {"cli", "both"}:
                def failed(*args): raise ValueError("its transport is not settled")
                c._ops_actions.release = failed
            if fails in {"restart", "both"}:
                def start(action):
                    c.mind.acts.append(action)
                    return Outcome(1,"failed","stopped")
                c.mind.act = start
            args = CASES[0]
            preview = (await post(c,{**args,"phase":"preview"})).json()
            with caplog.at_level("INFO"):
                reply = await post(c,{**args,"phase":"confirm","token":preview["token"],"typed_id":ROOT_ID})
                assert reply.status == 202
                await c._ops_actions.task
            app.store.conn.commit()
            record = ActionStore(app.store.conn).get(ROOT_ID)
            assert c.mind.acts == (["clean_stop"] if fails == "stop" else ["clean_stop","start"])
            assert (record.result_reason == "abandoned") == (fails in {"none","restart"}), c._ops_actions.operation["message"]
            assert (c._ops_actions.operation["state"] == "done") == (fails == "none")
            if fails in {"restart","both"}:
                assert c._ops_actions.operation["restart_outcome"] == "failed"
                assert c._ops_actions.operation["release_outcome"] == ("released" if fails=="restart" else "refused")
                assert ("was released" if fails=="restart" else "was refused") in c._ops_actions.operation["message"]
            assert "PRIVATE_REASON" not in caplog.text
    run(go())


@pytest.mark.parametrize("body", [[], None, {"phase":[]}, {"phase":"preview","action":"render","reason":"extra"},
    {"phase":"confirm","action":"snapshot","label":"a","token":"x","typed_id":"extra"}])
def test_bad_http_action_shape(app, body):
    async def go():
        async with served(app) as (c, endpoint):
            assert (await post(c,body)).status == (415 if body is None else 400)
            assert not endpoint.ops.busy
    run(go())


def test_release_clean_stop_never_uses_force():
    from leeroy_mind.console.control import MindHandle
    from leeroy_mind.lifecycle import OwnershipError
    handle = object.__new__(MindHandle)
    handle.stopping = False
    handle._clean_stop = lambda: (None,"no verified endpoint")
    handle._run = lambda action: pytest.fail("a forced stop was attempted")
    with pytest.raises(OwnershipError,match="clean mind stop"):
        handle.clean_stop()
    assert handle.stopping is False
