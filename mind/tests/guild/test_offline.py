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


@_run_async
async def test_driver_pending_retries_each_minute_up_to_the_attempt_bound_with_lease_release(setup):
    from leeroy_mind.guild.offline import MAX_ATTEMPTS
    ports, _, controller = setup
    ports.acks["start_offline_raid"].extend([(False, "driver_pending")] * (MAX_ATTEMPTS + 2))
    for count in range(MAX_ATTEMPTS):
        await controller.tick()
        assert len(ports.sent) == count + 1
        await advance(ports, 59)
        await controller.tick()
        assert len(ports.sent) == count + 1
        await advance(ports, 1)
    await controller.tick()
    assert len(ports.sent) == MAX_ATTEMPTS
    assert ports.gpu == [item for _ in range(MAX_ATTEMPTS) for item in ("acquire:offline_raid", "release:offline_raid")]


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
    day = controller._day_state()
    assert day["blocked"] and day["refused"] == "leader_not_tank"


@_run_async
async def test_setup_failure_reason_stays_in_the_day_receipt(setup):
    ports, armory, controller = setup
    await start(setup)
    await controller.on_job(event(controller, stage="starting"))
    await controller.on_job(event(controller, state="failed", stage="",
                                  reason="setup:starting:no_fresh_encounters" + "x" * 300))
    await controller.tick()
    day = OfflineRaids(ports, controller.cfg, armory, random.Random(7))._day_state()
    assert day["accepted"] == 1 and day["failed"].startswith("setup:starting:no_fresh_encounters")
    assert len(day["failed"]) == 200 and "refused" not in day


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


class Heard:
    def __init__(self):
        self.events = []

    async def on_start(self, run):
        self.events.append(("start", run.plan.raid_key, run.plan.size))

    async def on_end(self, reason, run=None):
        self.events.append(("end", reason))


def raids_today(ports):
    midnight = ports.clock.local().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    return ports.conn.execute("SELECT COUNT(*) FROM guild_raid WHERE offline=1 AND started>=?", (midnight,)).fetchone()[0]


def test_next_raid_key_skips_raids_proved_unusable_today():
    assert next_raid_key(("icc25", "icc10", "toc25"), {}, skip={"icc25"}) == "icc10"
    assert next_raid_key(("icc25", "toc25"), {"toc25": 50}, skip={"icc25"}) == "toc25"
    with pytest.raises(ValueError):
        next_raid_key(("icc25",), {}, skip={"icc25"})


@pytest.mark.parametrize("reason", ["leader_unavailable:in_combat", "leader_unavailable:dead",
                                    "busy", "busy:realm_owner:113", "in_instance:113",
                                    "training_commands_pending"])
@_run_async
async def test_busy_refusal_is_retried_later_that_day_and_the_raid_starts(setup, reason):
    ports, _, controller = setup
    heard = Heard()
    controller.add_listener(heard)
    ports.acks["start_offline_raid"].append((False, reason))
    today = ports.clock.local().date()
    await controller.tick()
    assert len(ports.sent) == 1
    day = controller._day_state()
    assert not day["blocked"] and day["refused"] == reason
    await advance(ports, 14 * 60)
    await controller.tick()
    assert len(ports.sent) == 1  # Not a 60 s hammer: the body is busy for a while.
    await advance(ports, 60)
    await controller.tick()
    assert len(ports.sent) == 2 and ports.sent[1].id != ports.sent[0].id
    assert ports.clock.local().date() == today
    await controller.on_job(event(controller))
    await controller.tick()
    assert heard.events == [("start", "icc", 25)]
    assert controller.session.ctx.instance_id == 77
    assert ports.gpu == ["acquire:offline_raid", "release:offline_raid", "acquire:offline_raid"]


@_run_async
async def test_busy_refusals_are_bounded_and_then_end_the_day(setup):
    from leeroy_mind.guild.offline import MAX_ATTEMPTS
    ports, _, controller = setup
    ports.acks["start_offline_raid"].extend([(False, "leader_unavailable:in_combat")] * (MAX_ATTEMPTS + 2))
    for _ in range(MAX_ATTEMPTS + 2):
        await controller.tick()
        await advance(ports, 15 * 60)
    assert len(ports.sent) == MAX_ATTEMPTS > 5
    assert controller._day_state()["blocked"]
    await advance(ports, 3600)
    await controller.tick()
    assert len(ports.sent) == MAX_ATTEMPTS


