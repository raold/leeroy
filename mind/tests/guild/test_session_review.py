"""Independent retry, shared-session serialization and public-evidence regressions."""

import asyncio
from functools import wraps
import json
import sqlite3

import pytest

from conftest import make_mem
from leeroy_mind.guild.config import GuildConfig
from leeroy_mind.guild.dkp import LedgerEntry
from leeroy_mind.guild.session import MvpVote, RaidPlan, RaidSession
from leeroy_mind.guild.store import add_ledger, balances
from leeroy_mind.guild.upgrades import ItemInfo
from leeroy_mind.protocol import Ack, LootDrop, LootItem
from leeroy_mind.storage.store import Store
from .fakes import FakeArmory, member, persona


def _run_async(test):
    """Use the repository's asyncio.run convention with pytest fixture discovery."""
    @wraps(test)
    def run(*args, **kwargs):
        return asyncio.run(test(*args, **kwargs))
    return run


@pytest.fixture
def review_setup(fake):
    for guid, cls, spec in ((101, 1, 2), (104, 8, 1), (105, 1, 0), (106, 6, 1)):
        fake.members[guid] = member(guid, f"M{guid}", cls, spec, map=631)
        fake.personas[guid] = persona(guid, f"M{guid}", raid_role="tank" if guid == 101 else "melee")
    blade = ItemInfo(50001, "Synthetic blade", 4, 264, 17, 2, 8, -1, frozenset({4}))
    armory = FakeArmory({blade.item_id: blade}, {101: {15: 277}, 105: {15: 251}, 106: {15: 277}})
    plan = RaidPlan("icc", 10, False, False, 101, 101, 104, [101, 104, 105, 106], [], True)
    return fake, GuildConfig(), armory, plan


async def _open(setup):
    ports, cfg, armory, plan = setup
    session = await RaidSession.open(ports, cfg, armory, plan)
    assert session.bind_instance(77)
    current = balances(ports.conn)
    add_ledger(ports.conn, [LedgerEntry(ports.clock.now(), guid,
        (100 if guid == 105 else 0) - amount, "adjust") for guid, amount in current.items()])
    return session


def _drop(*indices):
    return LootDrop(0, 631, 77, 36597,
                    tuple(LootItem(50001, "Synthetic blade", 4, index) for index in indices))


async def _due(ports):
    await ports.clock.advance(20, step=20)


def _charges(ports):
    return [tuple(row) for row in ports.conn.execute(
        "SELECT guid,delta FROM dkp_ledger WHERE reason='item' ORDER BY id")]


@_run_async
async def test_review_close_reconciles_acknowledged_charge_and_remains_retryable(review_setup):
    ports, _, _, _ = review_setup
    session = await _open(review_setup)
    await session.on_loot_drop(_drop(0))
    with ports.conn:
        ports.conn.execute("CREATE TRIGGER review_fail_charge BEFORE INSERT ON dkp_ledger "
                           "WHEN NEW.reason='item' BEGIN SELECT RAISE(ABORT,'review charge fault'); END")
    await _due(ports)
    with pytest.raises(sqlite3.IntegrityError, match="review charge fault"):
        await session.tick()
    assert len(ports.sent) == 1 and _charges(ports) == []

    # Closing must not strand an already successful remote give behind ended=NOT NULL.
    with pytest.raises(sqlite3.IntegrityError, match="review charge fault"):
        await session.close("called")
    assert ports.conn.execute("SELECT ended FROM guild_raid WHERE raid_id=?",
                              (session.raid_id,)).fetchone()[0] is None
    with ports.conn:
        ports.conn.execute("DROP TRIGGER review_fail_charge")
    assert await session.close("called") is None
    assert _charges(ports) == [(105, -80.0)] and len(ports.sent) == 1
    assert await session.close("called") is None
    await session.tick()
    assert _charges(ports) == [(105, -80.0)] and len(ports.sent) == 1


