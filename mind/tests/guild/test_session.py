import asyncio
from functools import wraps
from dataclasses import replace
import json
import sqlite3

import pytest

from leeroy_mind.guild.config import GuildConfig
from leeroy_mind.guild.dkp import LedgerEntry
from leeroy_mind.guild.lines import INSTRUCTIONS, TEMPLATES, instruction, template
from leeroy_mind.guild.loot import LootMaster, item_price, price_category, wants_item
from leeroy_mind.guild.session import MvpVote, RaidPlan, RaidSession
from leeroy_mind.guild.store import add_ledger, balances
from leeroy_mind.guild.upgrades import ItemInfo
from leeroy_mind.protocol import Ack, Boss, Chat, LootDrop, LootItem
from leeroy_mind.talk.turns import TurnKind
from .fakes import FakeArmory, member, persona


def _run_async(test):
    """Use the repository's asyncio.run convention with pytest fixture discovery."""
    @wraps(test)
    def run(*args, **kwargs):
        return asyncio.run(test(*args, **kwargs))
    return run


@pytest.fixture
def setup(fake):
    for guid, cls, spec in ((101, 1, 2), (102, 5, 0), (103, 8, 1), (104, 8, 1),
                            (105, 1, 0), (106, 6, 1), (107, 3, 1), (108, 8, 1)):
        fake.members[guid] = member(guid, f"M{guid}", cls, spec, map=631)
        fake.personas[guid] = persona(guid, f"M{guid}", guild_role="officer" if guid in {101, 104} else "raider",
                                        officer_title="Raid Lead" if guid == 101 else "DKP" if guid == 104 else "")
    blade = ItemInfo(50001, "Testblade", 4, 264, 17, 2, 8, -1, frozenset({4}))
    armory = FakeArmory({50001: blade}, {105: {15: 251}, 106: {15: 245}})
    plan = RaidPlan("icc", 10, False, False, 101, 101, 104, list(range(101, 108)), [108], True)
    return fake, GuildConfig(), armory, plan


async def opened(setup, *, offline=False, bind=True):
    ports, cfg, armory, plan = setup
    if offline:
        plan = replace(plan, offline=True, human_in_raid=False)
    session = await RaidSession.open(ports, cfg, armory, plan)
    if bind:
        assert session.bind_instance(77)
    return session


def set_balance(ports, guid, amount):
    current = balances(ports.conn).get(guid, 0)
    add_ledger(ports.conn, [LedgerEntry(ports.clock.now(), guid, amount - current, "adjust")])


def items_charged(ports):
    return [tuple(row) for row in ports.conn.execute("SELECT guid,delta,item_id FROM dkp_ledger WHERE reason='item' ORDER BY id")]


def drop(*, instance=77, map_id=631, ts=0, index=0, item=50001):
    return LootDrop(ts, map_id, instance, 36597, (LootItem(item, "Testblade", 4, index),))


async def advance(ports, seconds):
    await ports.clock.advance(seconds, step=max(seconds, 1))


@_run_async
async def test_open_records_attendance_and_dkp_once_on_active_reopen(setup):
    ports, cfg, armory, plan = setup
    session = await opened(setup)
    assert ports.leads[-1] == 101
    attendance = dict(ports.conn.execute("SELECT guid,status FROM guild_attendance WHERE raid_id=?", (session.raid_id,)))
    assert attendance == {**{g: "raided" for g in [1, *plan.members]}, 108: "benched"}
    assert balances(ports.conn) == {g: 5.0 for g in [1, *plan.members, 108]}
    again = await RaidSession.open(ports, cfg, armory, plan)
    assert again.raid_id == session.raid_id and again.ctx.instance_id == 77
    assert ports.conn.execute("SELECT COUNT(*) FROM dkp_ledger").fetchone()[0] == 9
    assert len(ports.memories) == 1


