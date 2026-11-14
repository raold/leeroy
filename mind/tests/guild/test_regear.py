"""The one-time regear ([guild] regear_to): the cast's bots down to an item level, once per value."""

import asyncio
import json
from dataclasses import replace
from functools import wraps

import pytest

from leeroy_mind.guild.config import GuildConfig
from leeroy_mind.guild.protocol import GearUp, JobSkip
from leeroy_mind.guild.roster import (
    REGEAR_TRIES, REGEAR_WAIT, Roster, regear_key, regear_members, regear_target,
)
from leeroy_mind.guild.store import flags, new_raid, set_flag
from leeroy_mind.protocol import Hello, Login, Logout
from .fakes import FakeArmory, job, member, persona


CFG = replace(GuildConfig(), regear_to=245)
KEY = regear_key(245)


def _run_async(test):
    @wraps(test)
    def run(*args, **kwargs):
        return asyncio.run(test(*args, **kwargs))
    return run


@pytest.fixture
def cast(fake):
    fake.members = {101: member(101, "Aster", 5, 1), 102: member(102, "Birch", 8, 2),
                    103: member(103, "Cedar", 1, 2), 104: member(104, "Dahl", 9, 2),
                    105: member(105, "Ember", 11, 2)}
    fake.personas = {g: persona(g, m.name) for g, m in fake.members.items()}
    return fake


def hello(features=("regear",), started_ms=1000):
    return Hello(0, "test", "1", 21, 1, features=features, started_ms=started_ms)


async def advance(fake, seconds):
    await fake.clock.advance(seconds, step=max(1, seconds))


async def regearing(fake, cfg=CFG, *, features=("regear",), started_ms=1000):
    fake.online = False
    roster = Roster(fake, cfg, FakeArmory({}, {}), premade={})
    await roster.on_hello(hello(features, started_ms))
    await advance(fake, 600)
    return roster


def progress(command, guid, avg=243, **kwargs):
    return job("gear_up", "progress", id=command.id, guid=guid, level=kwargs.pop("level", 80),
               avg_ilvl=avg, **kwargs)


def done(command, guids=None, skipped=(), reason=""):
    guids = tuple(command.guids) if guids is None else guids
    return job("gear_up", "done" if not reason else "failed", id=command.id, guids=guids,
               skipped=tuple(skipped), reason=reason)


def state(fake, key=KEY):
    row = fake.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return json.loads(row[0]) if row else None


def regears(fake):
    return [c for c in fake.sent if isinstance(c, GearUp) and c.regear]


async def finish(roster, command, avg=243):
    for guid in command.guids:
        await roster.on_job(progress(command, guid, avg))
    await roster.on_job(done(command))


def test_regear_members_are_the_cast_only():
    members = [member(1, "Testmage", 8, 2), member(101, "Aster", 5, 1),
               replace(member(102, "Birch", 8, 2), rank=0), member(103, "Cedar", 1, 2, level=79),
               member(104, "Dahl", 9, 2, online=False)]
    # Never the owner (1), never a rank-0 row, never a member under 80; offline members still count.
    assert regear_members(members, 1) == [101, 104]


def test_theorycrafters_regear_to_their_own_level_when_it_is_higher():
    tc, other = persona(101, "Aster", thinks_as="theorycrafter"), persona(102, "Birch")
    assert regear_target(tc, CFG) == 251 and regear_target(other, CFG) == 245
    assert regear_target(None, CFG) == 245
    assert regear_target(tc, replace(CFG, theorycrafter_ilvl=0)) == 245
    assert regear_target(tc, replace(CFG, regear_to=264)) == 264  # the rollback: never below the guild


@_run_async
async def test_off_by_default_and_never_sent_to_a_native_without_the_feature(cast):
    roster = await regearing(cast, GuildConfig())
    await roster.tick()
    assert not regears(cast) and state(cast) is None
    roster = await regearing(cast, features=())
    await roster.tick()
    assert not regears(cast) and state(cast) is None
    # An older native leaves gear-up as it was: the regear never holds it back.
    assert not roster._regear_running()


