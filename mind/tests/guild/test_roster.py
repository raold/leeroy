import asyncio
from dataclasses import replace
from functools import wraps
import sqlite3

import pytest

from leeroy_mind.guild.config import GuildConfig
from leeroy_mind.guild.protocol import GearUp, JobSkip, RemoveFriend, SetSpec
from leeroy_mind.guild.roster import (
    Roster, closeness, desired_rank, friends_plan, gear_up_targets, rank_changes,
    spec_changes, theorycrafter_targets,
)
from leeroy_mind.guild.store import flags, new_raid, set_flag
from leeroy_mind.protocol import Ack, AddFriend, Hello, Login, Logout, SetRank
from leeroy_mind.storage.rows import Relationship
from .fakes import FakeArmory, job, member, persona


PREMADE = {1: ["arms pve", "fury pve", "prot pve"],
           5: ["disc pve", "holy pve", "shadow pve"], 8: ["arcane pve", "fire pve", "frost pve"],
           9: ["affli pve", "demo pve", "destro pve"],
           11: ["balance pve", "bear pve", "resto pve", "cat pve"]}


def _run_async(test):
    @wraps(test)
    def run(*args, **kwargs):
        return asyncio.run(test(*args, **kwargs))
    return run


@pytest.fixture
def roster_data(fake):
    fake.members = {101: member(101, "Aster", 5, 2), 102: member(102, "Birch", 8, 2),
                    103: member(103, "Cedar", 1, 2)}
    fake.personas = {101: persona(101, "Aster", main_spec="Holy", raid_role="healer"),
                     102: persona(102, "Birch", main_spec="Frost", raid_role="ranged"),
                     103: persona(103, "Cedar", main_spec="Protection", raid_role="tank", gender="female")}
    armory = FakeArmory({}, {101: {0: 200}, 102: {0: 270}, 103: {0: 270}})
    return fake, armory


async def advance(fake, seconds):
    await fake.clock.advance(seconds, step=max(1, seconds))


async def offline(roster_data, **kwargs):
    fake, armory = roster_data
    fake.online = False
    roster = Roster(fake, kwargs.pop("cfg", GuildConfig()), armory, premade=PREMADE, **kwargs)
    await advance(fake, 600)
    return fake, roster


def progress(command, guid=101, **kwargs):
    return job("gear_up", "progress", id=command.id, guid=guid, level=kwargs.pop("level", 80),
               avg_ilvl=kwargs.pop("avg_ilvl", 251), **kwargs)


def test_desired_rank():
    ranks = GuildConfig().ranks
    assert desired_rank(None, ranks) is None
    for role, rank in (("officer", 1), ("raider", 2), ("social", 3)):
        assert desired_rank(persona(101, "Aster", guild_role=role), ranks) == rank
    assert desired_rank(persona(101, "Aster", guild_role="unknown"), ranks) is None
    assert desired_rank(persona(101, "Aster"), {"raider": 0}) is None


def test_rank_changes_preserve_human_and_guild_master(roster_data):
    fake, _ = roster_data
    fake.members[101] = replace(fake.members[101], rank=4)
    fake.members[102] = replace(fake.members[102], rank=0)
    fake.members[103] = replace(fake.members[103], rank=2)
    fake.members[1] = replace(fake.members[103], guid=1, rank=4)
    fake.personas[1] = persona(1, "Testmage")
    assert rank_changes(fake.cast(), fake.persona, 1, GuildConfig().ranks) == [(101, 2)]


def test_gear_up_targets(roster_data):
    fake, _ = roster_data
    fake.members[101] = replace(fake.members[101], level=67)
    assert gear_up_targets(fake.cast(), {101: 270, 102: 200, 103: 270}, set(), GuildConfig()) == [101, 102]
    fake.members[102] = replace(fake.members[102], online=False)
    assert gear_up_targets(fake.cast(), {}, {101, 103}, GuildConfig()) == []


def test_spec_changes_follow_named_pve_mapping_not_gender(roster_data):
    fake, _ = roster_data
    assert spec_changes(fake.cast(), fake.persona, PREMADE) == [(101, 1, True)]
    sparse = {**PREMADE, 5: ["", "shadow pvp", "", "holy pve"]}
    assert spec_changes(fake.cast(), fake.persona, sparse) == [(101, 3, True)]
    assert spec_changes(fake.cast(), fake.persona, {5: ["holy pvp"]}) == []