@_run_async
async def test_open_is_atomic_when_attendance_ledger_fails(setup):
    ports, cfg, armory, plan = setup
    ports.conn.execute("CREATE TRIGGER fail_attendance BEFORE INSERT ON dkp_ledger WHEN NEW.reason='attendance' BEGIN SELECT RAISE(ABORT,'test'); END")
    with pytest.raises(sqlite3.IntegrityError):
        await RaidSession.open(ports, cfg, armory, plan)
    assert ports.conn.execute("SELECT COUNT(*) FROM guild_raid").fetchone()[0] == 0
    assert ports.conn.execute("SELECT COUNT(*) FROM guild_attendance").fetchone()[0] == 0
    assert ports.leads == []


@_run_async
async def test_bench_memory_by_streak(setup):
    ports, cfg, armory, plan = setup
    first = await opened(setup)
    memory = ports.memories[-1]
    assert (memory.owner, memory.text, memory.importance, memory.valence) == (108, "I got benched for ICC 10.", 3, -0.2)
    assert memory.raid_id == first.raid_id and memory.evidence == () and memory.scope == "open"
    await first.close("called")
    await advance(ports, 1)
    await RaidSession.open(ports, cfg, armory, plan)
    memory = ports.memories[-1]
    assert memory.text == "I got benched for ICC 10 again, 2 raids in a row."
    assert memory.importance == 5 and memory.valence == -0.4
    assert any(channel == "officer" and "M108" in text for _, channel, text, _ in ports.proposed)


@_run_async
async def test_kill_awards_only_actual_planned_participants_once(setup):
    ports, _, _, _ = setup
    session = await opened(setup)
    event = Boss(0, 631, 77, 0, "Testboss", "killed", (101, 102, 1, 999, 101))
    await session.on_boss(event)
    await session.on_boss(replace(event, ts=int(ports.clock.now() * 1000)))
    assert session.kills == 1
    assert dict(ports.conn.execute("SELECT guid,delta FROM dkp_ledger WHERE reason='kill'")) == {101: 10.0, 102: 10.0, 1: 10.0}
    assert ports.said[-1] == (104, "raid", "+10 dkp to 3 for Testboss")


@_run_async
async def test_kill_receipt_and_count_roll_back_with_ledger(setup):
    ports, _, _, _ = setup
    session = await opened(setup)
    event = Boss(0, 631, 77, 0, "Testboss", "killed", (101,))
    ports.conn.execute("CREATE TRIGGER fail_kill BEFORE INSERT ON dkp_ledger WHEN NEW.reason='kill' BEGIN SELECT RAISE(ABORT,'test'); END")
    with pytest.raises(sqlite3.IntegrityError):
        await session.on_boss(event)
    assert session.kills == 0
    ports.conn.execute("DROP TRIGGER fail_kill")
    await session.on_boss(event)
    assert session.kills == 1


@_run_async
async def test_wipes_deduplicate_replay_but_distinct_attempts_count(setup):
    ports, _, _, _ = setup
    session = await opened(setup)
    event = Boss(0, 631, 77, 0, "Testboss", "wiped", (101,))
    await session.on_boss(event)
    await session.on_boss(event)
    await session.on_boss(replace(event, ts=int(ports.clock.now() * 1000)))
    assert session.wipes == 2
    assert ports.conn.execute("SELECT COUNT(*) FROM dkp_ledger WHERE reason='kill'").fetchone()[0] == 0


@_run_async
async def test_instance_only_binds_from_validated_member_boss_or_explicit_input(setup):
    ports, _, armory, _ = setup
    session = await opened(setup, bind=False)
    await session.on_loot_drop(drop())
    assert session.ctx.instance_id is None and ports.said == [] and armory.calls == []
    for event in (Boss(0, 649, 77, 0, "Foreign", "killed", (101,)),
                  Boss(0, 631, 77, 0, "Foreign", "killed", (999,)),
                  Boss(0, 631, 0, 0, "Foreign", "killed", (101,))):
        await session.on_boss(event)
    assert session.ctx.instance_id is None and session.kills == 0
    await session.on_boss(Boss(0, 631, 77, 0, "", "engaged", (101,)))
    assert session.ctx.instance_id == 77 and not session.bind_instance(78)
    await session.on_loot_drop(drop(instance=78))
    assert not armory.calls
    await session.on_loot_drop(drop())
    assert ports.said[-1] == (104, "raid", "Testblade: bids to me")


