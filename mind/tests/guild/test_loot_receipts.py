"""Currency and lost-ack regressions at the real session/ledger boundary."""
import asyncio
from dataclasses import replace
import json

import pytest

from leeroy_mind.guild.config import GuildConfig, load_guild_config
from leeroy_mind.guild.loot import _get, _put, reconcile_receipts, RETRY_AFTER_S
from leeroy_mind.guild.protocol import GetLootReceipt, GiveLoot
from leeroy_mind.guild.session import RaidSession
from leeroy_mind.guild.upgrades import ItemInfo
from leeroy_mind.protocol import Ack, LootDrop, LootItem, encode_command

from .test_loot_wire import _setup


async def session_for(fake, *, receipts=True):
    armory, plan = _setup(fake)
    fake.loot_receipts = True
    with fake.conn:
        _put(fake.conn, "guild_id", 21)
    session = await RaidSession.open(fake, GuildConfig(loot_receipts=receipts), armory, plan)
    assert session.bind_instance(77)
    return session, armory


async def unknown(fake, clock):
    session, _ = await session_for(fake)
    await session.on_loot_drop(LootDrop(0, 631, 77, 36597, (LootItem(50001, "Testblade", 4, 0),)))
    fake.acks["give_loot"].append((False, "loot_receipt_pending"))
    await clock.advance(20, step=20)
    await session.tick()
    key, record = session.loot._records()[0]
    assert record["state"] == "uncertain"
    assert record["receipt"] and record["intent"]["guid"] == 105
    await clock.advance(RETRY_AFTER_S, step=RETRY_AFTER_S)
    return session, key, record


@pytest.mark.parametrize("item_id", [29434, 40752, 40753, 45624, 47241, 49426])
def test_raid_currency_never_opens_a_dkp_window_even_without_template(fake, clock, item_id):
    async def go():
        session, _ = await session_for(fake, receipts=False)
        said = len(fake.said)
        await session.on_loot_drop(LootDrop(0, 631, 77, 36597, (LootItem(item_id, "Test currency", 4, 0),)))
        await clock.advance(30, step=30)
        await session.tick()
        assert session.loot._records() == [] and len(fake.said) == said
        assert not any(isinstance(cmd, GiveLoot) for cmd in fake.sent)
    asyncio.run(go())


def test_currency_template_bit_filters_custom_currency_but_keeps_the_real_drop(fake):
    async def go():
        session, armory = await session_for(fake, receipts=False)
        armory._items[50002] = ItemInfo(50002, "Test currency", 4, 1, 0, 15, 0, -1,
                                         frozenset(), bag_family=0x2000)
        await session.on_loot_drop(LootDrop(0, 631, 77, 36597,
            (LootItem(50002, "Test currency", 4, 0), LootItem(50001, "Testblade", 4, 1))))
        assert [r["item_id"] for _, r in session.loot._records()] == [50001]
    asyncio.run(go())


def test_lost_ack_is_proved_after_raid_closure_and_charged_once(fake, clock):
    async def go():
        session, key, record = await unknown(fake, clock)
        await session.close("done", raided=False)
        await reconcile_receipts(fake)
        await reconcile_receipts(fake)
        assert _get(fake.conn, key)["state"] == "charged"
        charges = fake.conn.execute("SELECT guid,item_id FROM dkp_ledger WHERE reason='item'").fetchall()
        assert [tuple(row) for row in charges] == [(105, 50001)]
        gives = [cmd for cmd in fake.sent if isinstance(cmd, GiveLoot)]
        proofs = [cmd for cmd in fake.sent if isinstance(cmd, GetLootReceipt)]
        assert len(gives) == len(proofs) == 1
        assert proofs[0].give_id == gives[0].id == record["command_id"]
        assert proofs[0].item_id == gives[0].item_id and proofs[0].to_guid == gives[0].to_guid
    asyncio.run(go())


@pytest.mark.parametrize("code", ["loot_receipt_absent", "loot_receipt_pending", "loot_receipt_unavailable",
                                 "loot_receipt_conflict", "no_item", "no_source"])
def test_unproven_status_neither_charges_nor_reissues_the_give(fake, clock, code):
    async def go():
        session, key, record = await unknown(fake, clock)
        for _ in range(3):
            fake.acks["get_loot_receipt"].append((False, code))
            await reconcile_receipts(fake)
            await session.tick()
            await clock.advance(RETRY_AFTER_S, step=RETRY_AFTER_S)
        assert _get(fake.conn, key)["state"] == "uncertain"
        assert session.loot._held()[105] == record["intent"]["paid"]
        assert len([cmd for cmd in fake.sent if isinstance(cmd, GiveLoot)]) == 1
        assert not fake.conn.execute("SELECT 1 FROM dkp_ledger WHERE reason='item'").fetchone()
        ids = [cmd.id for cmd in fake.sent if isinstance(cmd, GetLootReceipt)]
        assert len(ids) == len(set(ids)) == 3
    asyncio.run(go())


