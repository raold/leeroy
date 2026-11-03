import asyncio
from dataclasses import replace
from datetime import datetime, time, timedelta, timezone
import random

import pytest

from leeroy_mind.guild.config import GuildConfig
from leeroy_mind.guild.offline import OfflineRaids, is_due, next_raid_key
from leeroy_mind.guild.protocol import JobSkip, StartOfflineRaid
from leeroy_mind.guild.session import RaidSession
from leeroy_mind.protocol import Boss, Death, Login, Logout
from leeroy_mind.testing.fake_clock import FakeClock
from .fakes import job
from .test_raid_night import _run_async, advance, setup as raid_setup


@pytest.fixture
def setup(raid_setup):
    ports, armory, _ = raid_setup
    ports.clock = FakeClock(datetime(2026, 10, 29, 14, tzinfo=timezone.utc), yields=1)
    ports.online = False
    return ports, armory, OfflineRaids(ports, GuildConfig(), armory, random.Random(7))


def event(controller, *, state="progress", stage="running", reason="", instance=77, kills=0, wipes=0, **changes):
    saved = controller._owned
    fields = dict(id=saved["id"], guids=tuple(saved["accepted"] or saved["plan"]["members"]),
                  stage=stage, reason=reason, instance_id=instance, kills=kills, wipes=wipes)
    fields.update(changes)
    return job("offline_raid", state, **fields)


async def start(setup):
    ports, _, controller = setup
    await controller.tick()
    assert isinstance(ports.sent[-1], StartOfflineRaid)
    return controller


async def running(setup):
    controller = await start(setup)
    await controller.on_job(event(controller))
    await controller.tick()
    assert controller.session is not None and controller.session.ctx.instance_id == 77
    return controller


def due(now=datetime(2026, 10, 29, 14), **changes):
    values = dict(human_online=False, last_logout=datetime(2026, 10, 29, 11), raided_today=False,
                  window=(time(13), time(18)), min_away_min=60, raid_active=False)
    values.update(changes)
    return is_due(now, **values)


def test_is_due_boundaries_and_presence():
    assert due()
    assert not due(datetime(2026, 10, 29, 12, 59))
    assert due(datetime(2026, 10, 29, 13))
    assert not due(datetime(2026, 10, 29, 18))
    for flag in ("human_online", "raided_today", "raid_active"):
        assert not due(**{flag: True})
    assert not due(last_logout=datetime(2026, 10, 29, 13, 1))
    assert due(last_logout=datetime(2026, 10, 29, 13))
    assert due(last_logout=None)
    assert not due(last_logout=datetime(2026, 10, 29, 15))


def test_is_due_overnight_empty_window_and_dst_elapsed_absence():
    assert due(datetime(2026, 10, 30, 1), window=(time(22), time(2)))
    assert not due(datetime(2026, 10, 30, 2), window=(time(22), time(2)))
    assert not due(window=(time(13), time(13)))
    from zoneinfo import ZoneInfo
    tz = ZoneInfo("America/New_York")
    logout = datetime(2026, 11, 1, 1, 30, tzinfo=tz, fold=0)
    now = datetime(2026, 11, 1, 1, 30, tzinfo=tz, fold=1)
    assert is_due(now, human_online=False, last_logout=logout, raided_today=False,
                  window=(time(0), time(4)), min_away_min=60, raid_active=False)
    assert not due(last_logout=logout)  # mixed awareness fails closed


def test_next_raid_key_never_run_then_oldest_stable_ties():
    assert next_raid_key(("icc25", "icc10", "toc25"), {"icc25": 100, "toc25": 50}) == "icc10"
    assert next_raid_key(("icc25", "toc25"), {"icc25": 100, "toc25": 50}) == "toc25"
    assert next_raid_key(("TOC25", "icc25"), {}) == "toc25"
    assert next_raid_key(("toc25", "icc25"), {"icc25": 1, "toc25": 1}) == "toc25"
    for keys in ((), ("voa25",), ("icc20",)):
        with pytest.raises(ValueError):
            next_raid_key(keys, {})