@_run_async
async def test_foreign_and_stale_events_do_not_award(setup):
    ports, _, _, _ = setup
    session = await opened(setup)
    for event in (Boss(0, 631, 78, 0, "Foreign", "killed", (101,)),
                  Boss(1, 631, 77, 0, "Stale", "killed", (101,))):
        await session.on_boss(event)
    await session.on_loot_drop(drop(ts=1))
    assert session.kills == 0 and ports.said == []


@_run_async
async def test_loot_flow_awards_and_charges_after_matching_ack(setup):
    ports, _, _, _ = setup
    session = await opened(setup)
    set_balance(ports, 105, 100)
    set_balance(ports, 106, 40)
    await session.on_loot_drop(drop())
    await advance(ports, 19)
    await session.tick()
    assert ports.sent == [] and items_charged(ports) == []
    await advance(ports, 1)
    await session.tick()
    command = ports.sent[-1]
    assert (command.map, command.instance_id, command.source_entry, command.index, command.item_id, command.to_guid) == (631, 77, 36597, 0, 50001, 105)
    assert items_charged(ports) == [(105, -80.0, 50001)]
    assert ports.said[-1] == (104, "raid", "Testblade -> M105, 80 dkp")
    await session.on_loot_drop(drop())
    await session.tick()
    assert len(ports.sent) == 1 and len(items_charged(ports)) == 1


@_run_async
async def test_failed_give_charges_nothing_and_no_loser_notice(setup):
    ports, _, _, _ = setup
    session = await opened(setup)
    set_balance(ports, 105, 100)
    ports.acks["give_loot"].append((False, "no_item"))
    await session.on_loot_drop(drop())
    await advance(ports, 20)
    await session.tick()
    assert items_charged(ports) == [] and ports.lost == []
    assert ports.said[-1] == (104, "raid", "Testblade: couldn't hand that out (no_item)")
    await session.on_loot_drop(drop())
    await session.tick()
    assert len(ports.sent) == 1


@_run_async
async def test_success_for_wrong_command_id_never_charges(setup):
    ports, _, _, _ = setup
    session = await opened(setup)
    async def wrong(command):
        ports.sent.append(command)
        return Ack(0, "another-command", True, "")
    ports.send = wrong
    await session.on_loot_drop(drop())
    await advance(ports, 20)
    await session.tick()
    assert items_charged(ports) == [] and "wrong_ack" in ports.said[-1][2]


@_run_async
async def test_human_claim_wins_only_in_correct_chat_context(setup):
    ports, _, _, _ = setup
    session = await opened(setup)
    await session.on_loot_drop(drop())
    await session.on_chat(Chat(0, "whisper", 1, "Testmage", 104, "mine", 631, 4812))
    await advance(ports, 20)
    await session.tick()
    assert ports.sent[-1].to_guid == 1 and items_charged(ports) == [(1, -80.0, 50001)]


@_run_async
@pytest.mark.parametrize(("guid", "channel", "to_guid", "text", "via_bridge"), [
    (105, "raid", 0, "mine", False), (1, "whisper", 102, "mine", False),
    (1, "say", 0, "mine", False), (1, "raid", 0, "mine was better", False),
    (1, "raid", 0, "mine", True),
])
async def test_nonclaims_do_not_override_award(setup, guid, channel, to_guid, text, via_bridge):
    ports, _, _, _ = setup
    session = await opened(setup)
    set_balance(ports, 105, 100)
    await session.on_loot_drop(drop())
    await session.on_chat(Chat(0, channel, guid, "Speaker", to_guid, text, 631, 4812, via_bridge))
    await advance(ports, 20)
    await session.tick()
    assert ports.sent[-1].to_guid == 105


