"""What each member did with each counterpart: moves and topics per turn, and short pair notes.
Stored per pair and read back through the whisper rule, so nothing from a whisper reaches a
guild turn or another member's prompt."""
from __future__ import annotations

import json
from dataclasses import dataclass

from ..memory.recall import whisper_ok
from ..storage.store import Store
from .filters import _final_clause
from .moves import classify, main_move, topics

MOVES_SHOWN = 10
MOVES_KEEP_S = 3 * 86400.0
NOTES_KEEP_S = 30 * 86400.0
SCENE_SHOWN = 3
NOTE_MAX = 80
_FETCH = 200  # rows read before the whisper filter, so hidden ones cannot starve the result


@dataclass(frozen=True, slots=True)
class MoveRow:
    ts: float
    channel: str
    to_guid: int
    moves: frozenset[str]
    main: str
    topics: tuple[str, ...]
    clause: str
    angle: str


class MoveLog:
    def __init__(self, store: Store):
        self._store = store
        self._pruned = False

    def record(self, *, guid: int, counterpart: int, channel: str, to_guid: int, text: str,
               ts: float, angle: str = "") -> None:
        moves = classify(text)
        with self._store.transaction():
            conn = self._store.conn
            if not self._pruned:
                conn.execute("DELETE FROM talk_move WHERE ts < ?", (ts - MOVES_KEEP_S,))
                self._pruned = True
            conn.execute(
                "INSERT INTO talk_move(ts, guid, counterpart, channel, to_guid, moves, main, "
                "topics, clause, angle) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (ts, guid, counterpart, channel, to_guid, json.dumps(sorted(moves)),
                 main_move(moves), json.dumps(list(topics(text))), _final_clause(text), angle))

    def recent(self, guid: int, counterpart: int, *, turn_channel: str, to_guid: int,
               n: int = MOVES_SHOWN) -> list[MoveRow]:
        with self._store.transaction():
            rows = self._store.conn.execute(
                "SELECT ts, channel, to_guid, moves, main, topics, clause, angle FROM talk_move "
                "WHERE guid = ? AND counterpart = ? ORDER BY ts DESC, id DESC LIMIT ?",
                (guid, counterpart, _FETCH)).fetchall()
        out: list[MoveRow] = []
        for r in rows:
            if not whisper_ok(r["channel"], guid, r["to_guid"], speaker=guid,
                              turn_channel=turn_channel, to_guid=to_guid):
                continue
            out.append(MoveRow(r["ts"], r["channel"], r["to_guid"], frozenset(json.loads(r["moves"])),
                               r["main"], tuple(json.loads(r["topics"])), r["clause"], r["angle"]))
            if len(out) == n:
                break
        return out[::-1]


class PairNotes:
    def __init__(self, store: Store):
        self._store = store
        self._pruned = False

    def add(self, guid: int, other: int, kind: str, text: str, *, channel: str, to_guid: int,
            ts: float) -> None:
        text = text[:NOTE_MAX]
        with self._store.transaction():
            conn = self._store.conn
            if not self._pruned:
                conn.execute("DELETE FROM pair_note WHERE ts < ?", (ts - NOTES_KEEP_S,))
                self._pruned = True
            last = conn.execute("SELECT text FROM pair_note WHERE guid = ? AND other = ? "
                                "ORDER BY ts DESC, id DESC LIMIT 1", (guid, other)).fetchone()
            if last is not None and last["text"] == text:
                return
            conn.execute("INSERT INTO pair_note(ts, guid, other, kind, text, channel, to_guid) "
                         "VALUES (?,?,?,?,?,?,?)", (ts, guid, other, kind, text, channel, to_guid))

    def recent(self, guid: int, other: int, *, turn_channel: str, to_guid: int,
               n: int = SCENE_SHOWN) -> list[str]:
        with self._store.transaction():
            rows = self._store.conn.execute(
                "SELECT text, channel, to_guid FROM pair_note WHERE guid = ? AND other = ? "
                "ORDER BY ts DESC, id DESC LIMIT ?", (guid, other, _FETCH)).fetchall()
        out = [r["text"] for r in rows
               if whisper_ok(r["channel"], guid, r["to_guid"], speaker=guid,
                             turn_channel=turn_channel, to_guid=to_guid)]
        return out[:n][::-1]
