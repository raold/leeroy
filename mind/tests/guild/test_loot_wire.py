"""The DKP hand-out over the real bridge client and the fake mod-leeroy server (raid 18,
2026-11-20: every drop with bidders ended `uncertain` and no give_loot reached the game)."""

import asyncio
from functools import partial
import json
from types import SimpleNamespace

from leeroy_mind.bridge import Backoff, BridgeClient, NotConnected
from leeroy_mind.guild.config import GuildConfig
from leeroy_mind.guild.loot import RETRY_AFTER_S
from leeroy_mind.guild.ports import JobWaiter, LivePorts
from leeroy_mind.guild.protocol import register
from leeroy_mind.guild.session import RaidPlan, RaidSession
from leeroy_mind.guild.upgrades import ItemInfo
from leeroy_mind.protocol import LootDrop, LootItem
from leeroy_mind.testing.fake_bridge import FakeBridge

from .fakes import FakeArmory, member, persona


def _setup(fake):
    for guid, cls, spec in ((101, 1, 2), (104, 8, 1), (105, 1, 0)):
        fake.members[guid] = member(guid, f"M{guid}", cls, spec, map=631)
        fake.personas[guid] = persona(guid, f"M{guid}", guild_role="officer" if guid in {101, 104} else "raider")
    blade = ItemInfo(50001, "Testblade", 4, 264, 17, 2, 8, -1, frozenset({4}))
    armory = FakeArmory({50001: blade}, {101: {15: 264}, 105: {15: 251}})
    plan = RaidPlan("icc", 25, True, False, 1, 101, 104, [101, 104, 105], [], True)
    return armory, plan


async def _wired(fake, bridge, **client_kw):
    """The guild's LivePorts.send, unchanged, over a real BridgeClient."""
    register()
    port = await bridge.start()

    async def on_event(ev):
        return None

    client = BridgeClient("127.0.0.1", port, on_event, backoff=Backoff(initial=0.05, maximum=0.1, jitter=0),
                          **client_kw)
    task = asyncio.create_task(client.run())
    await client.wait_connected(2.0)
    live = SimpleNamespace(enabled=True, jobs=JobWaiter(), app=SimpleNamespace(bridge=client))
    fake.send = partial(LivePorts.send, live)
    return client, task


def _states(ports, raid_id):
    rows = ports.conn.execute("SELECT value FROM meta WHERE key LIKE ?", (f"guild:loot:{raid_id}:%",)).fetchall()
    return [json.loads(value)["state"] for (value,) in rows]


def test_a_won_drop_reaches_the_game_and_is_charged(fake, clock):
    async def go():
        bridge = FakeBridge(human_guid=1)
        client, task = await _wired(fake, bridge)
        try:
            armory, plan = _setup(fake)
            session = await RaidSession.open(fake, GuildConfig(), armory, plan)
            assert session.bind_instance(77)
            await session.on_loot_drop(LootDrop(0, 631, 77, 36597, (LootItem(50001, "Testblade", 4, 0),)))
            await clock.advance(20, step=20)
            await session.tick()
            gives = [cmd for cmd in bridge.commands if cmd.get("type") == "give_loot"]
            assert [(cmd["to_guid"], cmd["item_id"], cmd["index"]) for cmd in gives] == [(105, 50001, 0)]
            assert _states(fake, session.raid_id) == ["charged"]
            charged = fake.conn.execute("SELECT guid,item_id FROM dkp_ledger WHERE reason='item'").fetchall()
            assert [tuple(row) for row in charged] == [(105, 50001)]
        finally:
            await client.close()
            await asyncio.wait_for(task, 2.0)
            await bridge.stop()
    asyncio.run(go())


