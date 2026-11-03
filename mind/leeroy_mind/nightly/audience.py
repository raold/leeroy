"""Who could see a chat line, and an utterance's bit and reason (P1c Task 6): the memo's "start
logging now" items, which P1b Task 17 persists in 0002_audience_and_reasons.sql (controller
ruling I6). The three column names below are the one place to change if P1b's ever differ.

Where a column is missing, or a row stored before it holds NULL there, a fallback answers, and
says so: its first use in a process logs it at WARNING, once per reason, so Task 23's night
health can show which source the week used. The human is never in an audience: he is not
simulated."""
import json
import logging
import sqlite3

from leeroy_mind.logs import log
from leeroy_mind.storage.store import Store
from leeroy_mind.wow import CAST_MIN_LEVEL

AUDIENCE_COLUMN = "audience"  # chat_line: a nullable JSON list of the cast guids who could see it
BIT_COLUMN = "bit"            # utterance: the bit the turn brought out
REASON_COLUMN = "why"         # utterance: how the turn came about, e.g. "reply:named"
NEAR_S = 900.0                # P1b's bystander rule: who sent a line within 15 minutes of it
BIT_AFTER_S = 120.0           # P1b's _after_sent writes a bit's bit_use row once its lines are sent

_WARNED: set[str] = set()     # the fallbacks this process has announced


def _warn_once(event: str, **fields) -> None:
    key = event + "".join(f"|{k}={v}" for k, v in sorted(fields.items()))
    if key not in _WARNED:
        _WARNED.add(key)
        log(event, level=logging.WARNING, **fields)


def _rows(store: Store, sql: str, params=()) -> list[sqlite3.Row]:
    with store.transaction():
        return store.conn.execute(sql, params).fetchall()


def _columns(store: Store, table: str) -> set[str]:
    return {r[1] for r in _rows(store, f"PRAGMA table_info({table})")}


def has_line_audience(store: Store) -> bool:
    return AUDIENCE_COLUMN in _columns(store, "chat_line")


def has_utterance_columns(store: Store) -> bool:
    return {BIT_COLUMN, REASON_COLUMN} <= _columns(store, "utterance")


def cast_names(store: Store) -> dict[int, str]:
    """The cast as the night counts it (NightContext.members): a stored persona, in the stored
    roster at level CAST_MIN_LEVEL or more, and not the human (meta human_guid); guid to
    character name."""
    human = int(store.get_meta("human_guid") or 0)
    roster = store.cast()
    return {g: roster[g].name for g in sorted(store.persona_versions())
            if g != human and g in roster and roster[g].level >= CAST_MIN_LEVEL}


def audience_of(store: Store, line_id: int) -> frozenset[int]:
    """The cast members who could see the line: the cast guids in its audience column when the
    column exists and holds a value. Otherwise the fallback: a whisper's two ends, or for any
    other channel the cast members who sent a line on any channel within 15 minutes of it, plus
    its speaker. Empty for an unknown line."""
    cast = cast_names(store)
    column = has_line_audience(store)
    seen = f", {AUDIENCE_COLUMN} AS seen" if column else ""
    rows = _rows(store, f"SELECT ts, channel, from_guid, to_guid{seen} FROM chat_line WHERE id = ?",
                 (line_id,))
    if not rows:
        return frozenset()
    line = rows[0]
    if column and line["seen"] is not None:
        return frozenset(g for g in json.loads(line["seen"]) if g in cast)
    _warn_once("audience_fallback", reason="null_value" if column else "no_column")
    if line["channel"] == "whisper":
        return frozenset(g for g in (line["from_guid"], line["to_guid"]) if g in cast)
    near = {r[0] for r in _rows(store, "SELECT DISTINCT from_guid FROM chat_line "
                                       "WHERE ts BETWEEN ? AND ?",
                                (line["ts"] - NEAR_S, line["ts"] + NEAR_S))}
    return frozenset(g for g in near | {line["from_guid"]} if g in cast)


def _utterance(store: Store, utterance) -> sqlite3.Row | None:
    """The utterance row, from its id or from a row that carries one."""
    uid = utterance if isinstance(utterance, int) else utterance["id"]
    rows = _rows(store, "SELECT * FROM utterance WHERE id = ?", (uid,))
    return rows[0] if rows else None


def bit_of(store: Store, utterance) -> str | None:
    """The bit an utterance (its id, or its row) brought out: the bit column's value when it has
    one; otherwise, for kind "bit", the bit_id of the speaker's bit_use row within BIT_AFTER_S
    after it (pre-flight PR5: a NULL is read like a missing column); else None."""
    row = _utterance(store, utterance)
    if row is None:
        return None
    column = BIT_COLUMN in row.keys()
    if column and row[BIT_COLUMN] is not None:
        return row[BIT_COLUMN]
    if row["kind"] != "bit":
        return None
    _warn_once("bit_fallback", reason="null_value" if column else "no_column")
    used = _rows(store, "SELECT bit_id FROM bit_use WHERE guid = ? AND ts >= ? AND ts <= ? "
                        "ORDER BY ts, id LIMIT 1",
                 (row["guid"], row["ts"], row["ts"] + BIT_AFTER_S))
    return used[0]["bit_id"] if used else None


def reason_of(store: Store, utterance) -> str | None:
    """How an utterance (its id, or its row) came about: the reason column's value. Without the
    column it is None, and the first such read logs utterance_reason_missing; the reason then
    lives only in P1b's log event, and Task 21's bandits stay conservative."""
    row = _utterance(store, utterance)
    if row is None:
        return None
    if REASON_COLUMN in row.keys():
        return row[REASON_COLUMN]
    _warn_once("utterance_reason_missing")
    return None
