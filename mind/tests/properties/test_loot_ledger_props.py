"""The DKP hand-out (guild/loot.py LootMaster) over generated raid nights: drops, his claims,
credits, the clock, and every give outcome the bridge can have."""
import asyncio
from collections import deque
from datetime import datetime, timezone
import json
import tempfile

import pytest

pytest.importorskip("hypothesis", reason='Hypothesis comes with the test extra: pip install -e ".[test]"')

from hypothesis import given, settings, strategies as st

from leeroy_mind.bridge import AckTimeout, NotConnected
from leeroy_mind.guild.config import GuildConfig
from leeroy_mind.guild.dkp import LedgerEntry
from leeroy_mind.guild.loot import RETRY_AFTER_S
from leeroy_mind.guild.session import RaidPlan, RaidSession
from leeroy_mind.guild.store import add_ledger, balances
from leeroy_mind.guild.upgrades import ItemInfo
from leeroy_mind.protocol import Ack, LootDrop, LootItem
from leeroy_mind.testing.fake_clock import FakeClock

from guild.fakes import FakeArmory, FakePorts, member, persona

MEMBERS = (101, 102, 103, 104, 105, 106, 107)
# warriors, a death knight and a hunter can all use the two-hander; their gear differs
CLASSES = {101: (1, 2), 102: (5, 0), 103: (8, 1), 104: (8, 1), 105: (1, 0), 106: (6, 1), 107: (3, 1)}
BLADE = ItemInfo(50001, "Testblade", 4, 264, 17, 2, 8, -1, frozenset({4}))
OUTCOMES = ("ok", "ok", "refused", "lost", "unsent")

steps = st.lists(st.one_of(
    st.tuples(st.just("credit"), st.sampled_from(MEMBERS), st.integers(1, 120)),
    st.tuples(st.just("drop"), st.integers(1, 3)),
    st.tuples(st.just("claim")),
    st.tuples(st.just("wait"), st.sampled_from((1, 5, 20, RETRY_AFTER_S, 120))),
    st.tuples(st.just("tick"), st.lists(st.sampled_from(OUTCOMES), max_size=4)),
), max_size=30)


def records(ports):
    return {key: json.loads(value) for key, value in
            ports.conn.execute("SELECT key,value FROM meta WHERE key GLOB 'guild:loot:*'")}


def check(ports, session):
    """No balance pays twice and every charge is one acknowledged hand-out."""
    held = session.loot._held()
    owed = balances(ports.conn)
    for guid in MEMBERS:
        assert owed.get(guid, 0) - held.get(guid, 0) >= -1e-9, (guid, owed.get(guid), held.get(guid))
    charges = ports.conn.execute("SELECT guid,delta,note FROM dkp_ledger WHERE reason='item'").fetchall()
    notes = [note for _, _, note in charges]
    assert len(notes) == len(set(notes))  # a slot is charged at most once
    rows = records(ports)
    for guid, delta, note in charges:
        record = rows[note]
        assert record["state"] == "charged" and record["award"]["guid"] == guid
        assert delta == -record["award"]["paid"]
        assert guid != ports.human_guid or record["human_claimed"] is True
    assert {k for k, r in rows.items() if r["state"] == "charged"} == set(notes)


@settings(max_examples=max(10, settings.default.max_examples // 4), deadline=None)  # a database per example
@given(steps)
def test_no_balance_is_spent_twice_and_every_charge_is_one_acknowledged_give(script):
    """Over any raid night (credits, drops, his claims, time passing, and gives that are
    acknowledged, refused, lost on the wire or never sent), after every step:
    - each member's balance less the DKP her unsettled hand-outs hold (an acknowledged give not
      yet charged, a give of unknown outcome that may still be sent again) is never negative:
      a later bid never spends what an earlier award already holds;
    - a slot is charged at most once, only after its give was acknowledged, to its winner, at the
      price the award fixed; he is charged only for a drop he claimed."""
    with tempfile.TemporaryDirectory() as tmp:
        asyncio.run(_night(tmp, script))


async def _night(tmp, script):
    clock = FakeClock(datetime(2026, 10, 29, 20, 0, tzinfo=timezone.utc), yields=1)
    ports = FakePorts(tmp, clock)
    try:
        for guid in MEMBERS:
            cls, spec = CLASSES[guid]
            ports.members[guid] = member(guid, f"M{guid}", cls, spec, map=631)
            ports.personas[guid] = persona(guid, f"M{guid}", guild_role="officer" if guid in {101, 104} else "raider",
                                           wants=("Testblade",) if guid in {106, 107} else ())
        armory = FakeArmory({50001: BLADE}, {105: {15: 251}, 106: {15: 245}, 101: {15: 264}})
        plan = RaidPlan("icc", 10, False, False, 101, 101, 104, list(MEMBERS), [], True)
        session = await RaidSession.open(ports, GuildConfig(loot_receipts=False), armory, plan)
        assert session.bind_instance(77)
        outcomes = deque()

        async def send(cmd, *, expected_epoch=None):
            ports.sent.append(cmd)
            outcome = outcomes.popleft() if outcomes else "ok"
            if outcome == "lost":
                raise AckTimeout("no ack")
            if outcome == "unsent":
                raise NotConnected("bridge down")
            return Ack(0, cmd.id, outcome == "ok", "" if outcome == "ok" else "no_item")

        ports.send = send
        slot = 0
        check(ports, session)
        for step in script:
            if step[0] == "credit":
                add_ledger(ports.conn, [LedgerEntry(clock.now(), step[1], float(step[2]), "adjust")])
            elif step[0] == "drop":
                items = tuple(LootItem(50001, "Testblade", 4, slot + i) for i in range(step[1]))
                slot += step[1]
                await session.on_loot_drop(LootDrop(0, 631, 77, 36597, items))
            elif step[0] == "claim":
                session.loot.on_human_chat("mine")
            elif step[0] == "wait":
                await clock.advance(step[1], step=step[1])
            else:
                outcomes.extend(step[1])
                await session.tick()
            check(ports, session)
    finally:
        ports.close()