@_run_async
async def test_out_of_map_human_claim_and_changed_loot_context_do_not_issue_give(setup):
    ports, _, _, _ = setup
    session = await opened(setup)
    set_balance(ports, 105, 100)
    await session.on_loot_drop(drop())
    await session.on_chat(Chat(0, "guild", 1, "Testmage", 0, "mine", 1, 1637))
    await advance(ports, 20)
    session.ctx.instance_id = 78
    await session.tick()
    assert ports.sent == [] and items_charged(ports) == []
    session.ctx.instance_id = 77
    await session.tick()
    assert ports.sent[-1].to_guid == 105


@_run_async
async def test_close_loser_gets_one_notification_after_ack(setup):
    ports, _, _, _ = setup
    session = await opened(setup)
    set_balance(ports, 105, 100)
    set_balance(ports, 106, 95)
    await session.on_loot_drop(drop())
    await advance(ports, 20)
    await session.tick()
    await session.tick()
    assert items_charged(ports) == [(105, -96.0, 50001)]
    assert ports.lost == [(106, "Testblade")]
    assert "close one, M106 bid 95" in ports.said[-1][2]


@_run_async
async def test_no_candidates_or_unknown_item_is_not_given_even_if_claimed(setup):
    ports, cfg, armory, plan = setup
    cloth = ItemInfo(50002, "Testrobe", 4, 264, 20, 4, 1, -1, frozenset({5}))
    armory._items[50002] = cloth
    plan = replace(plan, members=[101, 105, 106], loot_guid=101, bench=[])
    session = await RaidSession.open(ports, cfg, armory, plan)
    session.bind_instance(77)
    await session.on_loot_drop(drop(item=50002))
    await advance(ports, 20)
    await session.tick()
    assert ports.sent == [] and ports.said[-1][2] == "Testrobe: no takers"
    await session.on_loot_drop(drop(item=59999, index=1))
    session.loot.on_human_chat("mine")
    await advance(ports, 20)
    await session.tick()
    assert ports.sent == []


@_run_async
async def test_offline_window_is_three_seconds_and_human_cannot_claim(setup):
    ports, _, _, _ = setup
    session = await opened(setup, offline=True)
    set_balance(ports, 105, 100)
    await session.on_loot_drop(drop())
    session.loot.on_human_chat("mine")
    await advance(ports, 2)
    await session.tick()
    assert ports.sent == []
    await advance(ports, 1)
    await session.tick()
    assert ports.sent[-1].to_guid == 105
    await session.on_boss(Boss(0, 631, 77, 0, "Testboss", "killed", (1, 101)))
    assert ports.conn.execute("SELECT COUNT(*) FROM dkp_ledger WHERE guid=1").fetchone()[0] == 0


@_run_async
async def test_due_items_refresh_balances_and_human_claim_targets_oldest(setup):
    ports, _, _, _ = setup
    session = await opened(setup)
    set_balance(ports, 105, 100)
    set_balance(ports, 106, 99)
    await session.on_loot_drop(drop(index=5))
    await session.on_loot_drop(drop(index=0))
    session.loot.on_human_chat("mine")
    session.loot.on_human_chat("mine")
    await advance(ports, 20)
    await session.tick()
    assert [(c.index, c.to_guid) for c in ports.sent] == [(5, 1), (0, 105)]
    await session.on_loot_drop(drop(index=1))
    await advance(ports, 20)
    await session.tick()
    assert ports.sent[-1].to_guid == 106


@_run_async
async def test_concurrent_ticks_never_give_one_item_twice(setup):
    ports, _, _, _ = setup
    session = await opened(setup)
    original = ports.send
    entered, release = asyncio.Event(), asyncio.Event()
    async def delayed(command):
        entered.set()
        await release.wait()
        return await original(command)
    ports.send = delayed
    await session.on_loot_drop(drop())
    await advance(ports, 20)
    first = asyncio.create_task(session.tick())
    await entered.wait()
    second = asyncio.create_task(session.tick())
    await asyncio.sleep(0)
    assert not second.done()
    release.set()
    await asyncio.gather(first, second)
    assert len(ports.sent) == 1 and len(items_charged(ports)) == 1