@_run_async
async def test_one_batch_of_the_cast_never_the_owner_and_flags_superseded(cast):
    cast.members[1] = member(1, "Testmage", 8, 2)
    cast.personas[1] = persona(1, "Testmage")
    cast.members[106] = member(106, "Fern", 4, 1, level=70)
    cast.members[107] = replace(member(107, "Gale", 4, 1), rank=0)
    for guid in (101, 102):
        set_flag(cast.conn, "geared", guid, cast.clock.now())
    set_flag(cast.conn, "tc_geared", 102, cast.clock.now())
    roster = await regearing(cast)
    await roster.maybe_regear()
    [command] = regears(cast)
    assert command.guids == (101, 102, 103, 104, 105) and command.ilvl == 245 and command.level_to == 80
    assert state(cast)["members"] == [101, 102, 103, 104, 105]
    await finish(roster, command, avg=243)
    assert flags(cast.conn, "geared") == {101, 102, 103, 104, 105}
    assert not flags(cast.conn, "tc_geared")  # 102 is no theorycrafter: its old flag went with the old gear
    values = {r[0] for r in cast.conn.execute("SELECT value FROM guild_flag WHERE kind='geared'")}
    assert values == {245}
    await advance(cast, 120)
    await roster.maybe_regear()
    stored = state(cast)
    assert stored["state"] == "done" and stored["done"] == {str(g): 243 for g in range(101, 106)}
    # Once per value: no further regear, and gear-up runs again.
    await advance(cast, 3600)
    await roster.maybe_regear()
    assert len(regears(cast)) == 1
    assert not roster._regear_running()


@_run_async
async def test_a_new_value_runs_once_more_and_the_old_receipt_stays(cast):
    roster = await regearing(cast)
    await roster.maybe_regear()
    await finish(roster, regears(cast)[0])
    await advance(cast, 120)
    await roster.maybe_regear()
    assert state(cast)["state"] == "done"
    again = Roster(cast, replace(CFG, regear_to=264, gear_up_ilvl=264), roster.armory, premade={})
    await again.on_hello(hello())
    await advance(cast, 600)
    await again.maybe_regear()
    assert regears(cast)[-1].ilvl == 264 and state(cast, regear_key(264))["state"] == "running"
    assert state(cast)["state"] == "done"


@_run_async
async def test_theorycrafters_go_in_their_own_batch_at_their_level(cast):
    cast.personas[102].game.thinks_as = "theorycrafter"
    roster = await regearing(cast)
    await roster.maybe_regear()
    first = regears(cast)[-1]
    assert first.guids == (101, 103, 104, 105) and first.ilvl == 245
    await finish(roster, first)
    await advance(cast, 120)
    await roster.maybe_regear()
    second = regears(cast)[-1]
    assert second.guids == (102,) and second.ilvl == 251
    await roster.on_job(progress(second, 102, avg=250))
    assert {102} <= flags(cast.conn, "tc_geared") and 102 in flags(cast.conn, "geared")
    await roster.on_job(done(second))
    await advance(cast, 120)
    await roster.maybe_regear()
    assert state(cast)["state"] == "done"
    # The theorycrafter pass does not take a regeared theorycrafter back down or up.
    await advance(cast, 3600)
    await roster.maybe_gear_theorycrafters()
    assert not [c for c in cast.sent if isinstance(c, GearUp) and not c.regear]


@pytest.mark.parametrize(("avg", "lands"), [(225, True), (224, False), (251, True), (252, False)])
@_run_async
async def test_receipt_bounds_are_near_the_target_not_above_it(cast, avg, lands):
    roster = await regearing(cast)
    await roster.maybe_regear()
    command = regears(cast)[-1]
    await roster.on_job(progress(command, 101, avg))
    stored = state(cast)
    assert ("101" in stored["done"]) is lands
    assert (101 in flags(cast.conn, "geared")) is lands
    if not lands:
        assert stored["tries"] == {"101": 1} and stored["wait"]["101"] == cast.clock.now() + REGEAR_WAIT
        # The same progress again (a replay) spends nothing more.
        await roster.on_job(progress(command, 101, avg))
        assert state(cast)["tries"] == {"101": 1}


@_run_async
async def test_receipt_checks_still_apply(cast):
    roster = await regearing(cast)
    await roster.maybe_regear()
    command = regears(cast)[-1]
    for event in (replace(progress(command, 101), id="foreign"), progress(command, 106),
                  progress(command, 101, level=79)):
        await roster.on_job(event)
    assert "101" not in state(cast)["done"] and not flags(cast.conn, "geared")


