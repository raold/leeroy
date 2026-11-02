"""Independent regressions for queued raid lifecycle and actual summon coverage."""

import asyncio
import threading

import pytest

from leeroy_mind.guild.protocol import ClearLockout, FormRaid, JobSkip, Summon
from leeroy_mind.guild.raid_night import EVENT_LIMIT, State
from leeroy_mind.protocol import Boss, Death, Logout

from .fakes import job
from .test_raid_night import (
    _run_async, advance, chat, controls, form, inside, running, setup,
)


@pytest.mark.parametrize("late_guid", [101, 130])
@_run_async
async def test_review_late_entrance_arrival_is_still_in_the_later_inside_wave(setup, late_guid):
    ports, _, night = setup
    night.summon_plan = lambda members: ([g for g in members if g != late_guid], [(late_guid, 10)])
    await form(setup)
    assert late_guid in night.run.plan.members
    await advance(ports, 11)
    await night.tick()
    assert any(isinstance(cmd, Summon) and cmd.where == "entrance" and cmd.guids == (late_guid,)
               for cmd in ports.sent)

    await inside(setup)
    inside_members = {g for cmd in ports.sent if isinstance(cmd, Summon) and cmd.where == "inside"
                      for g in cmd.guids}
    assert late_guid in inside_members
    assert late_guid in night._arrived
    await advance(ports, 30)
    await night.tick()
    await advance(ports, 20)
    await night.tick()
    assert night.state is State.RUNNING
    assert len(controls(ports, "dc_on")) == 1


@pytest.mark.parametrize("ending", ["called", "logout"])
@_run_async
async def test_review_pre_end_kill_is_accounted_before_close_and_later_kill_is_ignored(setup, ending):
    ports, _, night = setup
    await running(setup)
    session = night.session
    heard = []

    class Listener:
        async def on_boss(self, ev):
            heard.append(("boss", ev.boss_id))

        async def on_end(self, outcome):
            heard.append(("end", outcome))

    night.add_listener(Listener())
    await night.on_boss(Boss(0, 631, 77, 1, "Testboss", "killed", (1, 101)))
    # The dispatcher receives the end before its independent ticker gets a turn.
    if ending == "logout":
        ports.online = False
        await night.on_logout(Logout(0, 1, True))
    else:
        await night.on_chat(chat(ports, "calling it"))
    await night.on_boss(Boss(0, 631, 77, 2, "Later boss", "killed", (1, 101)))
    await night.tick()
    row = ports.conn.execute("SELECT kills,ended,outcome FROM guild_raid WHERE raid_id=?",
                             (session.raid_id,)).fetchone()
    assert tuple(row)[::2] == (1, ending)
    assert row[1] is not None
    awards = ports.conn.execute("SELECT guid FROM dkp_ledger WHERE raid_id=? AND reason='kill' ORDER BY guid",
                                (session.raid_id,)).fetchall()
    assert [row[0] for row in awards] == [1, 101]
    # Session accounting drains accepted input; action listeners are retired
    # immediately at the end boundary and must not react to that old boss.
    assert heard == [("end", ending)]
    assert night._votes


@pytest.mark.parametrize("ending", ["called", "logout"])
@_run_async
async def test_review_ending_invalidates_actions_before_background_close(setup, ending):
    ports, _, night = setup
    await running(setup)
    old_run = night.run
    invalidated, acted, ended = [], [], []

    class ActionListener:
        def invalidate(self, run):
            invalidated.append(run)

        async def on_boss(self, ev):
            acted.append(ev)

        async def on_end(self, outcome, *, run=None):
            ended.append((outcome, run))

    night.add_listener(ActionListener())
    await night.on_boss(Boss(0, 631, 77, 1, "Testboss", "killed", (1, 101)))
    if ending == "logout":
        ports.online = False
        await night.on_logout(Logout(0, 1, True))
    else:
        await night.on_chat(chat(ports, "calling it"))
    assert invalidated == [old_run] and not ended and not acted
    await night.tick()
    assert not acted and ended == [(ending, old_run)]
    assert ports.conn.execute("SELECT kills FROM guild_raid WHERE raid_id=?",
                              (old_run.session.raid_id,)).fetchone()[0] == 1


