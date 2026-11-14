"""The loot system under the guild's loot rule (decisions.loot_rules): the rule's price, its award,
the council's zero row, his claim in every mode and the member who lost to him. Without the
decisions port everything is today's DKP, byte for byte."""
import asyncio
from functools import wraps
import json

import pytest

from leeroy_mind.decisions.loot_rules import MODES, LootRule
from leeroy_mind.guild.loot import _get
from leeroy_mind.protocol import Chat
from .test_session import advance, drop, items_charged, opened, set_balance, setup  # noqa: F401  (the rig)


def _run_async(test):
    @wraps(test)
    def run(*args, **kwargs):
        return asyncio.run(test(*args, **kwargs))
    return run


def ruled(ports, rule):
    calls = []
    ports.loot_rule = lambda raid_id: calls.append(raid_id) or rule
    return calls


def records(ports, raid_id):
    return [json.loads(v) for _, v in ports.conn.execute("SELECT key,value FROM meta WHERE key GLOB ?",
                                                         (f"guild:loot:{raid_id}:*",))]


def notes(ports):
    return [r[0] for r in ports.conn.execute("SELECT note FROM dkp_ledger WHERE reason='item' ORDER BY id")]


@_run_async
async def test_without_the_decisions_port_loot_is_todays(setup):
    ports, _, _, _ = setup
    session = await opened(setup)
    set_balance(ports, 105, 100)
    set_balance(ports, 106, 40)
    await session.on_loot_drop(drop())
    await advance(ports, 20)
    await session.tick()
    assert items_charged(ports) == [(105, -80.0, 50001)]
    [record] = records(ports, session.raid_id)
    assert not {"mode", "slot"} & set(record) and all(set(b) == {"guid", "gain", "wanted"} for b in record["bidders"])


@_run_async
async def test_the_dkp_rule_awards_as_today_and_records_the_slot(setup):
    ports, _, _, _ = setup
    ruled(ports, LootRule())
    session = await opened(setup)
    set_balance(ports, 105, 100)
    set_balance(ports, 106, 40)
    await session.on_loot_drop(drop())
    await advance(ports, 20)
    await session.tick()
    assert items_charged(ports) == [(105, -80.0, 50001)] and notes(ports)[0].startswith("guild:loot:")
    [record] = records(ports, session.raid_id)
    assert (record["mode"], record["slot"], record["price"]) == ("dkp", "weapon_2h", 80)
    roles = {b["guid"]: b["role"] for b in record["bidders"]}
    assert (roles[101], roles[105], roles[106]) == ("tank", "melee", "melee")


@_run_async
async def test_prices_override_dkp_while_in_effect(setup):
    ports, _, _, _ = setup
    ruled(ports, LootRule("dkp", prices={"weapon_2h": 120.0}))
    session = await opened(setup)
    set_balance(ports, 105, 100)
    set_balance(ports, 106, 40)
    await session.on_loot_drop(drop())
    await advance(ports, 20)
    await session.tick()
    assert items_charged(ports) == [(105, -100.0, 50001)]  # the rule's 120 bids up to his whole balance
    assert records(ports, session.raid_id)[0]["price"] == 120.0


@_run_async
async def test_council_charges_nothing_and_records_a_zero_row(setup):
    ports, _, _, _ = setup
    ruled(ports, LootRule("loot_council"))
    session = await opened(setup)
    set_balance(ports, 105, 100)
    set_balance(ports, 106, 40)
    await session.on_loot_drop(drop())
    await advance(ports, 20)
    await session.tick()
    # The tank gains most from the blade (64 item levels): the council gives it to him for nothing
    assert ports.sent[-1].to_guid == 101
    assert items_charged(ports) == [(101, 0.0, 50001)]
    [key] = [k for k, in ports.conn.execute("SELECT key FROM meta WHERE key GLOB 'guild:loot:*'")]
    assert notes(ports) == ["council:" + key]
    assert ports.said[-1] == (104, "raid", "Testblade -> M101, loot council")
    assert _get(ports.conn, key)["state"] == "charged"


@_run_async
async def test_role_first_gives_the_slot_to_its_role(setup):
    ports, _, _, _ = setup
    ruled(ports, LootRule("gear_priority", role_first={"weapon_2h": ("melee",)}))
    session = await opened(setup)
    set_balance(ports, 105, 100)
    set_balance(ports, 106, 40)
    await session.on_loot_drop(drop())
    await advance(ports, 20)
    await session.tick()
    # Melee first, then the bigger upgrade (106's 19 over 105's 13), charged as DKP
    assert items_charged(ports) == [(106, -40.0, 50001)]


@_run_async
@pytest.mark.parametrize(("mode", "paid", "runner"), [("dkp", -80.0, 105), ("loot_council", 0.0, 101),
                                                       ("gear_priority", -80.0, 101)])
async def test_his_claim_always_wins_and_its_loser_remembers(setup, mode, paid, runner):
    assert mode in MODES
    ports, _, _, _ = setup
    ruled(ports, LootRule(mode))
    session = await opened(setup)
    set_balance(ports, 105, 100)
    set_balance(ports, 106, 40)
    await session.on_loot_drop(drop())
    await session.on_chat(Chat(0, "whisper", 1, "Testmage", 104, "mine", 631, 4812))
    await advance(ports, 20)
    await session.tick()
    assert ports.sent[-1].to_guid == 1 and items_charged(ports) == [(1, paid, 50001)]
    assert ports.lost == [(runner, "Testblade")]  # the member who lost it to him, once
    await session.tick()
    assert ports.lost == [(runner, "Testblade")]


@_run_async
async def test_his_claim_with_decisions_off_tells_nobody(setup):
    ports, _, _, _ = setup
    session = await opened(setup)
    set_balance(ports, 105, 100)
    await session.on_loot_drop(drop())
    await session.on_chat(Chat(0, "whisper", 1, "Testmage", 104, "mine", 631, 4812))
    await advance(ports, 20)
    await session.tick()
    assert items_charged(ports) == [(1, -80.0, 50001)] and ports.lost == []


@_run_async
async def test_the_rule_is_read_once_per_raid(setup):
    ports, _, _, _ = setup
    calls = ruled(ports, LootRule("loot_council"))
    session = await opened(setup)
    for index in range(3):
        await session.on_loot_drop(drop(index=index, ts=index))
    await advance(ports, 20)
    await session.tick()
    assert calls == [session.raid_id]


@_run_async
async def test_a_raid_fixes_its_rule_when_it_opens(setup):
    ports, _, _, _ = setup
    fixed = []
    ports.fix_loot_rule = fixed.append
    session = await opened(setup)
    assert fixed == [session.raid_id]
    # A reopened raid is not fixed again: its rule was fixed in the unit that opened it
    await opened(setup)
    assert fixed == [session.raid_id]