@_run_async
async def test_only_in_windows_never_while_he_plays_or_a_raid_runs(cast):
    cast.online = True
    roster = Roster(cast, CFG, FakeArmory({}, {}), premade={})
    await roster.on_hello(hello())
    await roster.tick()
    assert not regears(cast)
    cast.online = False
    await roster.on_logout(Logout(0, 1, True))
    await advance(cast, 599)
    await roster.maybe_regear()
    assert not regears(cast)
    await advance(cast, 1)
    raid = new_raid(cast.conn, started=cast.clock.now(), raid_key="icc", size=10, heroic=False,
                    offline=True, lead_guid=101, tank_guid=103, loot_guid=101)
    await roster.maybe_regear()
    assert not regears(cast)
    with cast.conn:
        cast.conn.execute("UPDATE guild_raid SET ended=? WHERE raid_id=?", (cast.clock.now(), raid))
    busy = Roster(cast, CFG, FakeArmory({}, {}), premade={}, raid_active=lambda: True)
    await busy.on_hello(hello())
    await advance(cast, 600)
    await busy.maybe_regear()
    assert not regears(cast)
    await roster.maybe_regear()
    assert len(regears(cast)) == 1


@_run_async
async def test_his_login_stops_it_and_the_next_window_resumes(cast):
    roster = await regearing(cast)
    await roster.maybe_regear()
    first = regears(cast)[-1]
    # Native ends the job at his login: the rest are skipped owner_online and wait for nothing.
    await roster.on_job(progress(first, 101))
    await roster.on_job(done(first, guids=(101,), reason="owner_online",
                             skipped=[JobSkip(g, "owner_online") for g in first.guids[1:]]))
    cast.online = True
    await roster.on_login(Login(0, 1, True))
    await advance(cast, 3600)
    await roster.tick()
    assert len(regears(cast)) == 1
    cast.online = False
    await roster.on_logout(Logout(0, 1, True))
    await advance(cast, 600)
    await roster.maybe_regear()
    resumed = regears(cast)[-1]
    assert resumed.guids == (102, 103, 104, 105) and not state(cast)["tries"] and not state(cast)["wait"]


@_run_async
async def test_resumes_after_a_mind_restart_without_resending_done_members(cast):
    roster = await regearing(cast)
    await roster.maybe_regear()
    command = regears(cast)[-1]
    for guid in (101, 102):
        await roster.on_job(progress(command, guid))
    # The mind stops before the terminal; the same worldserver finishes the job meanwhile.
    reopened = await regearing(cast)
    await reopened.maybe_regear()
    assert len(regears(cast)) == 1  # the batch still owns gear-up until its terminal
    await reopened.on_job(done(command))
    await advance(cast, 120)
    await reopened.maybe_regear()
    assert regears(cast)[-1].guids == (103, 104, 105)


@_run_async
async def test_a_restarted_worldserver_releases_the_batch_it_lost(cast):
    roster = await regearing(cast, started_ms=1000)
    await roster.maybe_regear()
    command = regears(cast)[-1]
    await roster.on_job(progress(command, 101))
    await roster.on_hello(hello(started_ms=1000))  # the same process: still its job
    assert roster._get("gear") is not None
    await roster.on_hello(hello(started_ms=5000))
    assert roster._get("gear") is None
    await advance(cast, 120)
    await roster.maybe_regear()
    assert regears(cast)[-1].guids == (102, 103, 104, 105)


@pytest.mark.parametrize("reason", ["not_bot", "not_cast", "owner"])
@_run_async
async def test_a_refusal_naming_a_member_leaves_that_member_out(cast, reason):
    cast.acks["gear_up"].append((False, f"{reason}:102"))
    roster = await regearing(cast)
    await roster.maybe_regear()
    assert roster._get("gear") is None and state(cast)["dropped"] == [102]
    cast.acks["gear_up"].clear()
    cast.acks["gear_up"].append((False, "owner_online"))
    await advance(cast, 120)
    await roster.maybe_regear()
    assert state(cast)["dropped"] == [102]  # his presence names nobody
    await advance(cast, 120)
    await roster.maybe_regear()
    assert regears(cast)[-1].guids == (101, 103, 104, 105)
    assert all(102 not in c.guids for c in regears(cast)[1:])


