"""Realm storage on the mind connection; no game-facing operations.

Each public write owns ``with conn``. Call these only outside
``Store.transaction()``: the canonical connection has ``autocommit=False``,
so ``conn.in_transaction`` cannot identify a nested Store unit of work.
Receipts are bounded diagnostics, never movement, ownership or release authority.
"""

import math
import sqlite3
from dataclasses import dataclass


_SQLITE_MAX_INT = (1 << 63) - 1
_UINT64_MAX = (1 << 64) - 1
_RECEIPT_CAP = 256


@dataclass(frozen=True, slots=True)
class CanonKill:
    map: int
    boss_id: int
    difficulty: int
    moment: str
    day: str


@dataclass(frozen=True, slots=True)
class RealmReceipt:
    seq: int
    command_id: str
    kind: str
    stage: str
    ok: bool
    reason: str
    value: int
    ts: float


def _integer(value: object, name: str, lo: int, hi: int = _SQLITE_MAX_INT) -> None:
    if type(value) is not int or not lo <= value <= hi:
        raise ValueError(f"{name}: expected an integer in range")


def _finite(value: object) -> bool:
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def _timestamp(value: object) -> float:
    if not _finite(value):
        raise ValueError("ts: expected a finite number")
    return float(value)


def _text(value: object, name: str, *, nonempty: bool = False) -> None:
    if type(value) is not str or (nonempty and not value):
        raise ValueError(f"{name}: expected {'a nonempty string' if nonempty else 'a string'}")


def record_kick(conn: sqlite3.Connection, guid: int, ts: float, rank: int) -> None:
    """Refresh the observed kick without resetting any explicit owner decision."""
    _integer(guid, "guid", 1)
    _integer(rank, "rank", 0)
    ts = _timestamp(ts)
    with conn:
        conn.execute(
            "INSERT INTO realm_kick(guid,ts,rank) VALUES (?,?,?) "
            "ON CONFLICT(guid) DO UPDATE SET ts=excluded.ts,rank=excluded.rank",
            (guid, ts, rank))


def kicked(conn: sqlite3.Connection) -> set[int]:
    return {row[0] for row in conn.execute("SELECT guid FROM realm_kick")}


def canon_kills(conn: sqlite3.Connection) -> list[CanonKill]:
    return [CanonKill(*row) for row in conn.execute(
        "SELECT map,boss_id,difficulty,moment,day FROM realm_canon_kill "
        "ORDER BY map,boss_id,difficulty")]


def set_actor_place(conn: sqlite3.Connection, guid: int, place: str, ts: float) -> None:
    """Update a known identity; a position receipt cannot create a realm actor."""
    _integer(guid, "guid", 1)
    _text(place, "place")
    ts = _timestamp(ts)
    with conn:
        changed = conn.execute(
            "UPDATE realm_actor SET place=?,placed=? WHERE guid=?", (place, ts, guid)).rowcount
        if not changed:
            raise ValueError("unknown realm actor")


def actor_places(conn: sqlite3.Connection) -> dict[int, tuple[str, float]]:
    return {guid: (place, float(placed)) for guid, place, placed in conn.execute(
        "SELECT guid,place,placed FROM realm_actor WHERE place<>'' ORDER BY guid")
        if type(place) is str and _finite(placed)}


def record_receipt(conn: sqlite3.Connection, *, command_id: str, kind: str, stage: str,
                   ok: bool, reason: str, value: int = 0, ts: float) -> bool:
    """Keep the first receipt per command/stage, retaining the newest 256 inserts.

    The caller must first match this receipt to an actually sent command. IDs
    and kind strings alone establish no provenance. Returns whether inserted.
    Values use decimal TEXT so the full unsigned 64-bit range stays exact.
    """
    _text(command_id, "command_id", nonempty=True)
    _text(kind, "kind", nonempty=True)
    _text(stage, "stage")
    if stage not in ("ack", "job"):
        raise ValueError("stage: expected ack or job")
    if type(ok) is not bool:
        raise ValueError("ok: expected a boolean")
    _text(reason, "reason")
    _integer(value, "value", 0, _UINT64_MAX)
    ts = _timestamp(ts)
    with conn:
        inserted = conn.execute(
            "INSERT INTO realm_receipt(command_id,kind,stage,ok,reason,value,ts) "
            "VALUES (?,?,?,?,?,?,?) ON CONFLICT(command_id,stage) DO NOTHING",
            (command_id, kind, stage, int(ok), reason, str(value), ts)).rowcount == 1
        conn.execute(
            "DELETE FROM realm_receipt WHERE seq NOT IN "
            "(SELECT seq FROM realm_receipt ORDER BY seq DESC LIMIT ?)", (_RECEIPT_CAP,))
    return inserted


def recent_receipts(conn: sqlite3.Connection, *, failures_only: bool = False,
                    limit: int = 20) -> list[RealmReceipt]:
    if type(failures_only) is not bool:
        raise ValueError("failures_only: expected a boolean")
    _integer(limit, "limit", 1, _RECEIPT_CAP)
    rows = conn.execute(
        "SELECT seq,command_id,kind,stage,ok,reason,value,ts FROM realm_receipt"
        + (" WHERE ok=0" if failures_only else "") + " ORDER BY seq DESC LIMIT ?", (limit,))
    return [RealmReceipt(seq, command_id, kind, stage, bool(ok), reason, int(value), float(ts))
            for seq, command_id, kind, stage, ok, reason, value, ts in rows]
