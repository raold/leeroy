"""Guild attendance and append-only DKP storage; timestamps are unix seconds.

Public writes own a connection transaction, as specified by the guild port.
The internal ledger insert joins its caller's transaction so decay and its
boundary receipt cannot commit separately. Event/session replay receipts belong
to their callers: equal ledger entries may be two legitimate awards.
"""

import sqlite3
from collections.abc import Iterable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .dkp import LedgerEntry


def new_raid(conn: sqlite3.Connection, *, started: float, raid_key: str, size: int,
             heroic: bool, offline: bool, lead_guid: int, tank_guid: int, loot_guid: int) -> int:
    with conn:
        row = conn.execute(
            "INSERT INTO guild_raid(started,raid_key,size,heroic,offline,lead_guid,tank_guid,loot_guid) "
            "VALUES (?,?,?,?,?,?,?,?)", (started, raid_key, size, int(heroic), int(offline),
                                       lead_guid, tank_guid, loot_guid))
        return row.lastrowid


def end_raid(conn, raid_id, *, ended, kills, wipes, outcome):
    with conn:
        conn.execute("UPDATE guild_raid SET ended=?,kills=?,wipes=?,outcome=? WHERE raid_id=?",
                     (ended, kills, wipes, outcome, raid_id))


def record_attendance(conn, raid_id, raided, benched):
    raided, benched = set(raided), set(benched)
    if raided & benched:
        raise ValueError("attendance cannot be both raided and benched")
    with conn:
        conn.executemany(
            "INSERT INTO guild_attendance(raid_id,guid,status) VALUES (?,?,?) "
            "ON CONFLICT(raid_id,guid) DO UPDATE SET status=excluded.status",
            [(raid_id, guid, status) for status, guids in (("raided", raided), ("benched", benched))
             for guid in sorted(guids)])


def bench_streaks(conn, guids) -> dict[int, int]:
    streaks = dict.fromkeys(guids, 0)
    done = set()
    rows = conn.execute(
        "SELECT a.guid,a.status FROM guild_attendance a JOIN guild_raid r USING(raid_id) "
        "ORDER BY r.started DESC,r.raid_id DESC")
    for guid, status in rows:
        if guid not in streaks or guid in done:
            continue
        if status == "raided":
            done.add(guid)
        else:
            streaks[guid] += 1
    return streaks


def attendance_counts(conn, guids, since) -> dict[int, int]:
    counts = dict.fromkeys(guids, 0)
    rows = conn.execute(
        "SELECT a.guid,COUNT(*) FROM guild_attendance a JOIN guild_raid r USING(raid_id) "
        "WHERE a.status='raided' AND r.started>=? GROUP BY a.guid", (since,))
    for guid, count in rows:
        if guid in counts:
            counts[guid] = count
    return counts


def last_run(conn) -> dict[str, float]:
    return {f"{key}{size}{'h' if heroic else ''}": started
            for key, size, heroic, started in conn.execute(
                "SELECT raid_key,size,heroic,MAX(started) FROM guild_raid "
                "GROUP BY raid_key,size,heroic")}


def last_led(conn) -> dict[int, float]:
    return dict(conn.execute("SELECT lead_guid,MAX(started) FROM guild_raid "
                             "WHERE offline=0 GROUP BY lead_guid"))


def offline_raid_between(conn, start, end) -> bool:
    """Whether an offline run started in the half-open local-day interval."""
    return conn.execute("SELECT 1 FROM guild_raid WHERE offline=1 AND started>=? "
                        "AND started<? LIMIT 1", (start, end)).fetchone() is not None


def _insert_ledger(conn, entries: Iterable["LedgerEntry"]):
    conn.executemany(
        "INSERT INTO dkp_ledger(ts,guid,delta,reason,raid_id,item_id,note) VALUES (?,?,?,?,?,?,?)",
        ((e.ts, e.guid, e.delta, e.reason, e.raid_id, e.item_id, e.note) for e in entries))


def add_ledger(conn, entries: Iterable["LedgerEntry"]):
    with conn:
        _insert_ledger(conn, entries)


def balances(conn) -> dict[int, float]:
    return dict(conn.execute("SELECT guid,SUM(delta) FROM dkp_ledger GROUP BY guid ORDER BY guid"))


def flags(conn, kind) -> set[int]:
    return {row[0] for row in conn.execute("SELECT guid FROM guild_flag WHERE kind=?", (kind,))}


def set_flag(conn, kind, guid, ts, value=None):
    with conn:
        conn.execute("INSERT INTO guild_flag(kind,guid,ts,value) VALUES (?,?,?,?) "
                     "ON CONFLICT(kind,guid) DO UPDATE SET ts=excluded.ts,value=excluded.value",
                     (kind, guid, ts, value))


def clear_flag(conn, kind, guid):
    with conn:
        conn.execute("DELETE FROM guild_flag WHERE kind=? AND guid=?", (kind, guid))