@_run_async
async def test_raid_without_fresh_encounters_falls_through_to_one_that_has_them(setup):
    ports, _, controller = setup
    heard = Heard()
    controller.add_listener(heard)
    await start(setup)
    assert (ports.sent[-1].map_id, ports.sent[-1].size) == (631, 25)
    await controller.on_job(event(controller, stage="starting"))
    await controller.on_job(event(controller, state="failed", stage="", reason="setup:starting:no_fresh_encounters"))
    await controller.tick()
    assert not controller.active and not controller._day_state()["blocked"]
    assert not [m for m in ports.memories if "without" in m.text]  # Nobody raided: no raid memory.
    await advance(ports, 60)
    await controller.tick()
    assert len(ports.sent) == 2 and (ports.sent[-1].map_id, ports.sent[-1].size) == (631, 10)
    await controller.on_job(event(controller))
    await controller.tick()
    assert heard.events == [("start", "icc", 10)]
    await controller.on_job(event(controller, state="done", stage="", reason="cleared", kills=3))
    await controller.tick()
    assert ("end", "cleared") in heard.events
    await advance(ports, 2 * 3600)
    await controller.tick()
    assert len(ports.sent) == 2  # The started raid is the day's one raid.


@_run_async
async def test_fall_through_survives_a_restart(setup):
    ports, armory, controller = setup
    await start(setup)
    await controller.on_job(event(controller, stage="starting"))
    await controller.on_job(event(controller, state="failed", stage="", reason="setup:starting:no_fresh_encounters"))
    await controller.tick()
    recreated = OfflineRaids(ports, controller.cfg, armory, random.Random(3))
    await advance(ports, 60)
    await recreated.tick()
    assert len(ports.sent) == 2 and ports.sent[-1].size == 10


@_run_async
async def test_rotation_with_no_fresh_raid_left_waits_for_tomorrow(setup):
    ports, _, controller = setup
    controller.cfg = replace(controller.cfg, offline_rotation=("icc25",))
    await start(setup)
    await controller.on_job(event(controller, stage="starting"))
    await controller.on_job(event(controller, state="failed", stage="", reason="setup:starting:no_fresh_encounters"))
    await controller.tick()
    await advance(ports, 3600)
    await controller.tick()
    assert len(ports.sent) == 1 and controller._day_state()["blocked"]
    await advance(ports, 23 * 3600)
    await controller.tick()
    assert len(ports.sent) == 2 and ports.sent[-1].size == 25


@pytest.mark.parametrize("reason", ["setup:restarting:timeout", "encounters_changed",
                                    "setup:starting:no_fresh_encounters", "setup:teleporting:in_instance:113"])
@_run_async
async def test_a_raid_that_started_is_still_the_days_only_raid(setup, reason):
    ports, _, controller = setup
    await running(setup)
    await controller.on_job(event(controller, state="failed", stage="", reason=reason))
    await controller.tick()
    for _ in range(4):
        await advance(ports, 15 * 60)
        await controller.tick()
    assert len(ports.sent) == 1 and raids_today(ports) == 1


def attendance(ports, raid_id):
    rows = ports.conn.execute("SELECT COUNT(*) FROM guild_attendance WHERE raid_id=?", (raid_id,)).fetchone()[0]
    dkp = ports.conn.execute("SELECT COUNT(*) FROM dkp_ledger WHERE raid_id=? AND reason IN ('attendance','standby')",
                             (raid_id,)).fetchone()[0]
    return rows, dkp


def closing_lines(ports):
    return ([text for _, _, text in ports.said if text.startswith("dkp top 5:")],
            [line for _, _, line, _ in ports.proposed if line.startswith("Wrap up")])


def not_raided(ports, raid_id):
    from leeroy_mind.guild.loot import _get
    return _get(ports.conn, f"guild:raid:{raid_id}:not-raided") is True


@_run_async
async def test_member_saved_to_the_owners_copy_of_the_planned_map_is_never_chosen(setup):
    ports, armory, controller = setup
    armory.binds = {1: {(631, 14)}, 103: {(631, 14)}, 120: {(631, 14)}, 102: {(631, 14)}}
    await start(setup)
    command = ports.sent[-1]
    chosen = {command.leader_guid, command.loot_master_guid, *command.members}
    assert not chosen & {102, 103, 120}
    assert not set(controller._owned["plan"]["bench"]) & {102, 103, 120}
    call = next(c for c in armory.calls if c[0] == "bound_with")
    assert call[1:3] == (1, 631) and set(call[3]) == set(range(101, 131))