@_run_async
async def test_review_active_reopen_cannot_spend_one_balance_on_two_inflight_awards(review_setup):
    ports, cfg, armory, plan = review_setup
    first = await _open(review_setup)
    reopened = await RaidSession.open(ports, cfg, armory, plan)
    assert reopened.raid_id == first.raid_id
    await first.on_loot_drop(_drop(0, 1))
    await _due(ports)
    entered, release = asyncio.Event(), asyncio.Event()

    async def send(command):
        ports.sent.append(command)
        if len(ports.sent) == 1:
            entered.set()
            await release.wait()
        return Ack(0, command.id, True, "")

    ports.send = send
    task = asyncio.create_task(first.tick())
    other = None
    try:
        await asyncio.wait_for(entered.wait(), 2)
        other = asyncio.create_task(reopened.tick())
        for _ in range(3):
            await asyncio.sleep(0)
        assert len(ports.sent) == 1, "reopened wrapper bypassed the active raid's award lock"
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(task, *([other] if other is not None else [])), 2)
    assert len(ports.sent) == 2
    assert _charges(ports) == [(105, -80.0), (105, -20.0)]
    assert balances(ports.conn)[105] == 0


@_run_async
async def test_review_close_waits_for_inflight_matching_ack_and_charges_before_ending(review_setup):
    ports, _, _, _ = review_setup
    session = await _open(review_setup)
    await session.on_loot_drop(_drop(0))
    await _due(ports)
    entered, release = asyncio.Event(), asyncio.Event()

    async def send(command):
        ports.sent.append(command)
        entered.set()
        await release.wait()
        return Ack(0, command.id, True, "")

    ports.send = send
    tick = asyncio.create_task(session.tick())
    close = None
    try:
        await asyncio.wait_for(entered.wait(), 2)
        close = asyncio.create_task(session.close("called"))
        await asyncio.sleep(0)
        assert not close.done()
        assert ports.conn.execute("SELECT ended FROM guild_raid WHERE raid_id=?",
                                  (session.raid_id,)).fetchone()[0] is None
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(tick, *([close] if close is not None else [])), 2)
    assert _charges(ports) == [(105, -80.0)] and len(ports.sent) == 1
    assert ports.conn.execute("SELECT ended FROM guild_raid WHERE raid_id=?",
                              (session.raid_id,)).fetchone()[0] is not None


class _First:
    def sample(self, values, count):
        return list(values)[:count]


@_run_async
@pytest.mark.parametrize("private_parent", [False, True])
async def test_review_mvp_follows_recalled_memory_ancestry_not_just_wrapper_scope(
        review_setup, private_parent):
    ports, _, _, plan = review_setup
    with ports.conn:
        event = ports.conn.execute("INSERT INTO game_event(ts,type,guid,payload) VALUES (1,'boss',101,'{}')").lastrowid
    store = Store(ports.conn, clock=ports.clock)
    parent = store.insert_memory(make_mem(owner=104, text="Synthetic source.", evidence=(f"event:{event}",),
        scope="private" if private_parent else "open", scope_with=1 if private_parent else 0))
    wrapper = store.insert_memory(make_mem(owner=104, text="Synthetic recalled source.",
                                           evidence=(f"memory:{parent}",), scope="open"))
    vote = MvpVote(ports, _First(), lead=101, members=plan.members, night="2026-10-29")
    vote.start(ports.clock.now())
    assert 104 in vote.voters
    with ports.conn:
        ports.conn.execute("INSERT INTO utterance(ts,guid,channel,kind,text,memory_ids,to_guid) "
                           "VALUES (?,104,'guild','aside','M105 carried tonight',?,0)",
                           (ports.clock.now(), json.dumps([wrapper])))
    assert await vote.tick(ports.clock.now() + 90)
    if private_parent:
        assert ports.said == [] and ports.memories == []
        assert ports.conn.execute("SELECT count(*) FROM game_event WHERE type='guild_mvp'").fetchone()[0] == 0
    else:
        assert ports.said[-1] == (101, "guild", "mvp tonight: M105 (1 votes)")
        assert [memory.owner for memory in ports.memories] == [105, 0]
        assert all(f"memory:{wrapper}" in memory.evidence for memory in ports.memories)