@_run_async
async def test_cancel_during_remote_give_is_uncertain_and_never_retried_or_charged(setup):
    ports, _, _, _ = setup
    session = await opened(setup)
    entered = asyncio.Event()
    async def interrupted(command):
        ports.sent.append(command)
        entered.set()
        await asyncio.Event().wait()
    ports.send = interrupted
    await session.on_loot_drop(drop())
    await advance(ports, 20)
    task = asyncio.create_task(session.tick())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await session.tick()
    await session.on_loot_drop(drop())
    assert len(ports.sent) == 1 and items_charged(ports) == []


@_run_async
async def test_acknowledged_award_survives_local_ledger_failure_without_resend(setup):
    ports, cfg, armory, _ = setup
    session = await opened(setup)
    await session.on_loot_drop(drop())
    ports.conn.execute("CREATE TRIGGER fail_item BEFORE INSERT ON dkp_ledger WHEN NEW.reason='item' BEGIN SELECT RAISE(ABORT,'test'); END")
    await advance(ports, 20)
    with pytest.raises(sqlite3.IntegrityError):
        await session.tick()
    assert len(ports.sent) == 1 and items_charged(ports) == []
    ports.conn.execute("DROP TRIGGER fail_item")
    restored = LootMaster(ports, cfg, armory, session.ctx)
    await restored.tick()
    await restored.tick()
    assert len(ports.sent) == 1 and len(items_charged(ports)) == 1


@_run_async
async def test_close_cancels_pending_loot_and_is_idempotent(setup):
    ports, _, _, _ = setup
    session = await opened(setup)
    await session.on_loot_drop(drop())
    assert await session.close("cleared") is None
    before = (list(ports.said), list(ports.proposed), len(ports.memories))
    assert await session.close("different outcome") is None
    await advance(ports, 20)
    await session.tick()
    await session.on_boss(Boss(0, 631, 77, 0, "Testboss", "killed", (101,)))
    row = ports.conn.execute("SELECT ended,outcome,kills FROM guild_raid WHERE raid_id=?", (session.raid_id,)).fetchone()
    assert row[0] is not None and row[1:] == ("cleared", 0)
    assert before == (ports.said, ports.proposed, len(ports.memories))
    assert ports.sent == [] and ports.leads[-1] is None
    assert "dkp top 5:" in ports.said[-1][2]


@_run_async
async def test_retry_of_cancelled_old_close_does_not_clear_a_new_raid_lead(setup):
    ports, cfg, armory, plan = setup
    first = await opened(setup)
    original = ports.say
    async def interrupted(*args):
        raise asyncio.CancelledError
    ports.say = interrupted
    with pytest.raises(asyncio.CancelledError):
        await first.close("called")
    ports.say = original
    second = await RaidSession.open(ports, cfg, armory, plan)
    assert second.raid_id != first.raid_id and ports.leads[-1] == 101
    await first.close("called")
    assert ports.leads[-1] == 101
    assert ports.conn.execute("SELECT ended FROM guild_raid WHERE raid_id=?", (second.raid_id,)).fetchone()[0] is None


class FirstThree:
    def sample(self, values, count):
        return list(values)[:count]


def utterance(ports, guid, text, *, channel="guild", dropped=None, when=None, refs=(), to_guid=0):
    with ports.conn:
        row = ports.conn.execute("INSERT INTO utterance(ts,guid,channel,kind,text,memory_ids,dropped,to_guid) VALUES (?,?,?,?,?,?,?,?)",
                                 (int(ports.clock.now() if when is None else when), guid, channel, "aside", text,
                                  json.dumps(refs), dropped, to_guid))
        return row.lastrowid


