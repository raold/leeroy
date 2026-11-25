import json
from dataclasses import replace

import pytest

from leeroy_mind.protocol import encode_command
from .test_offline import setup, start, event
from .test_offline_watchdog import heartbeat, stops
from .test_raid_night import _run_async, advance, setup as raid_setup


@pytest.mark.parametrize("supported", [False, True])
@_run_async
async def test_negotiated_native_stop_keeps_the_configured_limit_and_old_fallback(setup, supported):
    ports, _, controller = setup
    controller.cfg = replace(controller.cfg, offline_stall_minutes=17)
    if supported:
        controller._features |= {"offline_raid_native_stall"}
    await start(setup)
    wire = json.loads(encode_command(ports.sent[-1], 17))
    if supported:
        assert wire["stall_timeout_min"] == 17
    else:
        assert "stall_timeout_min" not in wire
    assert controller._owned["native_stall"] is supported
    await heartbeat(setup)
    await advance(ports, 17 * 60)
    await heartbeat(setup, age=17 * 60)
    assert len(stops(ports)) == (0 if supported else 1)


@_run_async
async def test_native_stall_terminal_closes_the_raid_without_a_mind_stop(setup):
    ports, _, controller = setup
    controller._features |= {"offline_raid_native_stall"}
    await start(setup)
    await heartbeat(setup)
    raid = controller.session.raid_id
    terminal = event(controller, state="done", stage="", reason="stalled", kills=1)
    await controller.on_job(terminal)
    await controller.tick()
    assert not controller.active and not stops(ports)
    assert ports.conn.execute("SELECT outcome FROM guild_raid WHERE raid_id=?", (raid,)).fetchone()[0] == "stalled"
    assert ports.gpu.count("release:offline_raid") == 1


@_run_async
async def test_operator_can_still_stop_a_run_with_a_native_watchdog(setup):
    ports, _, controller = setup
    controller._features |= {"offline_raid_native_stall"}
    await start(setup)
    await heartbeat(setup)
    await controller.request_stop(controller._owned["id"])
    assert len(stops(ports)) == 1
    assert controller._owned["stop"]["reason"] == "operator_stopped"