@_run_async
async def test_member_saved_to_a_copy_the_owner_is_not_in_may_still_be_chosen(setup):
    ports, armory, controller = setup
    # 120 holds another ICC copy (native unbinds it as before); 108 shares the owner's ToC copy only.
    armory.binds = {1: {(631, 14), (649, 30)}, 120: {(631, 15)}, 108: {(649, 30)}}
    await start(setup)
    command = ports.sent[-1]
    assert {120, 108} <= {command.leader_guid, *command.members}


@_run_async
async def test_unreadable_binds_hold_the_start_and_retry_later(setup):
    from leeroy_mind.personas.roster import RosterError
    ports, armory, controller = setup

    def unreadable(*args):
        raise RosterError("guild armory unavailable")

    armory.bound_with = unreadable
    await controller.tick()
    assert not ports.sent and not ports.gpu and not controller._day_state()["blocked"]
    del armory.bound_with
    await advance(ports, 14 * 60)
    await controller.tick()
    assert not ports.sent
    await advance(ports, 60)
    await controller.tick()
    assert len(ports.sent) == 1


@_run_async
async def test_skipped_run_awards_no_attendance_and_the_days_real_raid_awards_it_once(setup):
    ports, _, controller = setup
    await start(setup)
    await controller.on_job(event(controller, stage="starting"))
    await controller.tick()
    skipped = controller.session.raid_id
    assert attendance(ports, skipped) == (0, 0)
    await controller.on_job(event(controller, state="failed", stage="", reason="setup:starting:no_fresh_encounters"))
    await controller.tick()
    assert attendance(ports, skipped) == (0, 0) and closing_lines(ports) == ([], [])
    row = ports.conn.execute("SELECT ended, outcome FROM guild_raid WHERE raid_id=?", (skipped,)).fetchone()
    assert row[0] is not None and row[1] == "setup:starting:no_fresh_encounters"
    assert not_raided(ports, skipped)
    await advance(ports, 60)
    await controller.tick()
    await controller.on_job(event(controller, stage="teleporting"))
    await controller.tick()
    raided = controller.session.raid_id
    assert raided != skipped and attendance(ports, raided) == (0, 0)
    await controller.on_job(event(controller))
    await controller.tick()
    plan = controller.session.plan
    expected = (len(plan.members) + len(plan.bench),) * 2
    assert attendance(ports, raided) == expected
    await controller.on_job(event(controller, state="done", stage="", reason="cleared", kills=2))
    await controller.tick()
    assert attendance(ports, raided) == expected and attendance(ports, skipped) == (0, 0)
    standings, wrapups = closing_lines(ports)
    assert len(standings) == len(wrapups) == 1
    assert not not_raided(ports, raided)


@pytest.mark.parametrize("terminal", [("failed", "setup:teleporting:timeout"),
                                      ("failed", "setup:starting:group_changed"),
                                      ("done", "human_login")])
@_run_async
async def test_any_run_that_never_reached_running_is_not_raided(setup, terminal):
    ports, _, controller = setup
    await start(setup)
    await controller.on_job(event(controller, stage="teleporting"))
    await controller.tick()
    raid_id = controller.session.raid_id
    state, reason = terminal
    await controller.on_job(event(controller, state=state, stage="", reason=reason))
    await controller.tick()
    assert not controller.active
    assert attendance(ports, raid_id) == (0, 0) and closing_lines(ports) == ([], [])
    assert not [m for m in ports.memories if "without" in m.text or "benched" in m.text]
    assert not_raided(ports, raid_id)


@_run_async
async def test_a_run_that_reached_running_keeps_its_attendance_when_it_fails(setup):
    ports, _, controller = setup
    await running(setup)
    raid_id = controller.session.raid_id
    plan = controller.session.plan
    await controller.on_job(event(controller, state="failed", stage="", reason="encounters_changed"))
    await controller.tick()
    assert attendance(ports, raid_id)[0] == len(plan.members) + len(plan.bench)
    assert not not_raided(ports, raid_id)


OLD_DECAY = "2026-10-01T04:00:00"


def decay_receipt(ports):
    row = ports.conn.execute("SELECT value FROM meta WHERE key='dkp_last_decay'").fetchone()
    return row[0] if row else None


def decay_lines(ports):
    return [text for _, _, text in ports.said if text.startswith("dkp updated")]