def voting(ports):
    vote = MvpVote(ports, FirstThree(), lead=101, members=[1, 101, 102, 103, 104, 105, 106], night="2026-10-29")
    vote.start(ports.clock.now())
    assert vote.voters == (102, 103, 104)
    return vote


@_run_async
async def test_mvp_tallies_one_public_sent_vote_per_sampled_voter_and_links_real_event(setup):
    ports, _, _, _ = setup
    vote = voting(ports)
    assert len(ports.proposed) == 4 and all(p[1] == "guild" and p[3] is TurnKind.ASIDE for p in ports.proposed)
    utterance(ports, 102, "M105 carried tonight")
    utterance(ports, 103, "m105 for sure")
    utterance(ports, 104, "M106 tbh")
    utterance(ports, 104, "M105 instead")
    assert not await vote.tick(ports.clock.now() + 89)
    assert await vote.tick(ports.clock.now() + 90)
    assert ports.said[-1] == (101, "guild", "mvp tonight: M105 (2 votes)")
    assert [(m.owner, m.key, m.importance) for m in ports.memories] == [(105, "mvp:2026-10-29", 6), (0, "mvp:2026-10-29", 5)]
    event_id = int(ports.memories[0].evidence[0].split(":")[1])
    row = ports.conn.execute("SELECT type,payload FROM game_event WHERE id=?", (event_id,)).fetchone()
    assert row[0] == "guild_mvp" and json.loads(row[1])["votes"] == 2
    assert "carried" not in row[1] and "for sure" not in row[1]
    before = (len(ports.said), len(ports.memories))
    assert await vote.tick(ports.clock.now() + 100)
    vote.start(ports.clock.now())
    assert before == (len(ports.said), len(ports.memories))


@_run_async
async def test_mvp_ignores_unsent_private_old_late_self_human_and_unsampled_votes(setup):
    ports, _, _, _ = setup
    utterance(ports, 102, "M105 before the call")
    vote = voting(ports)
    utterance(ports, 102, "M105", dropped="cancelled")
    utterance(ports, 102, "M105", channel="whisper", to_guid=101)
    utterance(ports, 103, "M103 and Testmage")
    utterance(ports, 1, "M105")
    utterance(ports, 106, "M105")
    utterance(ports, 104, "M105", when=ports.clock.now() + 91)
    assert await vote.tick(ports.clock.now() + 100)
    assert ports.said == [] and ports.memories == []


@_run_async
async def test_mvp_first_name_is_textual_and_tie_goes_to_earliest_vote(setup):
    ports, _, _, _ = setup
    vote = voting(ports)
    utterance(ports, 102, "M106 then M105")
    utterance(ports, 103, "M105")
    await vote.tick(ports.clock.now() + 90)
    assert ports.said[-1][2] == "mvp tonight: M106 (1 votes)"


@_run_async
async def test_mvp_prefixes_do_not_vote_for_similarly_named_members(setup):
    ports, _, _, _ = setup
    vote = voting(ports)
    utterance(ports, 102, "M1050")
    await vote.tick(ports.clock.now() + 90)
    assert not ports.said and not ports.memories


@_run_async
async def test_mvp_rejects_missing_recalled_evidence_and_rechecks_rows_after_await(setup):
    ports, _, _, _ = setup
    vote = voting(ports)
    utterance(ports, 102, "M105", refs=[999999])
    valid = utterance(ports, 103, "M105")
    original = ports.say
    async def changed(guid, channel, text):
        result = await original(guid, channel, text)
        with ports.conn:
            ports.conn.execute("UPDATE utterance SET channel='whisper' WHERE id=?", (valid,))
        return result
    ports.say = changed
    await vote.tick(ports.clock.now() + 90)
    assert ports.said[-1][2] == "mvp tonight: M105 (1 votes)"
    assert ports.memories == []