@_run_async
async def test_review_foreign_event_flood_cannot_discard_a_queued_valid_kill(setup):
    ports, _, night = setup
    await running(setup)
    await night.on_boss(Boss(0, 631, 77, 1, "Testboss", "killed", (1, 101)))
    for _ in range(EVENT_LIMIT + 1):
        await night.on_death(Death(0, 999, 1, 0))
    await night.tick()
    assert night.session.kills == 1


@_run_async
async def test_review_backfill_member_is_cleared_before_joining_existing_raid(setup):
    ports, _, night = setup

    def first_job(command):
        assert 130 in command.members
        return job("form_raid", "done", guids=tuple(g for g in command.members if g != 130),
                   skipped=(JobSkip(130, "offline"),))

    ports.jobs["form_raid"].append(first_job)
    await form(setup)
    forms = [command for command in ports.sent if isinstance(command, FormRaid)]
    assert len(forms) == 2 and night.state is State.ASSEMBLING
    added = set(forms[1].members) - set(forms[0].members)
    assert len(added) == 1
    second_index = ports.sent.index(forms[1])
    cleared = {guid for command in ports.sent[:second_index] if isinstance(command, ClearLockout)
               and command.map_id == 631 for guid in command.guids}
    assert added <= cleared


@_run_async
async def test_review_pick_handler_defers_blocking_armory_lookup_to_worker_thread(setup):
    ports, armory, night = setup
    dispatcher_thread = threading.get_ident()
    calls = []

    def human_tier(guid):
        assert threading.get_ident() != dispatcher_thread, "SQL armory blocked the event loop"
        calls.append(guid)
        return 16

    armory.human_tier = human_tier
    night.state = State.CALLED
    night._call_deadline = ports.clock.now() + 600
    await night.on_chat(chat(ports, "icc 25"))
    assert calls == [] and ports.sent == []
    await night.tick()
    assert calls == [ports.human_guid]
    assert night.state is State.ASSEMBLING


@_run_async
async def test_review_logout_during_armory_lookup_prevents_all_formation_commands(setup):
    ports, armory, night = setup
    dispatcher_thread = threading.get_ident()
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    release = threading.Event()

    def human_tier(guid):
        assert threading.get_ident() != dispatcher_thread, "SQL armory blocked the event loop"
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(3), "test failed to release synthetic armory lookup"
        return 16

    armory.human_tier = human_tier
    night.state = State.CALLED
    night._call_deadline = ports.clock.now() + 600
    await night.on_chat(chat(ports, "icc 25"))
    work = asyncio.create_task(night.tick())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        ports.online = False
        await night.on_logout(Logout(0, 1, True))
    finally:
        release.set()
        await work
    assert ports.sent == []
    assert night.state is State.ENDED and night.session is None


@_run_async
async def test_review_logout_during_boss_listener_does_not_replay_delivered_listener(setup):
    ports, _, night = setup
    await running(setup)
    entered, release = asyncio.Event(), asyncio.Event()
    heard = []

    class First:
        async def on_boss(self, ev):
            heard.append("first")
            entered.set()
            await release.wait()

    class Second:
        async def on_boss(self, ev):
            heard.append("second")

        async def on_end(self, outcome):
            heard.append("end")

    night.add_listener(First())
    night.add_listener(Second())
    await night.on_boss(Boss(0, 631, 77, 1, "Testboss", "killed", (1, 101)))
    work = asyncio.create_task(night.tick())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        ports.online = False
        await night.on_logout(Logout(0, 1, True))
    finally:
        release.set()
        await work
    await night.tick()
    assert heard == ["first", "second", "end"]
