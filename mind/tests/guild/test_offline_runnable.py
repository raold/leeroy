"""Offline raids plan only what the runner can kill, and every stall names its cause."""

from dataclasses import replace

import pytest

from leeroy_mind.guild.config import GuildConfig
from leeroy_mind.guild.offline import UNSUPPORTED, next_raid_key, stall_cause, unsupported
from leeroy_mind.guild.protocol import StopOfflineRaid
from leeroy_mind.guild.raids import split_key
from leeroy_mind.guild.lines import OUTCOME_NEUTRAL, OUTCOME_WORDS, outcome_words
from .test_offline import attendance, closing_lines, event, not_raided, setup, start  # noqa: F401  (fixture)
from .test_raid_night import _run_async, advance, setup as raid_setup  # noqa: F401  (fixture)


def test_runner_never_plans_raids_dungeon_clear_cannot_run():
    assert next_raid_key(("toc25", "ulduar25", "icc10"), {}) == "icc10"
    assert next_raid_key(("TOC10H", "ulduar10", "naxx25"), {"naxx25": 5}) == "naxx25"
    with pytest.raises(ValueError):
        next_raid_key(("toc25", "ulduar25"), {})
    with pytest.raises(ValueError):
        next_raid_key(("toc25", "icc25"), {}, skip={"icc25"})
    assert unsupported(("toc25", "ulduar25", "icc25", "TOC10H")) == {
        "toc25": "dc_no_bosses", "ulduar25": "dc_gate", "toc10h": "dc_no_bosses"}
    assert unsupported(("icc25", "naxx10")) == {}


def test_default_rotation_holds_only_raids_the_runner_can_run():
    rotation = GuildConfig().offline_rotation
    assert not unsupported(rotation)
    assert {split_key(key)[0].key for key in rotation} == {"icc", "naxx"}
    # He raids Naxxramas 25 himself: an offline 25-man save would lock his raiders out of his copy.
    assert "naxx25" not in rotation and "naxx10" in rotation
    assert set(UNSUPPORTED) == {"toc", "ulduar"}


@pytest.mark.parametrize(("dc", "inside", "cause"), [
    ("door", 23, "dc_door_blocked"), ("paused", 23, "dc_paused"), ("off", 23, "dc_off"),
    ("door", 10, "dc_door_blocked"), ("on", 22, "members_out"), ("on", 23, "no_boss_reached"),
    (None, 23, "unknown"), (None, 20, "members_out")])
def test_stall_cause_reads_dungeon_clear_first(dc, inside, cause):
    assert stall_cause(dict(dc=dc, inside=inside), 23) == cause


def offline_log(caplog, name):
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith(name)]


@_run_async
async def test_unsupported_raids_in_a_configured_rotation_are_passed_over_and_logged_once(setup, caplog):
    ports, _, controller = setup
    controller.cfg = replace(controller.cfg, offline_rotation=("toc25", "ulduar25", "icc10"))
    with caplog.at_level("INFO"):
        await start(setup)
        assert (ports.sent[-1].map_id, ports.sent[-1].size) == (631, 10)
        controller._raid_key()
    assert sorted(offline_log(caplog, "offline_raid_unsupported")) == [
        "offline_raid_unsupported raid=toc25 reason=dc_no_bosses",
        "offline_raid_unsupported raid=ulduar25 reason=dc_gate"]


@_run_async
async def test_a_rotation_of_only_unsupported_raids_holds_the_day(setup):
    ports, _, controller = setup
    controller.cfg = replace(controller.cfg, offline_rotation=("toc25", "ulduar25"))
    await controller.tick()
    assert not ports.sent and not ports.gpu and controller._day_state()["blocked"]


@_run_async
async def test_a_map_dungeon_clear_finds_no_boss_on_passes_to_the_next_raid_that_day(setup):
    ports, _, controller = setup
    await start(setup)
    assert (ports.sent[-1].map_id, ports.sent[-1].size) == (631, 25)
    await controller.on_job(event(controller, stage="starting"))
    await controller.on_job(event(controller, state="failed", stage="", reason="setup:starting:dc_no_bosses"))
    await controller.tick()
    day = controller._day_state()
    assert not controller.active and not day["blocked"] and day["skipped"] == ["icc25"]
    assert not [m for m in ports.memories if "without" in m.text]
    await advance(ports, 60)
    await controller.tick()
    assert len(ports.sent) == 2 and (ports.sent[-1].map_id, ports.sent[-1].size) == (631, 10)