@_run_async
async def test_start_has_expected_comp_lease_and_no_unproved_attendance(setup):
    ports, _, controller = setup
    await start(setup)
    command = ports.sent[-1]
    assert (command.map_id, command.size, command.leader_guid, command.loot_master_guid) == (631, 25, 101, 104)
    assert len(command.members) == len(set(command.members)) == 24
    assert 1 not in command.members and 101 not in command.members
    assert (command.max_wipes, command.max_wipes_per_boss, command.timeout_min, command.min_members) == (6, 3, 180, 20)
    assert ports.gpu == ["acquire:offline_raid"] and controller.session is None
    assert ports.conn.execute("SELECT COUNT(*) FROM guild_raid WHERE offline=1 AND ended IS NULL").fetchone()[0] == 0
    await controller.on_job(event(controller, stage="grouping", instance=0, guids=()))
    await controller.tick()
    assert controller.session is None
    await controller.on_job(event(controller, stage="teleporting", instance=0))
    await controller.tick()
    assert controller.session is not None and controller.session.ctx.instance_id is None


@pytest.mark.parametrize("reason", ["driver_pending", "busy"])
@_run_async
async def test_retryable_errors_have_five_attempts_with_lease_release(setup, reason):
    ports, _, controller = setup
    ports.acks["start_offline_raid"].extend([(False, reason)] * 7)
    for count in range(5):
        await controller.tick()
        assert len(ports.sent) == count + 1
        await advance(ports, 59)
        await controller.tick()
        assert len(ports.sent) == count + 1
        await advance(ports, 1)
    await controller.tick()
    assert len(ports.sent) == 5
    assert ports.gpu == [item for _ in range(5) for item in ("acquire:offline_raid", "release:offline_raid")]


@_run_async
async def test_retry_does_not_cross_window_end_and_receipt_survives_restart(setup):
    ports, armory, controller = setup
    await advance(ports, 3 * 3600 + 59 * 60 + 30)
    ports.acks["start_offline_raid"].append((False, "busy"))
    await controller.tick()
    await advance(ports, 60)
    recreated = OfflineRaids(ports, controller.cfg, armory, random.Random(2))
    await recreated.tick()
    assert len(ports.sent) == 1


@_run_async
async def test_nonretryable_ack_failure_ends_day_without_attendance(setup):
    ports, _, controller = setup
    ports.acks["start_offline_raid"].append((False, "leader_not_tank"))
    await controller.tick()
    await advance(ports, 3600)
    await controller.tick()
    assert len(ports.sent) == 1 and ports.gpu[-1] == "release:offline_raid"
    assert controller.session is None


@_run_async
async def test_actual_participants_exclude_skipped_and_away_members(setup):
    ports, _, controller = setup
    ports.away_set = {130}
    await start(setup)
    assert 130 not in ports.sent[-1].members
    chosen = controller._owned["plan"]["members"]
    skipped = next(g for g in chosen if g not in {101, 104})
    await controller.on_job(event(controller, guids=tuple(g for g in chosen if g != skipped),
                                  skipped=(JobSkip(skipped, "offline"),)))
    await controller.tick()
    assert skipped not in controller.session.plan.members
    assert ports.conn.execute("SELECT 1 FROM guild_attendance WHERE raid_id=? AND guid=?",
                              (controller.session.raid_id, skipped)).fetchone() is None


@pytest.mark.parametrize("bad", ["id", "kind", "state", "instance", "foreign", "missing_tank", "count"])
@_run_async
async def test_job_identity_state_instance_and_membership_are_mandatory(setup, bad):
    ports, _, controller = setup
    await start(setup)
    sample = event(controller)
    if bad == "id": sample = replace(sample, id="foreign")
    if bad == "kind": sample = replace(sample, kind="form_raid")
    if bad == "state": sample = replace(sample, state="running")
    if bad == "instance": sample = replace(sample, instance_id=0)
    if bad == "foreign": sample = replace(sample, guids=(*sample.guids, 999))
    if bad == "missing_tank": sample = replace(sample, guids=tuple(g for g in sample.guids if g != 101), skipped=(JobSkip(101, "offline"),))
    if bad == "count": sample = replace(sample, kills=1025)
    await controller.on_job(sample)
    await controller.tick()
    assert controller.session is None and controller.active
    assert ports.gpu == ["acquire:offline_raid"]