@_run_async
async def test_owner_copy_fence_that_leaves_too_few_skips_to_the_next_raid_that_day(setup):
    ports, armory, controller = setup
    controller.cfg = replace(controller.cfg, offline_rotation=("icc25", "toc25"))
    armory.binds = {guid: {(631, 14)} for guid in (1, *range(101, 123))}  # his copy holds 22 of 30
    await controller.tick()
    assert not ports.sent and not ports.gpu
    day = controller._day_state()
    assert not day["blocked"] and day["skipped"] == ["icc25"]
    await advance(ports, 59)
    await controller.tick()
    assert not ports.sent
    await advance(ports, 1)
    await controller.tick()
    assert len(ports.sent) == 1 and (ports.sent[0].map_id, ports.sent[0].size) == (649, 25)
    assert [c[2] for c in armory.calls if c[0] == "bound_with"] == [631, 649]
    assert not controller._day_state()["blocked"]


@_run_async
async def test_a_plan_failure_without_a_fence_still_ends_the_day(setup):
    ports, armory, controller = setup
    ports.away_set = set(range(105, 131))
    await controller.tick()
    day = controller._day_state()
    assert day["blocked"] and not day.get("skipped")
    await advance(ports, 2 * 3600)
    await controller.tick()
    assert not ports.sent


@pytest.mark.parametrize("reason", ["setup:grouping:too_few", "setup:grouping:in_instance:113",
                                    "setup:grouping:busy:realm_owner:113", "setup:grouping:busy:realm_owner",
                                    "setup:teleporting:in_instance:113"])
@_run_async
async def test_a_setup_that_met_a_busy_body_is_tried_again_later_that_day(setup, reason):
    ports, _, controller = setup
    heard = Heard()
    controller.add_listener(heard)
    await start(setup)
    teleporting = reason.startswith("setup:teleporting:")
    if teleporting:
        await controller.on_job(event(controller, stage="teleporting"))
        await controller.tick()
    await controller.on_job(event(controller, state="failed", stage="", reason=reason,
                                  instance=77 if teleporting else 0))
    await controller.tick()
    assert not controller.active and not controller._day_state()["blocked"]
    await advance(ports, 14 * 60)
    await controller.tick()
    assert len(ports.sent) == 1
    await advance(ports, 60)
    await controller.tick()
    assert len(ports.sent) == 2
    await controller.on_job(event(controller))
    await controller.tick()
    # A teleporting failure has a closed record, so the rotation may move on to the next raid.
    assert [kind for kind, *_ in heard.events] == ["start"]


@_run_async
async def test_a_not_raided_offline_run_applies_no_decay_and_posts_nothing(setup):
    ports, _, controller = setup
    with ports.conn:
        ports.conn.execute("INSERT INTO meta(key,value) VALUES ('dkp_last_decay',?)", (OLD_DECAY,))
    await start(setup)
    await controller.on_job(event(controller, stage="teleporting"))
    await controller.tick()
    await controller.on_job(event(controller, state="failed", stage="", reason="setup:teleporting:timeout"))
    await controller.tick()
    assert decay_receipt(ports) == OLD_DECAY and not decay_lines(ports) and not ports.said
    assert ports.conn.execute("SELECT COUNT(*) FROM dkp_ledger WHERE reason='decay'").fetchone()[0] == 0


@_run_async
async def test_an_offline_run_that_reached_running_applies_the_due_decay_once(setup):
    ports, _, controller = setup
    with ports.conn:
        ports.conn.execute("INSERT INTO meta(key,value) VALUES ('dkp_last_decay',?)", (OLD_DECAY,))
    await start(setup)
    await controller.on_job(event(controller, stage="teleporting"))
    await controller.tick()
    assert decay_receipt(ports) == OLD_DECAY and not decay_lines(ports)
    await controller.on_job(event(controller))
    await controller.tick()
    assert decay_receipt(ports) != OLD_DECAY and len(decay_lines(ports)) == 1


@_run_async
async def test_attendance_runs_once_and_the_attended_flag_survives_a_restart(setup):
    ports, armory, controller = setup
    await running(setup)
    assert controller._owned["attended"] is True
    calls = []

    async def counted():
        calls.append(1)

    controller.session.attend = counted
    await controller.on_job(event(controller, stage="running"))
    await controller.tick()
    assert calls == []
    await controller._release()
    recreated = OfflineRaids(ports, controller.cfg, armory, random.Random(2))
    assert recreated._owned["attended"] is True
    recreated.session.attend = counted
    await recreated.on_job(event(recreated))
    await recreated.tick()
    await recreated.on_job(event(recreated, state="done", stage="", reason="cleared", kills=1))
    await recreated.tick()
    assert calls == [] and not recreated.active