def test_spec_changes_same_role_and_missing_persona(roster_data):
    fake, _ = roster_data
    fake.members[101] = replace(fake.members[101], spec_tab=0)
    assert spec_changes(fake.cast(), fake.persona, PREMADE) == [(101, 1, False)]
    fake.personas.pop(101)
    assert spec_changes(fake.cast(), fake.persona, PREMADE) == []


def test_theorycrafter_targets(roster_data):
    fake, _ = roster_data
    fake.personas[102].game.thinks_as = "theorycrafter"
    assert theorycrafter_targets(fake.cast(), fake.persona, set()) == [102]
    assert theorycrafter_targets(fake.cast(), fake.persona, {102}) == []


def test_closeness():
    assert closeness(0, 0, 0) == pytest.approx(.25)
    assert closeness(100, 100, 100) == pytest.approx(1)
    assert closeness(-100, 0, 0) == 0
    assert closeness(40, 40, 60) == pytest.approx(.59)
    assert closeness(float("nan"), 0, 0) == 0


def test_friends_plan_hysteresis_and_cap():
    strength = {101: .6, 102: .5, 103: .35, 104: .2, 105: .9}
    assert friends_plan(strength, {103, 104}, cap=3, add=.45, remove=.30) == ([105, 101], [104])
    assert friends_plan({101: .6, 102: .5, 105: .9}, {101, 102, 105},
                        cap=2, add=.45, remove=.30) == ([], [102])
    assert friends_plan({102: .5, 101: .5}, set(), cap=1, add=.5, remove=.3) == ([101], [])
    assert friends_plan({101: .3}, {101}, cap=1, add=.5, remove=.3) == ([], [])


@_run_async
async def test_event_hooks_never_send_or_wait(roster_data):
    fake, armory = roster_data
    roster = Roster(fake, GuildConfig(), armory, premade=PREMADE)
    await roster.on_hello(Hello(0, "test", "1", 21, 1))
    await roster.on_personas_changed()
    await roster.on_logout(Logout(0, 1, True))
    await roster.on_login(Login(0, 1, True))
    await roster.on_job(job("gear_up", "progress", guid=101, level=80, avg_ilvl=270))
    assert not fake.sent and not flags(fake.conn, "geared")


@_run_async
async def test_sync_ranks_sends_set_rank(roster_data):
    fake, armory = roster_data
    fake.members[102] = replace(fake.members[102], rank=0)
    roster = Roster(fake, GuildConfig(), armory, premade=PREMADE)
    await roster.sync_ranks()
    assert [(c.guid, c.rank) for c in fake.sent if isinstance(c, SetRank)] == [(101, 2), (103, 2)]


@_run_async
async def test_unknown_rank_waits_for_observation_before_new_desired_rank(roster_data):
    fake, armory = roster_data
    fake.members = {101: fake.members[101]}
    roster = Roster(fake, GuildConfig(), armory, premade=PREMADE)
    original = fake.send

    async def lost(command):
        await original(command)
        raise TimeoutError

    fake.send = lost
    await roster.sync_ranks()
    await roster.sync_ranks()
    assert len(fake.sent) == 1
    fake.members[101] = replace(fake.members[101], rank=2)
    fake.personas[101].play.guild_role = "officer"
    fake.send = original
    await roster.sync_ranks()
    assert len(fake.sent) == 2 and fake.sent[-1].rank == 1


@_run_async
async def test_maintenance_waits_ten_minutes_and_stops_for_any_active_raid(roster_data):
    fake, armory = roster_data
    fake.online = False
    roster = Roster(fake, GuildConfig(), armory, premade=PREMADE)
    await roster.maybe_gear_up()
    await advance(fake, 599)
    await roster.sync_specs()
    assert not fake.sent
    await advance(fake, 1)
    raid = new_raid(fake.conn, started=fake.clock.now(), raid_key="icc", size=10, heroic=False,
                    offline=True, lead_guid=101, tank_guid=103, loot_guid=101)
    await roster.maybe_gear_up()
    assert not fake.sent
    with fake.conn:
        fake.conn.execute("UPDATE guild_raid SET ended=? WHERE raid_id=?", (fake.clock.now(), raid))
    await roster.maybe_gear_up()
    assert isinstance(fake.sent[-1], GearUp)


@_run_async
async def test_active_raid_callback_prevents_work_before_session_row_exists(roster_data):
    fake, roster = await offline(roster_data, raid_active=lambda: True)
    await roster.sync_specs()
    await roster.maybe_gear_up()
    await roster.maybe_gear_theorycrafters()
    assert not fake.sent


