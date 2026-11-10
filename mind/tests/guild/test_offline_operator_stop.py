import pytest

from .test_offline import setup, start, event
from .test_offline_watchdog import heartbeat, stops
from .test_raid_night import _run_async, advance, setup as raid_setup


@_run_async
async def test_operator_stop_without_stall_waits_for_terminal_once(setup):
    ports, _, c = setup
    await start(setup)
    await heartbeat(setup)
    ident = c._owned["id"]
    raid = c.session.raid_id
    await c.request_stop(ident)
    await c.request_stop(ident)
    assert len(stops(ports)) == 1 and stops(ports)[0].raid_id == ident
    assert c.active and c._owned["stop"]["reason"] == "operator_stopped"
    await c.on_job(event(c,state="done",stage="",reason="stopped"))
    await c.tick()
    assert not c.active
    assert c.operator_result(ident) == {"id":ident,"stopped":True}
    assert ports.conn.execute("SELECT outcome FROM guild_raid WHERE raid_id=?",(raid,)).fetchone()[0] == "operator_stopped"
    with pytest.raises(ValueError,match="no longer"): await c.request_stop(ident)
    # A later automatic run cannot replace the operator action's terminal receipt.
    c._owned = {"id":"f"*32,"phase":"running"}
    c._save()
    assert c.operator_result(ident)["stopped"] is True


@pytest.mark.parametrize("case", ["unknown", "stale", "engaged", "online", "unsupported"])
@_run_async
async def test_operator_stop_preserves_fences(setup, case):
    ports, _, c = setup
    await start(setup)
    await heartbeat(setup,engaged=case=="engaged")
    if case == "stale": await advance(ports,121)
    if case == "online": ports.online = True
    if case == "unsupported": c._owned["stop_unsupported"] = True
    with pytest.raises(ValueError): await c.request_stop("f"*32 if case=="unknown" else c._owned["id"])
    assert not stops(ports)


@_run_async
async def test_operator_retry_uses_same_target_new_envelope_and_fresh_fences(setup):
    ports, _, c = setup
    await start(setup)
    await heartbeat(setup)
    ports.acks["offline_raid_stop"].append((False,"boss_engaged"))
    await c.request_stop(c._owned["id"])
    assert len(stops(ports)) == 1 and not c._owned["stop"]["accepted"]
    await advance(ports,60)
    await heartbeat(setup,engaged=True)
    assert len(stops(ports)) == 1
    await advance(ports,60)
    await heartbeat(setup)
    assert len(stops(ports)) == 2
    assert stops(ports)[0].id != stops(ports)[1].id
    assert stops(ports)[0].raid_id == stops(ports)[1].raid_id == c._owned["id"]


@_run_async
async def test_operator_intent_keeps_uncertain_stall_stop_after_progress_resumes(setup):
    from leeroy_mind.bridge import AckTimeout
    ports, _, c = setup
    await start(setup)
    await heartbeat(setup)
    original = ports.send

    async def timeout(cmd):
        await original(cmd)
        raise AckTimeout("synthetic timeout")

    ports.send = timeout
    await advance(ports, 1800)
    await heartbeat(setup, age=1800)
    ident, raid = c._owned["id"], c.session.raid_id
    assert c._owned["stop"]["reason"] == "stalled"
    await c.request_stop(ident)
    assert len(stops(ports)) == 2 and not c._owned["stop"]["accepted"]
    assert c._owned["operator_stop"] is True

    ports.send = original
    await advance(ports, 60)
    await heartbeat(setup, engaged=True)  # Operator intent cannot bypass combat.
    assert len(stops(ports)) == 2
    await advance(ports, 60)
    await heartbeat(setup)  # Fresh progress cancels only an automatic stall retry.
    assert len(stops(ports)) == 3 and c._owned["stop"]["accepted"]
    assert len({s.id for s in stops(ports)}) == 3
    assert {s.raid_id for s in stops(ports)} == {ident}
    assert c._owned["stop"]["reason"] == "stalled"
    await c.on_job(event(c, state="done", stage="", reason="stopped"))
    await c.tick()
    assert not c.active and c.operator_result(ident) == {"id": ident, "stopped": True}
    assert ports.conn.execute("SELECT outcome FROM guild_raid WHERE raid_id=?", (raid,)).fetchone()[0] == "stalled"


@_run_async
async def test_control_stop_is_pending_until_native_terminal_and_cleanup(setup,tmp_path):
    import asyncio
    from types import SimpleNamespace
    from leeroy_mind.endpoint import ControlEndpoint,ControlCfg,encode
    ports, _, c = setup
    await start(setup)
    await heartbeat(setup)
    app = SimpleNamespace(data_dir=tmp_path,stopping=False,store=SimpleNamespace(conn=ports.conn),
        guild=SimpleNamespace(offline=c),clock=ports.clock,bridge=SimpleNamespace(connected=True),
        hello=SimpleNamespace(server="fake",version="test",ts=1),
        world=SimpleNamespace(human_online=False,online=[],cast_guids=lambda:[]))
    e = ControlEndpoint(app,ControlCfg(ops_actions=True),key="K"*43)
    reply,_ = await e._answer(encode({"v":1,"key":e.key,"op":"ops","args":{"action":"offline_stop","id":c._owned["id"]}}))
    assert reply["operation"]["state"] == "running"
    await asyncio.sleep(.01)
    assert e.ops.busy and c.active and len(stops(ports)) == 1
    await c.on_job(event(c,state="done",stage="",reason="stopped"))
    await c.tick()
    await asyncio.wait_for(e.ops.task,2)
    assert e.ops.operation["state"] == "done"
    assert "cleanup finished" in e.ops.operation["message"]