def test_an_unanswered_give_is_sent_again_under_a_new_identity_and_charged_once(fake, clock):
    """No ack (a world stall, a dropped line): the bridge never reissues an identity, so the one
    resend has its own; native's slot answers it (here: given now)."""
    answered = []

    def ack(cmd):
        if cmd.get("type") != "give_loot":
            return True, ""
        answered.append(cmd["id"])
        return None if len(answered) == 1 else (True, "")

    async def go():
        bridge = FakeBridge(human_guid=1, ack=ack)
        client, task = await _wired(fake, bridge, ack_timeout=0.2)
        try:
            armory, plan = _setup(fake)
            session = await RaidSession.open(fake, GuildConfig(), armory, plan)
            assert session.bind_instance(77)
            await session.on_loot_drop(LootDrop(0, 631, 77, 36597, (LootItem(50001, "Testblade", 4, 0),)))
            await clock.advance(20, step=20)
            await session.tick()
            assert _states(fake, session.raid_id) == ["uncertain"]
            assert fake.said[-1][2] == "Testblade: lost track of that hand-out, checking again"
            await clock.advance(RETRY_AFTER_S, step=RETRY_AFTER_S)
            await session.tick()
            assert answered == [answered[0], answered[0] + "-r1"]
            assert _states(fake, session.raid_id) == ["charged"]
            charged = fake.conn.execute("SELECT guid,item_id FROM dkp_ledger WHERE reason='item'").fetchall()
            assert [tuple(row) for row in charged] == [(105, 50001)]
        finally:
            await client.close()
            await asyncio.wait_for(task, 2.0)
            await bridge.stop()
    asyncio.run(go())


def test_a_give_the_bridge_refused_before_writing_goes_again_under_the_same_rules(fake, clock):
    """LivePorts.send, the real client and its identity rules: a give refused NotConnected (first
    try, and a resend after an unanswered one) is sent again once the bridge is back."""
    answered = []

    def ack(cmd):
        if cmd.get("type") != "give_loot":
            return True, ""
        answered.append(cmd["id"])
        return None if len(answered) == 1 else (True, "")

    async def go():
        bridge = FakeBridge(human_guid=1, ack=ack)
        client, task = await _wired(fake, bridge, ack_timeout=0.2)
        down = ["down", None, "down", None]
        real = client.send

        async def flaky(cmd, **kw):
            if cmd.__class__.__name__ == "GiveLoot" and down.pop(0) == "down":
                raise NotConnected("bridge is not connected")
            return await real(cmd, **kw)

        client.send = flaky
        try:
            armory, plan = _setup(fake)
            session = await RaidSession.open(fake, GuildConfig(), armory, plan)
            assert session.bind_instance(77)
            await session.on_loot_drop(LootDrop(0, 631, 77, 36597, (LootItem(50001, "Testblade", 4, 0),)))
            await clock.advance(20, step=20)
            await session.tick()  # refused before writing: pending again
            assert _states(fake, session.raid_id) == ["pending"] and answered == []
            await session.tick()  # the same identity goes out; no ack
            assert _states(fake, session.raid_id) == ["uncertain"] and len(answered) == 1
            await clock.advance(RETRY_AFTER_S, step=RETRY_AFTER_S)
            await session.tick()  # the resend is refused before writing
            assert _states(fake, session.raid_id) == ["uncertain"] and len(answered) == 1
            await session.tick()  # and goes again: answered and charged
            assert answered == [answered[0], answered[0] + "-r1"]
            assert _states(fake, session.raid_id) == ["charged"]
        finally:
            await client.close()
            await asyncio.wait_for(task, 2.0)
            await bridge.stop()
    asyncio.run(go())


def test_loot_call_is_window_exempt_and_a_replayed_drop_is_called_once(fake, clock):
    """A loot call bypasses the guild-wide chat window (2026-11-25 13:45: dropped after a gz burst);
    the same drop seen twice is still called once."""
    async def go():
        bridge = FakeBridge(human_guid=1)
        client, task = await _wired(fake, bridge)
        try:
            armory, plan = _setup(fake)
            session = await RaidSession.open(fake, GuildConfig(), armory, plan)
            assert session.bind_instance(77)
            drop = LootDrop(0, 631, 77, 36597, (LootItem(50001, "Testblade", 4, 0),))
            await session.on_loot_drop(drop)
            await session.on_loot_drop(drop)
            calls = [text for _, _, text in fake.said if text.startswith("Testblade")]
            assert len(calls) == 1
            assert fake.exempt == calls
        finally:
            await client.close()
            await asyncio.wait_for(task, 2.0)
            await bridge.stop()
    asyncio.run(go())
