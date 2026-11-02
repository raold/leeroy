"""Independent recovery and real game-runner boundary regressions."""

import random
from dataclasses import replace

import pytest

from leeroy_mind.guild.offline import OfflineRaids
from leeroy_mind.protocol import Boss, Login, Logout, LootDrop
from .test_offline import setup, start, running, event
from .test_raid_night import _run_async, advance, setup as raid_setup


@_run_async
async def test_failed_grouping_guids_do_not_prove_attendance(setup):
    ports, _, controller = setup
    await start(setup)
    await controller.on_job(event(controller, stage="tearing_down", instance=0))
    await controller.on_job(event(controller, state="failed", stage="", instance=0,
                                  reason="setup:grouping:add_failed"))
    await controller.tick()
    assert not controller.active and controller.session is None
    assert ports.conn.execute("SELECT COUNT(*) FROM dkp_ledger WHERE reason='attendance'").fetchone()[0] == 0
    assert ports.gpu == ["acquire:offline_raid", "release:offline_raid"]


@_run_async
async def test_completed_receipt_does_not_restore_closed_session_next_day(setup):
    ports, armory, controller = setup
    await running(setup)
    old_id = controller.session.raid_id
    await controller.on_job(event(controller, state="done", stage="", reason="cleared"))
    await controller.tick()
    recreated = OfflineRaids(ports, controller.cfg, armory, random.Random(7))
    assert recreated.session is None and recreated.run is None
    await advance(ports, 86400)
    await recreated.tick()
    await recreated.on_job(event(recreated))
    await recreated.tick()
    assert recreated.session.raid_id != old_id
    assert len(ports.sent) == 2


@_run_async
async def test_recovery_monitors_owned_remote_job_without_resuming_local_actions(setup):
    ports, armory, controller = setup
    await running(setup)
    raid_id = controller.session.raid_id
    await controller._release()
    recreated = OfflineRaids(ports, controller.cfg, armory, random.Random(7))
    assert recreated._owned["cancelled"]

    async def unexpected(*args):
        pytest.fail("recovery without a model lease resumed local work")

    recreated.session.tick = unexpected
    recreated.session.on_loot_drop = unexpected
    await recreated.on_loot_drop(LootDrop(0, 631, 77, 1000, ()))
    await recreated.tick()
    assert recreated.active and len(ports.sent) == 1
    await recreated.on_job(event(recreated, state="done", stage="", reason="cleared", kills=13))
    await recreated.tick()
    assert not recreated.active
    assert ports.conn.execute("SELECT kills FROM guild_raid WHERE raid_id=?", (raid_id,)).fetchone()[0] == 13


@_run_async
async def test_login_preserves_queued_kill_accounting_but_drops_loot_and_cast_listeners(setup):
    ports, _, controller = setup
    await running(setup)
    heard = []

    class Listener:
        async def on_boss(self, ev):
            heard.append(ev)

    controller.add_listener(Listener())

    async def unexpected(*args):
        pytest.fail("cancelled run must not transfer queued loot")

    controller.session.on_loot_drop = unexpected
    before_said, before_proposed = list(ports.said), list(ports.proposed)
    await controller.on_boss(Boss(0, 631, 77, 1, "Testboss", "killed", (101, 104)))
    await controller.on_loot_drop(LootDrop(0, 631, 77, 1000, ()))
    await controller.on_login(Login(0, 1, True))
    ports.online = True
    await controller.tick()
    assert not heard and controller.session.kills == 1
    assert ports.said == before_said and ports.proposed == before_proposed
    assert dict(ports.conn.execute("SELECT guid,delta FROM dkp_ledger WHERE reason='kill'")) == {101: 10, 104: 10}


@_run_async
async def test_known_logout_wait_survives_restart(setup):
    ports, armory, controller = setup
    await controller.on_logout(Logout(0, 1, True))
    await advance(ports, 120)
    recreated = OfflineRaids(ports, controller.cfg, armory, random.Random(7))
    await recreated.tick()
    assert not ports.sent
    await advance(ports, 3480)
    await recreated.tick()
    assert len(ports.sent) == 1


@_run_async
async def test_local_deadline_stops_actions_before_remote_cleanup_grace_expires(setup):
    ports, _, controller = setup
    await running(setup)
    heard = []

    class Listener:
        async def on_boss(self, ev):
            heard.append(ev)

    controller.add_listener(Listener())
    await controller.on_boss(Boss(0, 631, 77, 1, "Testboss", "engaged", (101, 104)))
    await advance(ports, controller.cfg.offline_timeout_min * 60)
    await controller.tick()
    assert not heard and controller._owned["cancelled"] and controller.active
    assert ports.gpu == ["acquire:offline_raid"]
    await advance(ports, 60)
    await controller.tick()
    assert ports.gpu[-1] == "release:offline_raid" and controller.active


@_run_async
async def test_crash_before_dispatch_can_retry_acquisition_without_remote_ambiguity(setup):
    ports, armory, controller = setup
    await start(setup)
    # Simulate the durable pre-dispatch checkpoint; no command escaped in that process.
    controller._owned["phase"] = "acquiring"
    controller._save()
    ports.sent.clear()
    await controller._release()
    recreated = OfflineRaids(ports, controller.cfg, armory, random.Random(7))
    assert not recreated.active
    # Its simulated prior accepted quota must not influence the pre-dispatch case.
    recreated._update_day(accepted=0, next_try=0)
    await recreated.tick()
    assert len(ports.sent) == 1


@_run_async
async def test_acquisition_crossing_midnight_charges_dispatch_day_quota(setup):
    ports, _, controller = setup
    controller.cfg = replace(controller.cfg, offline_window=("22:00", "02:00"))
    await advance(ports, 10 * 3600 - 30)
    before_day = ports.clock.local().date().isoformat()
    original = ports.acquire_gpu

    async def acquire(holder):
        await original(holder)
        await advance(ports, 60)

    ports.acquire_gpu = acquire
    await start(setup)
    dispatch_day = ports.clock.local().date().isoformat()
    assert controller._owned["day"] == dispatch_day != before_day
    assert controller._day_state(dispatch_day)["accepted"] == 1
    assert controller._day_state(before_day).get("accepted", 0) == 0


@_run_async
async def test_failed_local_release_does_not_invent_remote_job_after_rejected_ack(setup):
    ports, _, controller = setup
    ports.acks["start_offline_raid"].append((False, "busy"))
    original = ports.release_gpu
    releases = 0

    async def release(holder):
        nonlocal releases
        releases += 1
        if releases == 1:
            raise RuntimeError("synthetic local release failure")
        await original(holder)

    ports.release_gpu = release
    await controller.tick()
    assert not controller.active and controller._owned["phase"] == "rejected"
    assert not controller._lease and releases == 2
    await advance(ports, 86400)
    await controller.tick()
    assert len(ports.sent) == 2