@_run_async
async def test_gear_up_once_flags_only_matching_qualifying_progress(roster_data):
    fake, roster = await offline(roster_data)
    await roster.maybe_gear_up()
    command = fake.sent[-1]
    assert command.guids == (101,) and command.ilvl == 245 and command.level_to == 80 and not command.regear
    await roster.maybe_gear_up()
    assert len(fake.sent) == 1 and not flags(fake.conn, "geared")
    for event in (replace(progress(command), id="foreign"), replace(progress(command), kind="offline_raid"),
                  progress(command, guid=102), progress(command, level=79), progress(command, avg_ilvl=231)):
        await roster.on_job(event)
    assert not flags(fake.conn, "geared")
    await roster.on_job(progress(command))
    assert flags(fake.conn, "geared") == {101}
    first = fake.conn.execute("SELECT ts FROM guild_flag WHERE kind='geared'").fetchone()[0]
    await advance(fake, 10)
    await roster.on_job(progress(command))
    assert fake.conn.execute("SELECT ts FROM guild_flag WHERE kind='geared'").fetchone()[0] == first
    await roster.on_job(job("gear_up", "done", id=command.id, guids=(101,)))
    await advance(fake, 3600)
    await roster.maybe_gear_up()
    assert len(fake.sent) == 1


@_run_async
async def test_skipped_gear_retries_only_after_hour_even_after_reopen(roster_data):
    fake, roster = await offline(roster_data)
    await roster.maybe_gear_up()
    command = fake.sent[-1]
    await roster.on_job(job("gear_up", "done", id=command.id, skipped=(JobSkip(101, "busy"),)))
    assert not flags(fake.conn, "geared")
    await advance(fake, 600)
    reopened = Roster(fake, GuildConfig(), roster.armory, premade=PREMADE)
    await advance(fake, 600)
    await reopened.maybe_gear_up()
    assert len(fake.sent) == 1
    await advance(fake, 2400)
    await reopened.maybe_gear_up()
    assert len(fake.sent) == 2 and fake.sent[-1].id != command.id


@_run_async
async def test_done_without_progress_never_creates_gear_flag(roster_data):
    fake, roster = await offline(roster_data)
    await roster.maybe_gear_up()
    command = fake.sent[-1]
    await roster.on_job(job("gear_up", "done", id=command.id, guids=(101,)))
    assert not flags(fake.conn, "geared")


@_run_async
async def test_malformed_terminal_cannot_release_owned_job(roster_data):
    fake, roster = await offline(roster_data)
    await roster.maybe_gear_up()
    command = fake.sent[-1]
    for event in (job("gear_up", "done", id=command.id),
                  job("gear_up", "done", id=command.id, guids=(102,)),
                  job("gear_up", "done", id=command.id, guids=(101,), skipped=(JobSkip(101, "busy"),))):
        await roster.on_job(event)
    await advance(fake, 7200)
    await roster.maybe_gear_up()
    assert len(fake.sent) == 1


@_run_async
async def test_unknown_gear_ack_survives_restart_without_resend(roster_data):
    fake, roster = await offline(roster_data)
    original = fake.send

    async def lost(command):
        await original(command)
        raise TimeoutError

    fake.send = lost
    await roster.maybe_gear_up()
    command = fake.sent[-1]
    reopened = Roster(fake, GuildConfig(), roster.armory, premade=PREMADE)
    await advance(fake, 7200)
    await reopened.maybe_gear_up()
    assert len(fake.sent) == 1
    await reopened.on_job(progress(command))
    assert flags(fake.conn, "geared") == {101}


@_run_async
async def test_cancellation_after_send_preserves_remote_job(roster_data):
    fake, roster = await offline(roster_data)
    original, started = fake.send, asyncio.Event()

    async def hanging(command):
        await original(command)
        started.set()
        await asyncio.Event().wait()

    fake.send = hanging
    task = asyncio.create_task(roster.maybe_gear_up())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    fake.send = original
    await advance(fake, 7200)
    await roster.maybe_gear_up()
    assert len(fake.sent) == 1


@_run_async
async def test_progress_during_send_is_not_overwritten(roster_data):
    fake, roster = await offline(roster_data)
    original = fake.send

    async def early(command):
        ack = await original(command)
        await roster.on_job(progress(command))
        await roster.on_job(job("gear_up", "done", id=command.id, guids=(101,)))
        return ack

    fake.send = early
    await roster.maybe_gear_up()
    assert flags(fake.conn, "geared") == {101} and roster._get("gear") is None