@_run_async
async def test_close_starts_mvp_only_after_kill_and_human_never_speaks(setup):
    ports, cfg, armory, plan = setup
    session = await RaidSession.open(ports, cfg, armory, replace(plan, lead_guid=1))
    await session.on_boss(Boss(0, 631, 77, 0, "Testboss", "killed", (101, 1)))
    vote = await session.close("called")
    assert isinstance(vote, MvpVote)
    assert await session.close("called") is vote
    assert all(guid != 1 for guid, *_ in ports.proposed)
    assert all(guid != 1 for guid, *_ in ports.said)


@_run_async
async def test_distinct_raids_same_night_get_distinct_votes_without_replaying_first(setup):
    ports, cfg, armory, plan = setup
    first = await opened(setup)
    await first.on_boss(Boss(0, 631, 77, 0, "Testboss", "killed", (101,)))
    first_vote = await first.close("called")
    await first_vote.tick(ports.clock.now() + 90)
    await advance(ports, 120)
    second = await RaidSession.open(ports, cfg, armory, plan)
    proposed = len(ports.proposed)
    await second.on_boss(Boss(0, 631, 78, 0, "Testboss", "killed", (101,)))
    second_vote = await second.close("cleared")
    assert first_vote.night == second_vote.night
    assert first_vote._key != second_vote._key
    assert len(ports.proposed) == proposed + 5  # wrapup plus call and three voters
    assert not await second_vote.tick(ports.clock.now())


def test_every_generic_line_formats_with_documented_facts():
    facts = dict(human="Testy", suggest="ICC", tanks=2, healers=3, damage=4, raid="ICC", reason="bad_size",
                 size=10, short="healer:1", names="M101, M102", kills=4, wipes=2, outcome="called",
                 feast="Fish Feast", amount=10, count=5, boss="Testboss", item="Testblade", winner="M105",
                 paid=80, runner_up="M106", runner_bid=75, error="no_item", top="M105 100.0", pct=10,
                 seconds=5, name="M105", votes=2)
    assert all(instruction(key, **facts) for key in INSTRUCTIONS)
    assert all(template(key, **facts) for key in TEMPLATES)


@pytest.mark.parametrize(("inventory", "item_class", "mask", "category"), [
    (17, 2, -1, "weapon_2h"), (13, 2, -1, "weapon_1h"), (15, 2, -1, "weapon_1h"),
    (21, 2, -1, "weapon_1h"), (22, 2, -1, "weapon_1h"), (25, 2, -1, "weapon_1h"),
    (26, 2, -1, "weapon_1h"), (12, 4, -1, "trinket"), (0, 15, 1, "token"),
    (0, 15, -1, "other"), (1, 4, -1, "other"),
])
def test_price_categories(inventory, item_class, mask, category):
    item = ItemInfo(50001, "Test", 4, 264, inventory, item_class, 0, mask, frozenset())
    assert price_category(item) == category
    assert item_price(item, GuildConfig().dkp.prices) == GuildConfig().dkp.prices[category]


def test_wants_item_is_case_insensitive_substring_either_way_without_empty_matches():
    assert wants_item(["testblade"], "Testblade of Tests")
    assert wants_item(["TESTBLADE OF TESTS"], "Testblade")
    assert not wants_item(["", "   "], "Testblade")
    assert not wants_item(["Testblade"], "")


@pytest.mark.parametrize("attend", [True, False])
@_run_async
async def test_due_decay_runs_with_attendance_at_open_or_at_attend(setup, attend):
    fake, cfg, armory, plan = setup
    with fake.conn:
        fake.conn.execute("INSERT INTO meta(key,value) VALUES ('dkp_last_decay','2026-10-01T04:00:00')")
    session = await RaidSession.open(fake, cfg, armory, plan, attend=attend)
    opened = [text for _, _, text in fake.said if text.startswith("dkp updated")]
    assert len(opened) == int(attend)
    await session.attend()
    await session.attend()
    assert len([text for _, _, text in fake.said if text.startswith("dkp updated")]) == 1
    assert fake.conn.execute("SELECT COUNT(*) FROM guild_attendance WHERE raid_id=?",
                             (session.raid_id,)).fetchone()[0] == len(plan.members) + 1 + len(plan.bench)