@_run_async
async def test_done_records_terminal_counts_memories_and_releases_once(setup):
    ports, _, controller = setup
    await running(setup)
    raid_id, members = controller.session.raid_id, controller.session.plan.members
    terminal = event(controller, state="done", stage="", reason="cleared", kills=12, wipes=2)
    await controller.on_job(terminal)
    await controller.tick()
    assert not controller.active and controller.session is None
    assert tuple(ports.conn.execute("SELECT kills,wipes,outcome FROM guild_raid WHERE raid_id=?", (raid_id,)).fetchone()) == (12, 2, "cleared")
    memories = [m for m in ports.memories if "without" in m.text]
    assert {m.owner for m in memories} == set(members)
    assert all("without Testy" in m.text and m.importance == 5 and m.valence == 0.4
               and m.scope == "open" and len(m.evidence) == 1 and m.evidence[0].startswith("event:") for m in memories)
    assert ports.conn.execute("SELECT COUNT(*) FROM dkp_ledger WHERE reason='kill'").fetchone()[0] == 0
    await controller.on_job(terminal)
    await controller.tick()
    assert len([m for m in ports.memories if "without" in m.text]) == len(members)
    assert ports.gpu == ["acquire:offline_raid", "release:offline_raid"]


@_run_async
async def test_progress_running_and_kill_before_ticker_preserve_order_and_dkp(setup):
    ports, _, controller = setup
    heard = []

    class Listener:
        async def on_start(self, run): heard.append(("start", run.human_in_raid))
        async def on_boss(self, ev): heard.append(("boss", ev.state))
        async def on_end(self, reason): heard.append(("end", reason))

    controller.add_listener(Listener())
    await start(setup)
    await controller.on_job(event(controller))
    await controller.on_boss(Boss(0, 631, 77, 1, "Testboss", "killed", (101, 104, 999)))
    await controller.on_job(event(controller, state="done", stage="", reason="cleared", kills=1))
    await controller.tick()
    assert heard == []  # Already terminal: retain accounting without starting a stale listener run.
    assert dict(ports.conn.execute("SELECT guid,delta FROM dkp_ledger WHERE reason='kill'")) == {101: 10, 104: 10}


@_run_async
async def test_login_latches_cancellation_and_retains_ownership_until_terminal(setup):
    ports, _, controller = setup
    await running(setup)
    raid_id = controller.session.raid_id
    await controller.on_login(Login(0, 1, True))
    ports.online = True
    await controller.tick()
    assert controller.active and controller.session.raid_id == raid_id
    assert ports.gpu == ["acquire:offline_raid"]
    await controller.on_logout(Logout(0, 1, True))
    ports.online = False
    await controller.on_job(event(controller, stage="running"))
    await advance(ports, 61)
    await controller.tick()
    assert len(ports.sent) == 1 and controller.active
    assert ports.gpu[-1] == "release:offline_raid"
    await controller.on_job(event(controller, state="failed", stage="", reason="cleanup_failed:human_login",
                                  skipped=(JobSkip(101, "cleanup:offline"),)))
    await controller.tick()
    assert not controller.active
    assert ports.conn.execute("SELECT outcome FROM guild_raid WHERE raid_id=?", (raid_id,)).fetchone()[0] == "cleanup_failed:human_login"


@_run_async
async def test_no_instance_binding_from_boss_or_wrong_job(setup):
    ports, _, controller = setup
    await start(setup)
    await controller.on_job(event(controller, stage="teleporting", instance=0))
    await controller.tick()
    await controller.on_boss(Boss(0, 631, 77, 1, "Testboss", "killed", (101,)))
    await controller.on_job(replace(event(controller), id="another-command"))
    await controller.tick()
    assert controller.session.ctx.instance_id is None and controller.session.kills == 0
    await controller.on_job(event(controller))
    await controller.tick()
    await controller.on_job(event(controller, instance=88, state="done", stage="", reason="cleared"))
    await controller.tick()
    assert controller.active and controller.session.ctx.instance_id == 77


@_run_async
async def test_daily_quota_and_next_day_rotation(setup):
    ports, _, controller = setup
    await running(setup)
    await controller.on_job(event(controller, state="done", stage="", reason="cleared"))
    await controller.tick()
    await advance(ports, 2 * 3600)
    await controller.tick()
    assert len(ports.sent) == 1
    await advance(ports, 22 * 3600)
    await controller.tick()
    assert len(ports.sent) == 2 and ports.sent[-1].size == 10


@_run_async
async def test_quota_zero_and_existing_online_raid_prevent_start(setup):
    ports, _, controller = setup
    controller.cfg = replace(controller.cfg, offline_raids_per_day=0)
    await controller.tick()
    assert not ports.sent and not ports.gpu
    controller.cfg = replace(controller.cfg, offline_raids_per_day=1)
    ports.conn.execute("INSERT INTO guild_raid(started,raid_key,size,heroic,offline,lead_guid,tank_guid,loot_guid) VALUES (?,?,?,?,?,?,?,?)",
                        (ports.clock.now(), "icc", 10, 0, 0, 101, 101, 104))
    await controller.tick()
    assert not ports.sent and not ports.gpu