async def heartbeat(setup, *, age=0, inside=25, engaged=False, **changes):
    ports, _, controller = setup
    now = int(ports.clock.now() * 1000)
    fields = dict(ts=now, last_progress_ts=now - age * 1000, members_in_instance=inside, boss_engaged=engaged)
    fields.update(changes)
    await controller.on_job(event(controller, **fields))
    await controller.tick()


@pytest.mark.parametrize(("dc", "inside", "cause"), [
    ("door", 25, "dc_door_blocked"), ("on", 25, "no_boss_reached"), ("on", 21, "members_out"),
    (None, 25, "unknown"), ("garbage", 25, "unknown")])
@_run_async
async def test_a_stall_names_its_cause_in_the_log_and_the_day_receipt(setup, caplog, dc, inside, cause):
    ports, _, controller = setup
    await start(setup)
    await heartbeat(setup)
    raid_id = controller.session.raid_id
    await advance(ports, 1800)
    with caplog.at_level("INFO"):
        await heartbeat(setup, age=1800, inside=inside, dc_state=dc)
        stops = [c for c in ports.sent if isinstance(c, StopOfflineRaid)]
        assert len(stops) == 1 and controller._owned["stop"]["cause"] == cause
        await controller.on_job(event(controller, state="done", stage="", reason="stopped"))
        await controller.tick()
    stop_line, = offline_log(caplog, "offline_raid_stop ")
    assert f"reason=stalled cause={cause} idle_s=1800" in stop_line
    ended, = offline_log(caplog, "offline_raid_ended")
    assert f"outcome=stalled cause={cause} kills=0 wipes=0" in ended
    # The row keeps the stable code the wrap-up reads; the cause rides the day receipt and the log.
    assert ports.conn.execute("SELECT outcome FROM guild_raid WHERE raid_id=?", (raid_id,)).fetchone()[0] == "stalled"
    assert controller._day_state()["stall_cause"] == cause
    assert not controller.active


@_run_async
async def test_native_door_stop_ends_the_run_under_its_own_name(setup, caplog):
    ports, _, controller = setup
    await start(setup)
    await heartbeat(setup, dc_state="on")
    raid_id = controller.session.raid_id
    await advance(ports, 600)
    with caplog.at_level("INFO"):
        await controller.on_job(event(controller, state="done", stage="", reason="dc_door_blocked", kills=2))
        await controller.tick()
    assert not [c for c in ports.sent if isinstance(c, StopOfflineRaid)]
    row = ports.conn.execute("SELECT outcome,kills FROM guild_raid WHERE raid_id=?", (raid_id,)).fetchone()
    assert tuple(row) == ("dc_door_blocked", 2)
    ended, = offline_log(caplog, "offline_raid_ended")
    assert "outcome=dc_door_blocked cause=- kills=2" in ended
    day = controller._day_state()
    assert not day["blocked"] and "stall_cause" not in day and not day.get("skipped")


@_run_async
async def test_operator_stop_is_named_as_such(setup, caplog):
    ports, _, controller = setup
    await start(setup)
    await heartbeat(setup, dc_state="on")
    with caplog.at_level("INFO"):
        await controller.request_stop(controller._owned["id"])
        await controller.tick()
    assert controller._owned["stop"]["cause"] == "operator"


@_run_async
async def test_a_raid_he_holds_a_save_of_at_that_size_is_passed_over_for_the_day(setup, caplog):
    ports, armory, controller = setup
    armory.saves = {1: {(631, 25)}, 101: {(631, 10)}}  # His ICC 25 this week; a member's save is not his.
    with caplog.at_level("INFO"):
        await controller.tick()
    assert not ports.sent and not ports.gpu
    day = controller._day_state()
    assert day["skipped"] == ["icc25"] and not day["blocked"] and day["attempts"] == 0
    assert offline_log(caplog, "offline_raid_skipped") == ["offline_raid_skipped raid=icc25 reason=owner_saved"]
    assert ("saved_sizes", 1, 631) in armory.calls
    await advance(ports, 60)
    await controller.tick()
    assert len(ports.sent) == 1 and (ports.sent[0].map_id, ports.sent[0].size) == (631, 10)