@_run_async
async def test_relogin_during_armory_await_stops_new_command(roster_data):
    fake, roster = await offline(roster_data)
    original = roster.armory.equipped

    def login(guids):
        fake.online = True
        return original(guids)

    roster.armory.equipped = login
    await roster.maybe_gear_up()
    assert not fake.sent


@_run_async
async def test_sync_specs_then_gear_for_new_role(roster_data):
    fake, roster = await offline(roster_data)
    await roster.sync_specs()
    assert isinstance(fake.sent[0], SetSpec) and fake.sent[0].spec_no == 1 and fake.sent[0].glyphs
    assert isinstance(fake.sent[1], GearUp) and fake.sent[1].guids == (101,)
    assert flags(fake.conn, "spec") == {101} and not flags(fake.conn, "geared")
    await roster.on_job(progress(fake.sent[1]))
    assert flags(fake.conn, "geared") == {101}
    assert roster._role_queue() == {}


@_run_async
async def test_busy_spec_retries_next_hour(roster_data):
    fake, roster = await offline(roster_data)
    fake.acks["set_spec"].append((False, "busy"))
    await roster.sync_specs()
    assert not flags(fake.conn, "spec") and len(fake.sent) == 1
    await roster.sync_specs()
    assert len(fake.sent) == 1
    await advance(fake, 3600)
    await roster.sync_specs()
    assert len(fake.sent) == 3


@_run_async
async def test_unknown_spec_ack_waits_for_actual_tab_before_role_gear(roster_data):
    fake, roster = await offline(roster_data)
    original = fake.send

    async def wrong_id(command):
        return replace(await original(command), id="unrelated")

    fake.send = wrong_id
    await roster.sync_specs()
    await advance(fake, 3600)
    await roster.sync_specs()
    assert len(fake.sent) == 1 and not flags(fake.conn, "spec")
    fake.members[101] = replace(fake.members[101], spec_tab=1)
    fake.send = original
    await advance(fake, 3600)
    await roster.sync_specs()
    assert len(fake.sent) == 2 and isinstance(fake.sent[-1], GearUp)
    assert not flags(fake.conn, "spec")  # No acknowledgement was invented.


@_run_async
async def test_observed_unknown_spec_allows_a_new_persona_request(roster_data):
    fake, roster = await offline(roster_data)
    original = fake.send

    async def lost(command):
        await original(command)
        raise TimeoutError

    fake.send = lost
    await roster.sync_specs()
    fake.members[101] = replace(fake.members[101], spec_tab=1)
    fake.personas[101].play.main_spec = "Discipline"
    fake.send = original
    await advance(fake, 3600)
    await roster.sync_specs()
    assert [c.spec_no for c in fake.sent if isinstance(c, SetSpec)] == [1, 0]
    assert not any(isinstance(c, GearUp) for c in fake.sent)


@_run_async
async def test_relogin_during_spec_ack_prevents_role_gear(roster_data):
    fake, roster = await offline(roster_data)
    original = fake.send

    async def login(command):
        ack = await original(command)
        fake.online = True
        await roster.on_login(Login(0, 1, True))
        return ack

    fake.send = login
    await roster.sync_specs()
    assert len(fake.sent) == 1 and isinstance(fake.sent[0], SetSpec)
    assert flags(fake.conn, "spec") == {101}


@_run_async
async def test_persona_change_during_ack_does_not_flag_or_gear_old_role(roster_data):
    fake, roster = await offline(roster_data)
    original = fake.send

    async def changed(command):
        ack = await original(command)
        fake.personas[101].play.main_spec = "Shadow"
        fake.personas[101].play.raid_role = "ranged"
        return ack

    fake.send = changed
    await roster.sync_specs()
    assert len(fake.sent) == 1 and not flags(fake.conn, "spec")


@_run_async
async def test_next_spec_candidate_is_revalidated_after_previous_ack(roster_data):
    fake, roster = await offline(roster_data)
    fake.members[102] = replace(fake.members[102], spec_tab=1)
    original = fake.send

    async def changed(command):
        ack = await original(command)
        if isinstance(command, SetSpec) and command.guid == 101:
            fake.personas[102].play.main_spec = "Fire"
        return ack

    fake.send = changed
    await roster.sync_specs()
    assert [c.guid for c in fake.sent if isinstance(c, SetSpec)] == [101]