@_run_async
async def test_skipped_members_wait_failures_spend_tries_and_it_still_finishes(cast):
    cast.members = {g: cast.members[g] for g in (101, 102, 103)}
    roster = await regearing(cast)
    for attempt in range(REGEAR_TRIES):
        await roster.maybe_regear()
        command = regears(cast)[-1]
        assert 101 in command.guids
        landed = [g for g in command.guids if g != 101 and g != 102]
        for guid in landed:
            await roster.on_job(progress(command, guid))
        await roster.on_job(done(command, guids=tuple(landed),
                                 skipped=[JobSkip(101, "failed")]
                                 + ([JobSkip(102, "busy:realm_owner")] if 102 in command.guids else [])))
        await advance(cast, REGEAR_WAIT)
    await roster.maybe_regear()
    command = regears(cast)[-1]
    assert command.guids == (102,)  # 101 gave up; 102, only ever fenced, is sent again
    await finish(roster, command)
    await advance(cast, 120)
    await roster.maybe_regear()
    stored = state(cast)
    assert stored["state"] == "done" and stored["gave_up"] == [101] and set(stored["done"]) == {"102", "103"}


@_run_async
async def test_a_fenced_member_waits_before_it_is_sent_again(cast):
    cast.members = {g: cast.members[g] for g in (101,)}
    roster = await regearing(cast)
    await roster.maybe_regear()
    command = regears(cast)[-1]
    await roster.on_job(done(command, guids=(), skipped=[JobSkip(101, "grouped")]))
    await advance(cast, 120)
    await roster.maybe_regear()
    assert len(regears(cast)) == 1 and not state(cast)["tries"]
    await advance(cast, REGEAR_WAIT)
    await roster.maybe_regear()
    assert len(regears(cast)) == 2


@_run_async
async def test_members_who_leave_the_cast_are_dropped(cast):
    roster = await regearing(cast)
    await roster.maybe_regear()
    command = regears(cast)[-1]
    await roster.on_job(done(command, guids=(), skipped=[JobSkip(g, "in_combat") for g in command.guids]))
    cast.members.pop(103)
    cast.members[104] = replace(cast.members[104], rank=0)
    await advance(cast, REGEAR_WAIT)
    await roster.maybe_regear()
    assert regears(cast)[-1].guids == (101, 102, 105)
    assert state(cast)["dropped"] == [103, 104]


@_run_async
async def test_gear_up_and_theorycrafters_wait_for_an_unfinished_regear(cast):
    cast.members[101] = member(101, "Aster", 5, 1, level=70)  # an under-80 member gear-up would take
    cast.personas[103].game.thinks_as = "theorycrafter"
    roster = await regearing(cast)
    await roster.maybe_gear_up()
    await roster.maybe_gear_theorycrafters()
    assert not cast.sent
    await roster.maybe_regear()
    for command in regears(cast):
        await finish(roster, command)
    for _ in range(2):
        await advance(cast, 120)
        await roster.maybe_regear()
        for command in regears(cast)[-1:]:
            if roster._get("gear"):
                await finish(roster, command)
    assert state(cast)["state"] == "done"
    await advance(cast, 3600)
    await roster.maybe_gear_up()
    gear = [c for c in cast.sent if isinstance(c, GearUp) and not c.regear]
    assert gear and gear[-1].guids == (101,) and gear[-1].ilvl == 245


@_run_async
async def test_an_unreadable_receipt_runs_nothing(cast):
    with cast.conn:
        cast.conn.execute("INSERT INTO meta(key,value) VALUES (?,?)", (KEY, json.dumps({"state": "running"})))
    roster = await regearing(cast)
    await roster.maybe_regear()
    assert not regears(cast)


@_run_async
async def test_the_owner_is_never_sent_even_from_a_stored_member_list(cast):
    with cast.conn:
        cast.conn.execute("INSERT INTO meta(key,value) VALUES (?,?)", (KEY, json.dumps({
            "ilvl": 245, "state": "running", "started": 0, "finished": None, "members": [1, 101],
            "done": {}, "kept": {}, "gave_up": [], "dropped": [], "tries": {}, "wait": {}})))
    cast.members[1] = member(1, "Testmage", 8, 2)
    roster = await regearing(cast)
    await roster.maybe_regear()
    assert regears(cast)[-1].guids == (101,) and state(cast)["dropped"] == [1]
