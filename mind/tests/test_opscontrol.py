"""Synthetic state only: no live service, model, GPU, console or credential reads."""
import asyncio
import json
import sqlite3
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import run
from leeroy_mind.endpoint import BadRequest, ControlCfg, ControlEndpoint, load_control_config
from leeroy_mind.opscontrol import arguments, take_snapshot
from leeroy_mind.console.cli import settings_from
from selfies.fakes import mind_app
from console.test_console_ops import add_raid
from leeroy_mind.realm.action_store import ActionStore
from leeroy_mind.realm.protocol import RealmMove
from realm.test_realm_action_store import prepare, dispatch

ROOT_ID = "a"*32


def add_root(conn, root_id=ROOT_ID):
    journal = ActionStore(conn)
    dispatch(journal, prepare(journal, RealmMove(root_id, 101, "home"), now=1000000), now=1000001)
    journal.recover(process_id="new_process", now=1000002)
    return journal


@pytest.fixture
def app(tmp_path):
    a = mind_app(tmp_path)
    a.bridge = SimpleNamespace(connected=True)
    a.hello = SimpleNamespace(server="synthetic",version="test",ts=1)
    yield a
    a.store.close()


def ep(app, enabled=True):
    return ControlEndpoint(app, ControlCfg(ops_actions=enabled))


@pytest.mark.parametrize("body", [None, [], {}, {"action": []}, {"action":"erase"},
    {"action":"snapshot","label":"../escape"}, {"action":"snapshot","label":"x/y"},
    {"action":"snapshot","label":"x:y"}, {"action":"snapshot","label":"x"*41},
    {"action":"snapshot","label":True}, {"action":"offline_stop","id":"x"},
    {"action":"render","extra":"PRIVATE"}, {"action":"status","id":ROOT_ID}])
def test_strict_ops_arguments(body):
    with pytest.raises(BadRequest):
        arguments(body)


@pytest.mark.parametrize("content", ["", "[control]\nops_actions=false", "[control]\nops_actions='yes'",
    "control=4", "[control]\nops_actions=true\nport='bad'", "[control]\nenabled=false\nops_actions=true"])
def test_missing_disabled_invalid_control_is_off(tmp_path, content):
    (tmp_path/"mind.toml").write_text(content)
    assert not settings_from(tmp_path,tmp_path,1234,"k").ops_actions


def test_valid_config_and_endpoint_default(tmp_path):
    (tmp_path/"mind.toml").write_text("[control]\nops_actions=true")
    assert load_control_config(tmp_path/"mind.toml").ops_actions is True
    assert settings_from(tmp_path,tmp_path,1234,"k").ops_actions is True
    assert ControlCfg().ops_actions is False


@pytest.mark.parametrize("action,args", [("release",{"id":ROOT_ID}), ("offline_stop",{"id":ROOT_ID}),
    ("render",{}), ("snapshot",{"label":"review"})])
@pytest.mark.parametrize("condition", ["off", "online", "bridge", "stopping", "raid", "other_action"])
def test_all_mind_action_preflight_gates(app, action, args, condition):
    endpoint = ep(app, enabled=condition!="off")
    if condition == "online": app.world.human_online = True
    if condition == "bridge": app.bridge.connected = False
    if condition == "stopping": app._stopping = True
    if condition == "raid":
        add_raid(app.store.conn, 1, 0)
        app.store.conn.commit()
    async def go():
        if condition == "other_action": await endpoint._raid.acquire()
        result = await endpoint.ops.answer({"action":"inspect","kind":action,**args})
        assert not result["ok"] and not endpoint.ops.busy
    run(go())


def test_snapshot_committed_database_and_one_action(app, caplog):
    async def go():
        endpoint = ep(app)
        with caplog.at_level("INFO"):
            started = await endpoint.ops.answer({"action":"snapshot","label":"PRIVATE_LABEL"})
            assert started["ok"] and started["operation"]["state"] == "running"
            refused = await endpoint.ops.answer({"action":"snapshot","label":"second"})
            assert not refused["ok"]
            await endpoint.ops.task
        assert endpoint.ops.operation["state"] == "done"
        saved = list((app.data_dir/"snapshots").glob("*.db"))
        assert len(saved) == 1
        with sqlite3.connect(saved[0]) as copy:
            assert copy.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert copy.execute("SELECT count(*) FROM persona_core").fetchone()[0] > 0
        assert "PRIVATE_LABEL" not in caplog.text
        assert sum("ops_action" in r.message for r in caplog.records) == 1
    run(go())


def test_snapshot_refuses_existing_staging_directory(app):
    staging = app.data_dir/"snapshots"/(".ops-"+ROOT_ID)
    staging.mkdir(parents=True)
    marker = staging/"do-not-touch"
    marker.write_text("kept")
    with pytest.raises(FileExistsError): take_snapshot(app.data_dir,"test",ROOT_ID)
    assert marker.read_text() == "kept"


def test_snapshot_refuses_linked_destination(app, tmp_path):
    import os
    target = tmp_path/"outside"
    target.mkdir()
    link = app.data_dir/"snapshots"
    if os.name == "nt":
        import subprocess
        subprocess.run(["cmd","/c","mklink","/J",str(link),str(target)],check=True,capture_output=True)
    else:
        link.symlink_to(target,target_is_directory=True)
    try:
        with pytest.raises(ValueError, match="links"): take_snapshot(app.data_dir,"test",ROOT_ID)
        assert not list(target.iterdir())
    finally:
        link.rmdir() if os.name == "nt" else link.unlink()


def test_release_inspection_uses_cli_refusals_and_projects_no_evidence_text(app):
    add_root(app.store.conn)
    app.store.conn.commit()
    async def go():
        e = ep(app)
        result = await e.ops.answer({"action":"inspect","kind":"release","id":ROOT_ID})
        assert result["ok"] and result["details"]["members"] == 1, result
        assert "PRIVATE" not in json.dumps(result)
        direct = await e.ops.answer({"action":"release","id":ROOT_ID})
        assert not direct["ok"] and "clean stop" in direct["error"]
    run(go())


@pytest.mark.parametrize("case", ["conflict", "completed", "inflight", "missing", "child"])
def test_release_inspection_refuses_every_nonreleasable_root(app, case):
    add_root(app.store.conn)
    if case in {"conflict","completed"}:
        app.store.conn.execute("UPDATE realm_action SET state=? WHERE command_id=?",(case,ROOT_ID))
    if case == "inflight":
        app.store.conn.execute("UPDATE realm_action SET transport_state='inflight' WHERE command_id=?",(ROOT_ID,))
    if case == "child":
        add_root(app.store.conn,"b"*32)
        app.store.conn.execute("UPDATE realm_action SET role='cleanup',parent_id=? WHERE command_id=?",("b"*32,ROOT_ID))
    app.store.conn.commit()
    async def go():
        reply = await ep(app).ops.answer({"action":"inspect","kind":"release","id":"c"*32 if case=="missing" else ROOT_ID})
        assert not reply["ok"]
    run(go())