@_run_async
async def test_spec_limit_is_durable_across_roster_reopen(roster_data):
    fake, roster = await offline(roster_data)
    for guid in range(110, 125):
        fake.members[guid] = member(guid, f"M{guid}", 8, 1)
        fake.personas[guid] = persona(guid, f"M{guid}", main_spec="Frost", raid_role="ranged")
    fake.members[101] = replace(fake.members[101], online=False)
    await roster.sync_specs()
    assert len(fake.sent) == 10
    reopened = Roster(fake, GuildConfig(), roster.armory, premade=PREMADE)
    await advance(fake, 600)
    await reopened.sync_specs()
    assert len(fake.sent) == 10


@_run_async
async def test_theorycrafters_geared_once_and_disabled_means_no_send(roster_data):
    fake, roster = await offline(roster_data)
    fake.personas[102].game.thinks_as = "theorycrafter"
    await roster.maybe_gear_theorycrafters()
    command = fake.sent[-1]
    assert command.guids == (102,) and command.ilvl == 251
    await roster.on_job(progress(command, guid=102, avg_ilvl=244))
    assert not flags(fake.conn, "tc_geared")
    await roster.on_job(progress(command, guid=102, avg_ilvl=245))
    assert flags(fake.conn, "tc_geared") == {102}
    await roster.on_job(job("gear_up", "done", id=command.id, guids=(102,)))
    await advance(fake, 3600)
    await roster.maybe_gear_theorycrafters()
    assert len(fake.sent) == 1
    disabled = Roster(fake, replace(GuildConfig(), theorycrafter_ilvl=0), roster.armory, premade=PREMADE)
    await advance(fake, 600)
    await disabled.maybe_gear_theorycrafters()
    assert len(fake.sent) == 1


def friends_setup(roster_data, *, actual=None, cfg=None):
    fake, armory = roster_data
    actual = set() if actual is None else actual
    for guid in fake.members:
        fake.rels[guid, 1] = Relationship(guid, 1, affinity=100, trust=100, familiarity=100)
    roster = Roster(fake, cfg or GuildConfig(), armory, premade=PREMADE, friends_of=lambda: set(actual))
    return fake, roster, actual


@_run_async
async def test_friends_wait_120_seconds_and_only_flag_ok_ack(roster_data):
    fake, roster, _ = friends_setup(roster_data)
    fake.acks["add_friend"].extend([(False, "friends_full"), (True, ""), (False, "enemy_faction")])
    await roster.sync_friends()
    assert not fake.sent
    await advance(fake, 120)
    await roster.sync_friends()
    assert flags(fake.conn, "friend") == {102}
    assert fake.conn.execute("SELECT value FROM guild_flag WHERE kind='friend' AND guid=102").fetchone()[0] == 1
    assert all(isinstance(c, AddFriend) and c.note == "" for c in fake.sent)
    await roster.sync_friends()
    assert len(fake.sent) == 3


@_run_async
async def test_unowned_existing_friends_are_never_adopted_or_removed(roster_data):
    fake, roster, _ = friends_setup(roster_data, actual={101, 999})
    fake.rels[101, 1] = Relationship(101, 1, affinity=-100)
    await advance(fake, 120)
    await roster.sync_friends()
    assert flags(fake.conn, "friend") == {102, 103}
    assert {c.guid for c in fake.sent} == {102, 103}


@_run_async
async def test_unowned_friends_reserve_capacity(roster_data):
    fake, roster, _ = friends_setup(roster_data, actual={999, 998}, cfg=replace(GuildConfig(), friends_cap=3))
    await advance(fake, 120)
    await roster.sync_friends()
    assert flags(fake.conn, "friend") == {101}


@_run_async
async def test_missing_snapshot_fails_closed_for_new_additions(roster_data):
    fake, roster, _ = friends_setup(roster_data)
    roster.friends_of = None
    await advance(fake, 120)
    await roster.sync_friends()
    assert not fake.sent and not flags(fake.conn, "friend")


@_run_async
async def test_remove_friend_only_managed_and_clear_only_ok(roster_data):
    fake, roster, _ = friends_setup(roster_data, actual={101, 102, 999})
    for guid in (101, 102):
        set_flag(fake.conn, "friend", guid, fake.clock.now(), fake.human_guid)
        fake.rels[guid, 1] = Relationship(guid, 1, affinity=-100)
    fake.acks["remove_friend"].extend([(False, "human_offline"), (True, "")])
    await advance(fake, 120)
    await roster.sync_friends()
    assert 101 in flags(fake.conn, "friend") and 102 not in flags(fake.conn, "friend")
    assert {c.guid for c in fake.sent if isinstance(c, RemoveFriend)} == {101, 102}