@pytest.mark.parametrize("cancel", [False, True])
@_run_async
async def test_acquire_failure_or_cancellation_releases_only_attempted_holder(setup, cancel):
    ports, _, controller = setup
    entered = asyncio.Event()

    async def acquire(holder):
        ports.gpu.append("acquire:" + holder)
        entered.set()
        if cancel:
            await asyncio.Event().wait()
        raise RuntimeError("synthetic preload failure")

    ports.acquire_gpu = acquire
    work = asyncio.create_task(controller.tick())
    await entered.wait()
    if cancel:
        work.cancel()
        with pytest.raises(asyncio.CancelledError):
            await work
    else:
        await work
    assert ports.gpu == ["acquire:offline_raid", "release:offline_raid"]
    assert not ports.sent


@_run_async
async def test_denied_lease_does_not_release_somebody_elses_holder(setup):
    ports, _, controller = setup

    async def acquire(holder):
        return False

    ports.acquire_gpu = acquire
    await controller.tick()
    assert not ports.gpu and not ports.sent


@pytest.mark.parametrize("cancel", [False, True])
@_run_async
async def test_interrupted_start_ack_never_resends_or_reopens_after_login(setup, cancel):
    ports, _, controller = setup
    entered, release = asyncio.Event(), asyncio.Event()
    original = ports.send

    async def blocked(command):
        result = await original(command)
        entered.set()
        await release.wait()
        return result

    ports.send = blocked
    work = asyncio.create_task(controller.tick())
    await entered.wait()
    if cancel:
        work.cancel()
        with pytest.raises(asyncio.CancelledError):
            await work
    else:
        await controller.on_login(Login(0, 1, True))
        ports.online = True
        release.set()
        await work
    await controller.on_job(event(controller))
    await controller.tick()
    assert controller.session is None and not ports.leads
    await controller.on_job(event(controller, state="done", stage="", reason="human_login"))
    await controller.tick()
    assert not controller.active and ports.gpu[-1] == "release:offline_raid"
    assert len(ports.sent) == 1


@pytest.mark.parametrize("cancel", [False, True])
@_run_async
async def test_interrupted_session_open_does_not_repeat_attendance(setup, cancel):
    ports, _, controller = setup
    await start(setup)
    entered = asyncio.Event()

    async def remember(candidate):
        entered.set()
        if cancel:
            await asyncio.Event().wait()
        raise RuntimeError("synthetic writer failure")

    original = ports.remember
    ports.remember = remember
    await controller.on_job(event(controller))
    work = asyncio.create_task(controller.tick())
    await entered.wait()
    if cancel:
        work.cancel()
        with pytest.raises(asyncio.CancelledError):
            await work
    else:
        with pytest.raises(RuntimeError):
            await work
    assert controller._owned["cancelled"]
    ports.remember = original
    before = ports.conn.execute("SELECT COUNT(*) FROM dkp_ledger WHERE reason='attendance'").fetchone()[0]
    await controller.tick()
    assert not controller._owned["started"]
    assert ports.conn.execute("SELECT COUNT(*) FROM dkp_ledger WHERE reason='attendance'").fetchone()[0] == before
    assert len(ports.sent) == 1
    await controller.on_job(event(controller, state="done", stage="", reason="human_login" if cancel else "cleared"))
    await controller.tick()
    assert not controller.active


@_run_async
async def test_restart_reuses_exact_owned_session_and_terminal_receipts(setup):
    ports, armory, controller = setup
    await running(setup)
    raid_id = controller.session.raid_id
    # Simulate process exit's lease cleanup before constructing its successor.
    await controller._release()
    recreated = OfflineRaids(ports, controller.cfg, armory, random.Random(2))
    await recreated.on_job(event(recreated))
    await recreated.tick()
    assert recreated.session.raid_id == raid_id and len(ports.sent) == 1
    assert ports.conn.execute("SELECT COUNT(*) FROM guild_raid WHERE offline=1 AND ended IS NULL").fetchone()[0] == 1
    await recreated.on_job(event(recreated, state="done", stage="", reason="cleared", kills=1))
    await recreated.tick()
    assert ports.gpu == ["acquire:offline_raid", "release:offline_raid"]