@_run_async
async def test_his_saves_of_every_size_of_a_raid_pass_to_the_next_raid_heroic_included(setup):
    ports, armory, controller = setup
    controller.cfg = replace(controller.cfg, offline_rotation=("icc25h", "icc10", "naxx10"))
    armory.saves = {1: {(631, 25), (631, 10)}}
    for _ in range(3):
        await controller.tick()
        await advance(ports, 60)
    assert len(ports.sent) == 1 and (ports.sent[0].map_id, ports.sent[0].size) == (533, 10)
    assert controller._day_state()["skipped"] == ["icc25h", "icc10"]


@_run_async
async def test_unreadable_saves_hold_the_start_like_unreadable_binds(setup):
    from leeroy_mind.personas.roster import RosterError
    ports, armory, controller = setup

    def unreadable(*args):
        raise RosterError("guild armory unavailable")

    armory.saved_sizes = unreadable
    await controller.tick()
    assert not ports.sent and not ports.gpu and not controller._day_state()["blocked"]
    assert controller._day_state()["attempts"] == 1


@_run_async
async def test_a_door_block_before_any_fight_voids_the_run_and_takes_its_attendance_back(setup):
    ports, _, controller = setup
    await start(setup)
    await heartbeat(setup, dc_state="on")
    raid_id = controller.session.raid_id
    assert attendance(ports, raid_id)[0] > 0  # Attendance is recorded once the run reaches running.
    await advance(ports, 600)
    await controller.on_job(event(controller, state="done", stage="", reason="dc_door_blocked"))
    await controller.tick()
    assert not controller.active
    assert attendance(ports, raid_id) == (0, 0) and not_raided(ports, raid_id)
    assert closing_lines(ports) == ([], [])
    assert not [m for m in ports.memories if "without" in m.text]
    day = controller._day_state()
    assert not day["blocked"] and day["skipped"] == ["icc25"]
    assert ports.conn.execute("SELECT outcome FROM guild_raid WHERE raid_id=?", (raid_id,)).fetchone()[0] == "dc_door_blocked"
    await advance(ports, 60)
    await controller.tick()
    assert len(ports.sent) == 2 and (ports.sent[-1].map_id, ports.sent[-1].size) == (631, 10)


@_run_async
async def test_a_door_block_after_a_wipe_still_counts_as_raided(setup):
    ports, _, controller = setup
    await start(setup)
    await heartbeat(setup, dc_state="on")
    raid_id = controller.session.raid_id
    await controller.on_job(event(controller, state="done", stage="", reason="dc_door_blocked", wipes=1))
    await controller.tick()
    assert attendance(ports, raid_id)[0] > 0 and not not_raided(ports, raid_id)
    assert not controller._day_state().get("skipped")


def test_outcome_codes_reach_the_wrap_up_as_words_and_unknown_codes_read_neutrally():
    assert outcome_words("dc_door_blocked") == "got stuck at a door we couldn't open"
    assert outcome_words("cleared_reachable") == "cleared every boss we could reach"
    assert outcome_words("logout") == "the raid leader logged off"
    for code in ("terminal_lost", "encounters_changed", "event_overflow", "setup:starting:timeout", None):
        assert outcome_words(code) == OUTCOME_NEUTRAL
    assert all("_" not in words and ":" not in words for words in OUTCOME_WORDS.values())


@_run_async
async def test_the_wrap_up_line_gets_words_not_the_native_code(setup):
    ports, _, controller = setup
    await start(setup)
    await heartbeat(setup, dc_state="on")
    await controller.on_job(event(controller, state="done", stage="", reason="cleared_reachable", kills=13))
    await controller.tick()
    _, wrapups = closing_lines(ports)
    assert len(wrapups) == 1 and "outcome cleared every boss we could reach." in wrapups[0]
    assert "cleared_reachable" not in wrapups[0]