def test_durable_rejection_releases_bid_without_charging(fake, clock):
    async def go():
        session, key, _ = await unknown(fake, clock)
        fake.acks["get_loot_receipt"].append((False, "loot_receipt_rejected"))
        await reconcile_receipts(fake)
        assert _get(fake.conn, key)["state"] == "failed" and session.loot._held() == {}
        assert not fake.conn.execute("SELECT 1 FROM dkp_ledger WHERE reason='item'").fetchone()
    asyncio.run(go())


def test_crashed_sending_record_recovers_and_its_bid_is_held_in_later_raids(fake, clock):
    async def go():
        old, key, record = await unknown(fake, clock)
        record["state"] = "sending"
        with fake.conn:
            _put(fake.conn, key, record)
        await old.close("stopped", raided=False)
        later, _ = await session_for(fake, receipts=False)
        assert later.loot._held()[105] == record["intent"]["paid"]
        await reconcile_receipts(fake)
        assert _get(fake.conn, key)["state"] == "charged" and later.loot._held() == {}
    asyncio.run(go())


def test_wrong_ack_and_other_owner_do_not_settle(fake, clock):
    async def go():
        _, key, _ = await unknown(fake, clock)
        real_send = fake.send
        async def wrong(cmd):
            return replace(await real_send(cmd), id="another-command")
        fake.send = wrong
        await reconcile_receipts(fake)
        assert _get(fake.conn, key)["state"] == "uncertain"
        fake.send = real_send
        fake.human_guid = 2
        await clock.advance(RETRY_AFTER_S, step=RETRY_AFTER_S)
        await reconcile_receipts(fake)
        assert _get(fake.conn, key)["state"] == "uncertain"
    asyncio.run(go())


def test_receipt_opt_in_requires_native_support_and_off_wire_stays_unchanged(fake, clock):
    async def go():
        session, _ = await session_for(fake)
        fake.loot_receipts = False
        await session.on_loot_drop(LootDrop(0, 631, 77, 36597, (LootItem(50001, "Testblade", 4, 0),)))
        await clock.advance(20, step=20)
        await session.tick()
        assert not any(isinstance(cmd, GiveLoot) for cmd in fake.sent)
    asyncio.run(go())
    cmd = GiveLoot("test", 631, 77, 36597, 0, 50001, 105)
    assert "receipt" not in json.loads(encode_command(cmd, 0))


def test_receipt_config_is_strict_and_defaults_off(tmp_path):
    from leeroy_mind.config import ConfigError
    path = tmp_path / "mind.toml"
    assert load_guild_config(path).loot_receipts is False
    path.write_text("[guild]\nloot_receipts = true\n")
    assert load_guild_config(path).loot_receipts is True
    path.write_text('[guild]\nloot_receipts = "true"\n')
    with pytest.raises(ConfigError):
        load_guild_config(path)


def test_real_bridge_recovers_a_lost_award_ack_with_a_read_only_status(fake, clock):
    from leeroy_mind.testing.fake_bridge import FakeBridge
    from .test_loot_wire import _wired

    async def go():
        bridge = FakeBridge(human_guid=1, ack=lambda cmd: None if cmd["type"] == "give_loot" else (True, ""))
        client, task = await _wired(fake, bridge, ack_timeout=0.1)
        try:
            session, _ = await session_for(fake)
            await session.on_loot_drop(LootDrop(0, 631, 77, 36597, (LootItem(50001, "Testblade", 4, 0),)))
            await clock.advance(20, step=20)
            await session.tick()
            assert session.loot._records()[0][1]["state"] == "uncertain"
            await session.close("done", raided=False)
            await clock.advance(RETRY_AFTER_S, step=RETRY_AFTER_S)
            await reconcile_receipts(fake)
            await reconcile_receipts(fake)
            assert session.loot._records()[0][1]["state"] == "charged"
            awards = [cmd for cmd in bridge.commands if cmd["type"] == "give_loot"]
            proofs = [cmd for cmd in bridge.commands if cmd["type"] == "get_loot_receipt"]
            assert len(awards) == len(proofs) == 1
            assert awards[0]["receipt"] and proofs[0]["give_id"] == awards[0]["id"]
        finally:
            await client.close()
            await asyncio.wait_for(task, 2)
            await bridge.stop()
    asyncio.run(go())


def test_proven_unsent_give_can_restart_with_receipts_disabled(fake, clock):
    from leeroy_mind.bridge import NotConnected

    async def go():
        session, armory = await session_for(fake)
        await session.on_loot_drop(LootDrop(0, 631, 77, 36597, (LootItem(50001, "Testblade", 4, 0),)))
        real_send = fake.send
        async def disconnected(cmd):
            raise NotConnected("synthetic disconnect")
        fake.send = disconnected
        await clock.advance(20, step=20)
        await session.tick()
        key, record = session.loot._records()[0]
        assert record["state"] == "pending"
        assert not {"receipt", "receipt_owner", "receipt_guild", "intent", "attempt"} & record.keys()
        fake.send = real_send
        restarted = RaidSession(fake, GuildConfig(loot_receipts=False), armory, session.plan, session.raid_id)
        await restarted.tick()
        sent = [cmd for cmd in fake.sent if isinstance(cmd, GiveLoot)]
        assert len(sent) == 1 and sent[0].receipt is False
        assert _get(fake.conn, key)["state"] == "charged"
    asyncio.run(go())