@_run_async
async def test_unknown_add_ack_never_claims_or_retries_friend(roster_data):
    fake, roster, actual = friends_setup(roster_data)
    fake.members = {101: fake.members[101]}
    original = fake.send

    async def lost(command):
        await original(command)
        actual.add(command.guid)
        raise TimeoutError

    fake.send = lost
    await advance(fake, 120)
    await roster.sync_friends()
    assert not flags(fake.conn, "friend")
    reopened = Roster(fake, GuildConfig(), roster.armory, premade=PREMADE, friends_of=lambda: actual)
    await advance(fake, 1800)
    await reopened.sync_friends()
    assert len(fake.sent) == 1 and not flags(fake.conn, "friend")


@_run_async
async def test_logout_during_snapshot_stops_friend_commands(roster_data):
    fake, roster, _ = friends_setup(roster_data)

    async def snapshot():
        fake.online = False
        await roster.on_logout(Logout(0, 1, True))
        return set()

    roster.friends_of = snapshot
    await advance(fake, 120)
    await roster.sync_friends()
    assert not fake.sent


@_run_async
async def test_ack_and_friend_flag_commit_together_or_remain_uncertain(roster_data):
    fake, roster, _ = friends_setup(roster_data)
    fake.members = {101: fake.members[101]}
    with fake.conn:
        fake.conn.execute("CREATE TRIGGER reject_friend BEFORE INSERT ON guild_flag "
                          "WHEN NEW.kind='friend' BEGIN SELECT RAISE(ABORT,'test'); END")
    await advance(fake, 120)
    with pytest.raises(sqlite3.IntegrityError):
        await roster.sync_friends()
    assert roster._get("friend-add:101")["state"] == "sending"
    assert not flags(fake.conn, "friend")
    with fake.conn:
        fake.conn.execute("DROP TRIGGER reject_friend")
    await advance(fake, 1800)
    await roster.sync_friends()
    assert len(fake.sent) == 1


@_run_async
async def test_human_and_rank_zero_never_mutated_even_with_bad_legacy_flags(roster_data):
    fake, roster, _ = friends_setup(roster_data)
    fake.members[1] = replace(fake.members[101], guid=1, rank=1)
    fake.members[101] = replace(fake.members[101], rank=0)
    fake.personas[1] = persona(1, "Testmage", main_spec="Holy", raid_role="healer")
    for guid in (1, 101):
        set_flag(fake.conn, "friend", guid, fake.clock.now())
    await roster.sync_ranks()
    await advance(fake, 120)
    await roster.sync_friends()
    fake.online = False
    await roster.on_logout(Logout(0, 1, True))
    await advance(fake, 600)
    await roster.sync_specs()
    await roster.maybe_gear_up()
    assert all(c.guid not in {1, 101} for c in fake.sent if hasattr(c, "guid"))
    assert all(not set(c.guids) & {1, 101} for c in fake.sent if isinstance(c, GearUp))


@_run_async
async def test_changed_human_never_inherits_old_owners_friend_flags(roster_data):
    fake, roster, actual = friends_setup(roster_data)
    fake.members = {101: fake.members[101]}
    await advance(fake, 120)
    await roster.sync_friends()
    assert flags(fake.conn, "friend") == {101}
    assert fake.conn.execute("SELECT value FROM guild_flag WHERE kind='friend'").fetchone()[0] == 1
    fake.sent.clear()
    fake.human_guid = 2
    fake.ident = replace(fake.ident, guid=2)
    fake.rels[101, 2] = Relationship(101, 2, affinity=-100)
    actual.add(101)  # The new human already had this friend independently.
    changed = Roster(fake, GuildConfig(), roster.armory, premade=PREMADE, friends_of=lambda: actual)
    await advance(fake, 120)
    await changed.sync_friends()
    assert not fake.sent
    assert fake.conn.execute("SELECT value FROM guild_flag WHERE kind='friend'").fetchone()[0] == 1


@pytest.mark.parametrize("owner", [None, 0, -1, 1.5, "unknown"])
@_run_async
async def test_unattributed_legacy_friend_flags_are_protected(roster_data, owner):
    fake, roster, _ = friends_setup(roster_data, actual={101})
    fake.members = {101: fake.members[101]}
    fake.rels[101, 1] = Relationship(101, 1, affinity=-100)
    set_flag(fake.conn, "friend", 101, fake.clock.now(), owner)
    await advance(fake, 120)
    await roster.sync_friends()
    assert not fake.sent
    assert flags(fake.conn, "friend") == {101}
