"""The Guild tab's DKP stats: attendance, who is due an item and what each raid handed out,
from synthetic rows. Read-only questions to mind.db (raidview.dkp_stats)."""
import asyncio
import sqlite3
from pathlib import Path

import pytest

from console.rig import http, make_app, running
from leeroy_mind.console import raidview
from leeroy_mind.guild.dkp import LedgerEntry
from leeroy_mind.guild.store import add_ledger, end_raid, new_raid, record_attendance

T0 = 1_793_000_000.0
DAY = 86400.0


@pytest.fixture
def data(store):
    return Path(store.conn.execute("PRAGMA database_list").fetchone()["file"]).parent


def _raid(conn, n, raided, benched=(), *, finish=True):
    rid = new_raid(conn, started=T0 + n * DAY, raid_key="naxx", size=10, heroic=False,
                   offline=False, lead_guid=1101, tank_guid=1101, loot_guid=1101)
    record_attendance(conn, rid, raided, benched)
    if finish:
        end_raid(conn, rid, ended=T0 + n * DAY + 3600, kills=1, wipes=0, outcome="complete")
    return rid


@pytest.fixture
def db(store):
    """5 finished raids and 1 open. 1101 raided 1-5, 1102 raided 2-5, 1103 raided 1 and was
    benched 3 and 5, 1104 raided 2-5. 1101 won an item in raid 2; 1102 holds 30 DKP; 1104 none."""
    c = store.conn
    ids = {1: _raid(c, 1, [1101, 1103]), 2: _raid(c, 2, [1101, 1102, 1104]),
           3: _raid(c, 3, [1101, 1102, 1104], [1103]), 4: _raid(c, 4, [1101, 1102, 1104]),
           5: _raid(c, 5, [1101, 1102, 1104], [1103])}
    _raid(c, 6, [1101, 1102, 1103, 1104], finish=False)
    add_ledger(c, [LedgerEntry(T0 + 2 * DAY, 1101, 30, "kill", ids[2]),
                   LedgerEntry(T0 + 2 * DAY + 1, 1101, -20, "item", ids[2], 40001, "x"),
                   LedgerEntry(T0 + 4 * DAY, 1102, 30, "kill", ids[4]),
                   LedgerEntry(T0 + 4 * DAY, 1104, 5, "kill", ids[4]),
                   LedgerEntry(T0 + 5 * DAY, 1104, -5, "decay"),
                   LedgerEntry(T0 + 6 * DAY, 1103, 50, "attendance", None)])
    # a raid that is still open counts for nobody
    add_ledger(c, [LedgerEntry(T0 + 6 * DAY, 1102, 99, "kill", 6)])
    return c, ids


def test_attendance_counts_raids_since_a_members_first(db):
    c, _ = db
    s = {m["guid"]: m for m in raidview.dkp_stats(c)["members"]}
    assert (s[1101]["attendance"], s[1102]["attendance"], s[1103]["attendance"]) == (100, 100, 60)
    assert s[1102]["possible"] == 4            # the open raid counts for nobody
    assert (s[1103]["raids"], s[1103]["benched"], s[1103]["possible"]) == (1, 2, 5)


def test_due_is_four_raids_without_an_item_and_dkp_left(db):
    c, _ = db
    stats = raidview.dkp_stats(c)
    s = {m["guid"]: m for m in stats["members"]}
    assert s[1101]["since_item"] == 3 and not s[1101]["due"]
    assert s[1102]["since_item"] == 4 and s[1102]["due"]
    assert s[1104]["since_item"] == 4 and not s[1104]["due"]  # no DKP left
    assert stats["due"] == [1102]


def test_due_is_ordered_by_balance_and_capped(store):
    c = store.conn
    guids = list(range(2001, 2001 + raidview.DUE_MAX + 3))
    for n in range(1, raidview.DUE_RAIDS + 1):
        _raid(c, n, guids)
    add_ledger(c, [LedgerEntry(T0, g, float(g - 2000), "kill") for g in guids])
    due = raidview.dkp_stats(c)["due"]
    assert len(due) == raidview.DUE_MAX and due[0] == guids[-1]
    assert due == sorted(due, reverse=True)


def test_each_raid_says_what_it_handed_out(store):
    c = store.conn
    _raid(c, 1, [1101])
    rid = _raid(c, 3, [1101, 1102, 1105], [1103])
    add_ledger(c, [LedgerEntry(T0, g, 10, "kill", rid) for g in (1101, 1102, 1105)]
               + [LedgerEntry(T0, g, 5, "attendance", rid) for g in (1101, 1102, 1105)]
               + [LedgerEntry(T0, 1103, 5, "standby", rid),
                  LedgerEntry(T0, 1101, -20, "item", rid, 40001, "x")])
    raids = {r["raid_id"]: r for r in raidview.dkp_stats(c)["raids"]}
    r = raids[rid]
    assert (r["awarded"], r["spent"], r["items"], r["paid"]) == (50.0, 20.0, 1, 4)
    assert (r["raid"], r["size"], r["heroic"], r["offline"], r["kills"]) == ("naxx", 10, False, False, 1)


def test_dkp_stats_on_an_older_mind_db_are_empty(tmp_path):
    conn = sqlite3.connect(tmp_path / "old.db")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)")
    assert raidview.dkp_stats(conn) == {"members": [], "raids": [], "due": []}


def test_the_guild_route_carries_dkp_stats(db, data):
    async def go():
        app = make_app(data, mind=running(data))
        await app.start()
        try:
            g = await http(app, "GET", "/api/guild")
            assert g.status == 200
            assert set(g.json()["dkp_stats"]) == {"members", "raids", "due"}
        finally:
            await app.close()
    asyncio.run(go())
