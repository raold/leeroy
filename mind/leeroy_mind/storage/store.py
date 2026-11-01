import json
import logging
import re
import sqlite3
from contextlib import contextmanager

from leeroy_mind import protocol
from leeroy_mind.clock import Clock
from leeroy_mind.protocol import Chat, Member
from leeroy_mind.storage import db
from leeroy_mind.storage.rows import (
    REL_DIMS, REL_RANGES, ChatLineRow, MemoryRow, PersonaStateRow, Relationship)

log = logging.getLogger(__name__)

VEC_DIM = db.VEC_DIM
MAX_TURN_LINES = 3  # a turn says at most 3 lines (a bit), 2 otherwise (talk.turns)
TURN_GAP_S = 120.0  # a turn's lines are typed one after another, never this far apart
_SURROGATE = re.compile("[\ud800-\udfff]")
_WORD = re.compile(r"\w+", re.UNICODE)


def clean(s: str) -> str:
    """json.loads accepts lone-surrogate escapes, and such a str cannot be encoded. Replace each
    one with U+FFFD before it reaches SQLite. (A valid pair is one astral char in a Python str,
    so every surrogate seen here is lone.)"""
    try:
        s.encode("utf-8")
        return s
    except UnicodeEncodeError:
        return _SURROGATE.sub("�", s)


def _j(obj) -> str:
    return clean(json.dumps(obj, ensure_ascii=False))


def fts_query(text: str) -> str:
    """Turn free text into a safe FTS5 query: each word quoted, OR-joined. Operators and
    punctuation (quote, star, minus, NEAR, parentheses, colon) cannot reach the parser.
    Returns '' when there is nothing to search for."""
    return " OR ".join(f'"{w}"' for w in _WORD.findall(clean(text)))


def _guids(raw: str | None) -> tuple[int, ...] | None:
    return None if raw is None else tuple(json.loads(raw))


def _memory_from_row(r: sqlite3.Row) -> MemoryRow:
    return MemoryRow(
        id=r["id"], owner=r["owner"], kind=r["kind"], text=r["text"], t_event=r["t_event"],
        t_created=r["t_created"], t_accessed=r["t_accessed"], access_n=r["access_n"],
        actors=tuple(json.loads(r["actors"])), place=r["place"], boss_id=r["boss_id"],
        raid_id=r["raid_id"], importance=r["importance"], valence=r["valence"],
        source=r["source"], evidence=tuple(json.loads(r["evidence"])),
        confidence=r["confidence"], status=r["status"], superseded_by=r["superseded_by"],
        key=r["key"], merged_n=r["merged_n"], embedding=r["embedding"])


def _chat_from_row(r: sqlite3.Row) -> ChatLineRow:
    return ChatLineRow(
        id=r["id"], ts=r["ts"], channel=r["channel"], from_guid=r["from_guid"],
        from_name=r["from_name"], to_guid=r["to_guid"], text=r["text"], map=r["map"],
        zone=r["zone"], session_id=r["session_id"], thread_id=r["thread_id"],
        origin=r["origin"], audience=_guids(r["audience"]))


def _check_vec(v: bytes) -> bytes:
    if len(v) != VEC_DIM * 4:
        raise ValueError(f"embedding must be {VEC_DIM} float32 values, got {len(v)} bytes")
    return v


class Store:
    """All of mind.db. Times are mind-clock unix seconds (see migrations/0001_init.sql).
    Every write runs in `with self.transaction():`; never await inside one."""

    def __init__(self, conn: sqlite3.Connection, *, clock: Clock):
        self.conn = conn
        self.clock: Clock = clock
        self._depth = 0

    # -- transactions ------------------------------------------------------------------

    @contextmanager
    def transaction(self):
        """The outermost block commits on a clean exit and rolls back on an exception. An inner
        block joins it through a savepoint, so a failure the caller catches undoes only the
        inner writes and the outer unit still commits atomically."""
        if self._depth == 0:
            self._depth = 1
            try:
                yield
            except BaseException:
                self.conn.rollback()
                raise
            else:
                try:
                    self.conn.commit()
                except BaseException:
                    self.conn.rollback()
                    raise
            finally:
                self._depth = 0
        else:
            name = f"sp{self._depth}"
            self.conn.execute(f"SAVEPOINT {name}")
            self._depth += 1
            try:
                yield
            except BaseException:
                try:
                    self.conn.execute(f"ROLLBACK TO {name}")
                    self.conn.execute(f"RELEASE {name}")
                except sqlite3.OperationalError:
                    pass  # SQLite already rolled the whole transaction back; keep the original error
                raise
            else:
                self.conn.execute(f"RELEASE {name}")
            finally:
                self._depth -= 1

    def _all(self, sql: str, params=()) -> list[sqlite3.Row]:
        rows = self.conn.execute(sql, params).fetchall()
        if self._depth == 0:
            self.conn.commit()  # end the read transaction so it never pins a WAL snapshot
        return rows

    def _one(self, sql: str, params=()):
        rows = self._all(sql, params)
        return rows[0] if rows else None

    def close(self) -> None:
        """Checkpoint and truncate the WAL, then close. Not allowed inside a transaction."""
        if self._depth != 0:
            raise RuntimeError("Store.close() called inside a transaction")
        try:
            db.checkpoint(self.conn)
        finally:
            self.conn.close()

    # -- meta --------------------------------------------------------------------------

    def get_meta(self, key: str) -> str | None:
        r = self._one("SELECT value FROM meta WHERE key = ?", (key,))
        return r["value"] if r else None

    def set_meta(self, key: str, value: str) -> None:
        with self.transaction():
            self.conn.execute(
                "INSERT INTO meta(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, clean(value)))

    # -- sessions ----------------------------------------------------------------------

    def start_session(self, ts: float) -> int:
        with self.transaction():
            return self.conn.execute("INSERT INTO session(started) VALUES (?)", (ts,)).lastrowid

    def end_session(self, id: int, ts: float) -> None:
        with self.transaction():
            self.conn.execute("UPDATE session SET ended = ? WHERE id = ?", (ts, id))

    def open_session(self) -> int | None:
        r = self._one("SELECT id FROM session WHERE ended IS NULL ORDER BY id DESC LIMIT 1")
        return r["id"] if r else None

    def latest_session(self) -> int | None:
        """The newest session, open or ended (ids only grow), or None before the first."""
        r = self._one("SELECT max(id) AS id FROM session")
        return None if r is None else r["id"]

    def session_last_activity(self, id: int) -> float:
        """The last time anything was recorded in the session (a chat line, a game event or an
        utterance), else its start; 0.0 for an unknown session."""
        r = self._one(
            "SELECT max(t) AS t FROM ("
            "SELECT max(ts) AS t FROM chat_line WHERE session_id = ? UNION ALL "
            "SELECT max(ts) FROM game_event WHERE session_id = ? UNION ALL "
            "SELECT max(ts) FROM utterance WHERE session_id = ? UNION ALL "
            "SELECT started FROM session WHERE id = ?)", (id, id, id, id))
        return float(r["t"]) if r is not None and r["t"] is not None else 0.0

    # -- cast --------------------------------------------------------------------------

    def upsert_cast(self, members, ts: float) -> None:
        with self.transaction():
            for m in members:
                self.conn.execute(
                    "INSERT INTO cast_member(guid, name, race, class, gender, level, rank, "
                    "online, map, zone, spec_tab, updated) VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(guid) DO UPDATE SET name=excluded.name, race=excluded.race, "
                    "class=excluded.class, gender=excluded.gender, level=excluded.level, "
                    "rank=excluded.rank, online=excluded.online, map=excluded.map, "
                    "zone=excluded.zone, spec_tab=excluded.spec_tab, updated=excluded.updated",
                    (m.guid, clean(m.name), m.race, m.class_, m.gender, m.level, m.rank,
                     int(m.online), m.map, m.zone, m.spec_tab, ts))

    def replace_cast(self, members, ts: float, *, keep=()) -> list[int]:
        """The bridge's whole cast (a Cast event: mod-leeroy sends every member each time):
        its members are upserted and every other row goes, but the guids in `keep` (his: the
        bridge never sends him, and human_identity reads his row). An empty snapshot, from a
        bridge that hasn't loaded the guild yet, deletes nothing. Only the roster changes: the
        personas, memories and relationships of a member who left stay (fix wave G6). The
        guids deleted, in order."""
        members = list(members)
        with self.transaction():
            self.upsert_cast(members, ts)
            if not members:
                return []
            stay = json.dumps(sorted({m.guid for m in members} | set(keep)))
            gone = [r["guid"] for r in self._all(
                "SELECT guid FROM cast_member WHERE guid NOT IN (SELECT value FROM json_each(?)) "
                "ORDER BY guid", (stay,))]
            self.conn.execute("DELETE FROM cast_member WHERE guid NOT IN "
                              "(SELECT value FROM json_each(?))", (stay,))
            return gone

    def cast(self) -> dict[int, Member]:
        return {
            r["guid"]: Member(
                guid=r["guid"], name=r["name"], race=r["race"], class_=r["class"],
                gender=r["gender"], level=r["level"], rank=r["rank"], online=bool(r["online"]),
                map=r["map"], zone=r["zone"], spec_tab=r["spec_tab"])
            for r in self._all("SELECT * FROM cast_member ORDER BY guid")}

    def set_online(self, guid: int, online: bool) -> None:
        with self.transaction():
            self.conn.execute("UPDATE cast_member SET online = ? WHERE guid = ?",
                              (int(online), guid))

    # -- chat log ----------------------------------------------------------------------

    def insert_chat_line(self, ev: Chat, *, session_id: int | None, origin: str,
                         audience=None, ts: float | None = None) -> int:
        """Stamps ts from the injected clock (mind seconds), or `ts` when given: the app gives
        a line the bridge replayed after a restart the time it was said (MindApp.is_backlog).
        The server's ms ts is not kept otherwise. `audience` is the cast guids who could see
        the line (stored sorted; None: unknown)."""
        seen = None if audience is None else _j(sorted(set(audience)))
        with self.transaction():
            return self.conn.execute(
                "INSERT INTO chat_line(ts, channel, from_guid, from_name, to_guid, text, map, "
                "zone, session_id, origin, audience) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (self.clock.now() if ts is None else ts, clean(ev.channel), ev.from_guid,
                 clean(ev.from_name), ev.to_guid, clean(ev.text), ev.map, ev.zone, session_id,
                 origin, seen)).lastrowid

    def chat_lines(self, ids) -> list[ChatLineRow]:
        ids = list(ids)
        if not ids:
            return []
        marks = ",".join("?" * len(ids))
        return [_chat_from_row(r) for r in
                self._all(f"SELECT * FROM chat_line WHERE id IN ({marks}) ORDER BY id", ids)]

    def lines_before(self, line_id: int, channels, *, since: float, from_guid: int = 0,
                     limit: int = 50) -> list[ChatLineRow]:
        """The lines on `channels` stored before line `line_id` and no older than `since`
        (mind seconds), newest first, at most `limit`; with `from_guid`, only theirs. Every
        origin: his lines, the mind's and the game's (fix wave G6: the pushback window)."""
        who = " AND from_guid = ?" if from_guid else ""
        return [_chat_from_row(r) for r in self._all(
            "SELECT * FROM chat_line WHERE id < ? AND ts >= ? AND channel IN "
            f"(SELECT value FROM json_each(?)){who} ORDER BY id DESC LIMIT ?",
            (line_id, since, json.dumps(sorted(set(channels))),
             *((from_guid,) if from_guid else ()), limit))]

    def recent_mind_lines(self, speaker_guid: int, n: int, *, model_only: bool = True
                          ) -> list[tuple[str, int, str]]:
        """The member's last `n` turns said in chat, newest last, in the shape the talk
        pipeline keeps its own (Pipeline._own): (channel, to_guid, the turn's lines joined by a
        space). After a restart they seed the speaker's "Your last lines" and repeat check
        (prompts minor 15); whispers among them are for the caller to keep to the same person,
        as it does with its own.
        - model_only: the turns the model wrote (llm_s set), as _own keeps; not a template's
          (a gz, a kill cheer, say_as).
        - A turn is the rows of one turn_id. Ids start at t1 again in each run of the mind, so
          the same id on another channel or more than TURN_GAP_S apart is another turn, and a
          row with no turn_id is a turn of its own.
        - The text is as it went out, typos and all; _own keeps the words before styling."""
        if n <= 0:
            return []
        rows = self._all(
            "SELECT ts, channel, to_guid, text, turn_id FROM utterance WHERE guid = ? "
            "AND dropped IS NULL" + (" AND llm_s IS NOT NULL" if model_only else "")
            + " ORDER BY id DESC LIMIT ?", (speaker_guid, MAX_TURN_LINES * (n + 1)))
        turns: list[list[sqlite3.Row]] = []
        for r in rows:  # newest first
            later = turns[-1][-1] if turns else None
            if (later is not None and r["turn_id"] is not None
                    and r["turn_id"] == later["turn_id"]
                    and (r["channel"], r["to_guid"]) == (later["channel"], later["to_guid"])
                    and later["ts"] - r["ts"] <= TURN_GAP_S):
                turns[-1].append(r)
            else:
                turns.append([r])
        return [(t[0]["channel"], t[0]["to_guid"], " ".join(r["text"] for r in reversed(t)))
                for t in reversed(turns[:n])]

    def chat_line_parties(self, ids) -> dict[int, tuple[str, int, int]]:
        """(channel, from_guid, to_guid) of each stored line among `ids`, in one query however
        many ids there are (they go in as one JSON list). An id with no line is left out."""
        ids = sorted(set(ids))
        if not ids:
            return {}
        return {r["id"]: (r["channel"], r["from_guid"], r["to_guid"]) for r in self._all(
            "SELECT id, channel, from_guid, to_guid FROM chat_line "
            "WHERE id IN (SELECT value FROM json_each(?))", (json.dumps(ids),))}

    # -- threads -----------------------------------------------------------------------

    def open_thread(self, key: str, ts: float) -> int:
        with self.transaction():
            return self.conn.execute(
                "INSERT INTO chat_thread(key, opened) VALUES (?, ?)", (clean(key), ts)).lastrowid

    def set_thread(self, line_ids, thread_id: int) -> None:
        with self.transaction():
            self.conn.executemany("UPDATE chat_line SET thread_id = ? WHERE id = ?",
                                  [(thread_id, i) for i in line_ids])

    def close_thread(self, id: int, ts: float, status: str) -> None:
        with self.transaction():
            self.conn.execute("UPDATE chat_thread SET closed = ?, status = ? WHERE id = ?",
                              (ts, status, id))

    def thread_status(self, id: int) -> str | None:
        r = self._one("SELECT status FROM chat_thread WHERE id = ?", (id,))
        return None if r is None else r["status"]

    def thread_closed(self, id: int) -> float | None:
        r = self._one("SELECT closed FROM chat_thread WHERE id = ?", (id,))
        return None if r is None else r["closed"]

    def set_thread_status(self, id: int, ts: float, status: str) -> None:
        """Set the status without moving a close time the tracker already recorded."""
        with self.transaction():
            self.conn.execute(
                "UPDATE chat_thread SET status = ?, closed = COALESCE(closed, ?) WHERE id = ?",
                (status, ts, id))

    def pend_open_threads(self, ts: float) -> list[int]:
        """Threads still 'open' become 'pending' (closed at `ts` unless they had a close time),
        for threads_pending. A ThreadTracker never outlives its process, so at start every open
        row is one a crash left behind. Returns their ids."""
        with self.transaction():
            ids = [r["id"] for r in self.conn.execute(
                "SELECT id FROM chat_thread WHERE status = 'open' ORDER BY id").fetchall()]
            if ids:
                self.conn.execute("UPDATE chat_thread SET status = 'pending', "
                                  "closed = COALESCE(closed, ?) WHERE status = 'open'", (ts,))
            return ids

    def threads_pending(self) -> list[int]:
        """Threads P1c still has to extract: status 'pending' or 'failed'."""
        return [r["id"] for r in self._all(
            "SELECT id FROM chat_thread WHERE status IN ('pending','failed') ORDER BY id")]

    # -- game events -------------------------------------------------------------------

    def insert_game_event(self, ev, *, session_id: int | None, ts: float | None = None) -> int:
        """ts is the mind clock (seconds), or `ts` when given (a replayed event's own time, as
        for chat lines). The server's ms ts is also kept in the payload JSON."""
        with self.transaction():
            guid = getattr(ev, "guid", None)
            return self.conn.execute(
                "INSERT INTO game_event(ts, type, guid, payload, session_id) VALUES (?,?,?,?,?)",
                (self.clock.now() if ts is None else ts, protocol.event_type(ev), guid,
                 _j(protocol._jsonable(ev)), session_id)).lastrowid

    # -- memory: writes ----------------------------------------------------------------

    def _memory_params(self, m: MemoryRow) -> tuple:
        return (m.owner, m.kind, clean(m.text), m.t_event, m.t_created, m.t_accessed, m.access_n,
                _j(list(m.actors)), None if m.place is None else clean(m.place), m.boss_id,
                m.raid_id, m.importance, m.valence, m.source, _j(list(m.evidence)),
                m.confidence, m.status, m.superseded_by,
                None if m.key is None else clean(m.key), m.merged_n,
                None if m.embedding is None else _check_vec(m.embedding))

    _MEMORY_COLS = ("owner, kind, text, t_event, t_created, t_accessed, access_n, actors, place, "
                    "boss_id, raid_id, importance, valence, source, evidence, confidence, status, "
                    "superseded_by, key, merged_n, embedding")

    def _index_actors(self, mid: int, actors) -> None:
        self.conn.execute("DELETE FROM memory_actor WHERE memory_id = ?", (mid,))
        self.conn.executemany("INSERT OR IGNORE INTO memory_actor(memory_id, guid) VALUES (?, ?)",
                              [(mid, g) for g in actors])

    def _index_vec(self, mid: int, owner: int, vec: bytes | None) -> None:
        self.conn.execute("DELETE FROM memory_vec WHERE memory_id = ?", (mid,))
        if vec is not None:
            self.conn.execute(
                "INSERT INTO memory_vec(memory_id, owner, embedding) VALUES (?, ?, ?)",
                (mid, owner, vec))

    def insert_memory(self, m: MemoryRow) -> int:
        with self.transaction():
            marks = ",".join("?" * 21)
            mid = self.conn.execute(
                f"INSERT INTO memory({self._MEMORY_COLS}) VALUES ({marks})",
                self._memory_params(m)).lastrowid
            self._index_actors(mid, m.actors)
            if m.embedding is not None:
                self._index_vec(mid, m.owner, m.embedding)
            return mid

    def upsert_keyed_memory(self, m: MemoryRow) -> int:
        """Insert, or update in place the owner's memory with the same key (text, times, scores,
        evidence, actors and embedding are replaced; access_n and merged_n are kept). With no
        embedding in `m` the stale one is dropped so the memory gets re-embedded."""
        if m.key is None:
            raise ValueError("upsert_keyed_memory needs a key")
        with self.transaction():
            r = self._one("SELECT id FROM memory WHERE owner = ? AND key = ?", (m.owner, clean(m.key)))
            if r is None:
                return self.insert_memory(m)
            mid = r["id"]
            p = self._memory_params(m)
            self.conn.execute(
                "UPDATE memory SET kind=?, text=?, t_event=?, t_accessed=?, actors=?, place=?, "
                "boss_id=?, raid_id=?, importance=?, valence=?, source=?, evidence=?, "
                "confidence=?, status=?, embedding=? WHERE id = ?",
                (p[1], p[2], p[3], p[5], p[7], p[8], p[9], p[10], p[11], p[12], p[13], p[14],
                 p[15], p[16], p[20], mid))
            self._index_actors(mid, m.actors)
            self._index_vec(mid, m.owner, m.embedding)
            return mid

    def merge_memory(self, id: int, evidence, importance: int) -> None:
        """Fold a duplicate into memory `id`: evidence is unioned, importance takes the max,
        merged_n counts up."""
        with self.transaction():
            r = self._one("SELECT evidence, importance FROM memory WHERE id = ?", (id,))
            if r is None:
                raise KeyError(f"no memory {id}")
            ev = list(json.loads(r["evidence"]))
            ev += [e for e in dict.fromkeys(evidence) if e not in ev]
            self.conn.execute(
                "UPDATE memory SET evidence = ?, importance = ?, merged_n = merged_n + 1 "
                "WHERE id = ?", (_j(ev), max(r["importance"], importance), id))

    def set_embedding(self, id: int, vec: bytes) -> None:
        with self.transaction():
            r = self._one("SELECT owner FROM memory WHERE id = ?", (id,))
            if r is None:
                raise KeyError(f"no memory {id}")
            self.conn.execute("UPDATE memory SET embedding = ? WHERE id = ?", (_check_vec(vec), id))
            self._index_vec(id, r["owner"], vec)

    def touch_memories(self, ids, ts: float) -> None:
        with self.transaction():
            self.conn.executemany(
                "UPDATE memory SET t_accessed = ?, access_n = access_n + 1 WHERE id = ?",
                [(ts, i) for i in ids])

    # -- memory: reads -----------------------------------------------------------------

    def active_memory_count(self) -> int:
        """How many memories are active (fix wave G3: when the guild's word counts change)."""
        r = self._one("SELECT count(*) AS n FROM memory WHERE status = 'active'")
        return int(r["n"]) if r is not None else 0

    def active_memory_texts(self) -> list[str]:
        """Every active memory's text: how rare a word is across the guild's memories."""
        return [r["text"] for r in self._all("SELECT text FROM memory WHERE status = 'active'")]

    def get_memories(self, ids) -> list[MemoryRow]:
        ids = list(ids)
        if not ids:
            return []
        marks = ",".join("?" * len(ids))
        by_id = {r["id"]: _memory_from_row(r)
                 for r in self._all(f"SELECT * FROM memory WHERE id IN ({marks})", ids)}
        return [by_id[i] for i in ids if i in by_id]

    def keyed_memory(self, owner: int, key: str) -> MemoryRow | None:
        """The owner's memory with this key (upsert_keyed_memory's), if there is one."""
        r = self._one("SELECT * FROM memory WHERE owner = ? AND key = ?", (owner, clean(key)))
        return None if r is None else _memory_from_row(r)

    def memories_missing_embedding(self, limit: int, exclude=()) -> list[MemoryRow]:
        """Active memories without a vector, oldest first, leaving out the ids in `exclude`."""
        return [_memory_from_row(r) for r in self._all(
            "SELECT * FROM memory WHERE embedding IS NULL AND status = 'active' "
            "AND id NOT IN (SELECT value FROM json_each(?)) ORDER BY id LIMIT ?",
            (json.dumps(sorted(exclude)), limit))]

    def memories_with_actors(self, owner: int, guids, limit: int = 200) -> list[MemoryRow]:
        """The owner's active memories that involve any of `guids`, newest first."""
        guids = list(guids)
        if not guids:
            return []
        marks = ",".join("?" * len(guids))
        return [_memory_from_row(r) for r in self._all(
            f"SELECT * FROM memory WHERE owner = ? AND status = 'active' AND id IN "
            f"(SELECT memory_id FROM memory_actor WHERE guid IN ({marks})) "
            f"ORDER BY t_created DESC, id DESC LIMIT ?", (owner, *guids, limit))]

    def recent_similar_candidates(self, owner: int, kind: str, actors, since: float
                                  ) -> list[MemoryRow]:
        """Dedupe candidates: the owner's active memories of `kind` created at or after `since`
        (unix seconds) whose actor set is exactly `actors` (no actors: only actor-less rows)."""
        actors = sorted(set(actors))
        sql = ("SELECT * FROM memory WHERE owner = ? AND kind = ? AND status = 'active' "
               "AND t_created >= ?")
        params: list = [owner, kind, since]
        if actors:
            n = len(actors)
            sql += (" AND id IN (SELECT memory_id FROM memory_actor GROUP BY memory_id "
                    "HAVING count(*) = ? AND sum(guid IN (%s)) = ?)" % ",".join("?" * n))
            params += [n, *actors, n]
        else:
            sql += " AND NOT EXISTS (SELECT 1 FROM memory_actor WHERE memory_id = memory.id)"
        return [_memory_from_row(r) for r in self._all(sql + " ORDER BY t_created DESC, id DESC",
                                                       params)]

    def count_inserted_since(self, owner: int, ts: float) -> int:
        return self._one("SELECT count(*) AS n FROM memory WHERE owner = ? AND t_created >= ?",
                         (owner, ts))["n"]

    # -- search ------------------------------------------------------------------------

    def knn(self, owner: int, vec: bytes, k: int = 50) -> list[tuple[int, float]]:
        """Nearest memories of `owner` as (memory_id, cosine distance). Not filtered by status."""
        rows = self._all(
            "SELECT memory_id, distance FROM memory_vec "
            "WHERE embedding MATCH ? AND k = ? AND owner = ?", (_check_vec(vec), k, owner))
        return [(r["memory_id"], r["distance"]) for r in rows]

    def fts(self, owner: int, query: str, k: int = 20) -> list[tuple[int, float]]:
        """Active memories of `owner` matching an FTS5 expression, as (memory_id, bm25) with the
        best (lowest) first. A malformed expression returns [] and is logged; build safe ones
        from free text with fts_query()."""
        return self._fts(query, k, owner)

    def fts_all(self, query: str, k: int = 40) -> list[tuple[int, float]]:
        """fts over every owner's active memories at once: who holds a memory on a line (fix
        wave G3)."""
        return self._fts(query, k, None)

    def _fts(self, query: str, k: int, owner: int | None) -> list[tuple[int, float]]:
        if not query.strip():
            return []
        mine = "" if owner is None else "AND m.owner = ? "
        try:
            rows = self._all(
                "SELECT m.id AS id, bm25(memory_fts) AS score FROM memory_fts "
                "JOIN memory m ON m.id = memory_fts.rowid "
                f"WHERE memory_fts MATCH ? {mine}AND m.status = 'active' "
                "ORDER BY score LIMIT ?",
                (clean(query), k) if owner is None else (clean(query), owner, k))
        except sqlite3.OperationalError as e:
            log.warning("fts query %r rejected: %s", query, e)
            if self._depth == 0:
                self.conn.rollback()
            return []
        return [(r["id"], r["score"]) for r in rows]

    # -- relationships -----------------------------------------------------------------

    def get_relationship(self, a: int, b: int) -> Relationship:
        r = self._one("SELECT * FROM relationship WHERE a = ? AND b = ?", (a, b))
        if r is None:
            return Relationship(a=a, b=b)
        return Relationship(
            a=a, b=b, affinity=r["affinity"], trust=r["trust"], respect=r["respect"],
            attraction=r["attraction"], familiarity=r["familiarity"], label=r["label"],
            romance_stage=r["romance_stage"], notes=r["notes"],
            pinned=tuple(json.loads(r["pinned"])), last_seen=r["last_seen"],
            interactions=r["interactions"])

    def put_relationship(self, r: Relationship) -> None:
        """Scores are clamped to REL_RANGES and never rounded."""
        scores = {d: min(max(float(getattr(r, d)), REL_RANGES[d][0]), REL_RANGES[d][1])
                  for d in REL_DIMS}
        with self.transaction():
            self.conn.execute(
                "INSERT OR REPLACE INTO relationship(a, b, affinity, trust, respect, attraction, "
                "familiarity, label, romance_stage, notes, pinned, last_seen, interactions) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (r.a, r.b, scores["affinity"], scores["trust"], scores["respect"],
                 scores["attraction"], scores["familiarity"], clean(r.label), r.romance_stage,
                 clean(r.notes), _j(list(r.pinned)), r.last_seen, r.interactions))

    def add_rel_delta(self, a: int, b: int, dim: str, delta: float, reason: str, evidence,
                      ts: float, *, applied: bool) -> int:
        if dim not in REL_DIMS and dim != "romance_stage":
            raise ValueError(f"unknown relationship dimension {dim!r}")
        with self.transaction():
            return self.conn.execute(
                "INSERT INTO rel_delta(a, b, dim, delta, reason, evidence, ts, applied) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (a, b, dim, delta, clean(reason), _j(list(evidence)), ts,
                 int(applied))).lastrowid

    def pending_rel_deltas(self) -> list[sqlite3.Row]:
        return self._all("SELECT * FROM rel_delta WHERE applied = 0 ORDER BY id")

    def note_interaction(self, a: int, b: int, ts: float) -> None:
        with self.transaction():
            self.conn.execute(
                "INSERT INTO relationship(a, b, last_seen, interactions) VALUES (?, ?, ?, 1) "
                "ON CONFLICT(a, b) DO UPDATE SET last_seen = max(last_seen, excluded.last_seen), "
                "interactions = interactions + 1", (a, b, ts))

    # -- personas ----------------------------------------------------------------------

    def put_persona(self, guid: int, json: str, card: str, card_tokens: int, source: str,
                    ts: float) -> int:
        """Store a persona and return its version (1, then +1 per write)."""
        with self.transaction():
            r = self._one("SELECT version FROM persona_core WHERE guid = ?", (guid,))
            version = (r["version"] + 1) if r else 1
            self.conn.execute(
                "INSERT OR REPLACE INTO persona_core(guid, json, card, card_tokens, version, "
                "source, updated) VALUES (?,?,?,?,?,?,?)",
                (guid, clean(json), clean(card), card_tokens, version, source, ts))
            return version

    def get_persona_json(self, guid: int) -> str | None:
        r = self._one("SELECT json FROM persona_core WHERE guid = ?", (guid,))
        return r["json"] if r else None

    def get_card(self, guid: int) -> str | None:
        r = self._one("SELECT card FROM persona_core WHERE guid = ?", (guid,))
        return r["card"] if r else None

    def persona_versions(self) -> dict[int, int]:
        return {r["guid"]: r["version"]
                for r in self._all("SELECT guid, version FROM persona_core")}

    def get_state(self, guid: int) -> PersonaStateRow:
        r = self._one("SELECT * FROM persona_state WHERE guid = ?", (guid,))
        if r is None:
            return PersonaStateRow(guid=guid)
        return PersonaStateRow(
            guid=guid, mood=r["mood"], mood_baseline=r["mood_baseline"],
            goals=tuple(json.loads(r["goals"])), self_notes=tuple(json.loads(r["self_notes"])),
            altered_state=r["altered_state"], state_since=r["state_since"],
            state_session=r["state_session"], life_thread=r["life_thread"], updated=r["updated"])

    def put_state(self, s: PersonaStateRow) -> None:
        with self.transaction():
            self.conn.execute(
                "INSERT OR REPLACE INTO persona_state(guid, mood, mood_baseline, goals, "
                "self_notes, altered_state, state_since, state_session, life_thread, updated) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (s.guid, s.mood, s.mood_baseline, _j(list(s.goals)), _j(list(s.self_notes)),
                 clean(s.altered_state), s.state_since, s.state_session, clean(s.life_thread),
                 s.updated))

    # -- talk bookkeeping --------------------------------------------------------------

    def record_bit(self, guid: int, bit_id: str, ts: float, night: str, voice_bit: bool,
                   session_id: int | None) -> None:
        with self.transaction():
            self.conn.execute(
                "INSERT INTO bit_use(guid, bit_id, ts, night, voice_bit, session_id) "
                "VALUES (?,?,?,?,?,?)",
                (guid, clean(bit_id), ts, night, int(voice_bit), session_id))

    def last_bit_use(self, guid: int, bit_id: str) -> int | None:
        r = self._one("SELECT max(ts) AS t FROM bit_use WHERE guid = ? AND bit_id = ?",
                      (guid, bit_id))
        return r["t"]

    def voice_bit_used_on(self, night: str) -> bool:
        return self._one("SELECT 1 FROM bit_use WHERE night = ? AND voice_bit = 1 LIMIT 1",
                         (night,)) is not None

    def log_utterance(self, ts: float, guid: int, channel: str, kind: str, text: str,
                      memory_ids, dropped: str | None, session_id: int | None, *,
                      bit: str | None = None, why: str | None = None,
                      trigger_line: int | None = None, llm_s: float | None = None,
                      to_guid: int = 0, turn_id: str | None = None) -> int:
        """`bit` is the BitScheduler bit the turn brought out, `why` how the turn came about
        (talk.pipeline.TurnMeta.why), `trigger_line` the chat line it answered
        (talk.turns.Turn.trigger_line), `llm_s` the model's seconds on it (TurnMeta.llm_s; None
        for a template), `to_guid` whom a whisper went to and `turn_id` the turn the line
        belongs to (TurnMeta.turn_id)."""
        with self.transaction():
            return self.conn.execute(
                "INSERT INTO utterance(ts, guid, channel, kind, text, memory_ids, dropped, "
                "session_id, bit, why, trigger_line, llm_s, to_guid, turn_id) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (ts, guid, clean(channel), clean(kind), clean(text), _j(list(memory_ids)),
                 None if dropped is None else clean(dropped), session_id,
                 None if bit is None else clean(bit), None if why is None else clean(why),
                 trigger_line, llm_s, to_guid, None if turn_id is None else clean(turn_id))
            ).lastrowid

    def talk_stats(self, since: float, until: float) -> dict:
        """The utterance log in (since, until], for the stats line (MindApp._stats): the lines
        said by turn kind, the drops by reason, and the model's seconds on every row that has
        them, sorted. Counts, never text."""
        rows = self._all("SELECT kind, dropped, llm_s FROM utterance WHERE ts > ? AND ts <= ?",
                         (since, until))
        said: dict[str, int] = {}
        drops: dict[str, int] = {}
        for r in rows:
            if r["dropped"] is None:
                said[r["kind"]] = said.get(r["kind"], 0) + 1
            else:
                drops[r["dropped"]] = drops.get(r["dropped"], 0) + 1
        return {"said": dict(sorted(said.items())), "drops": dict(sorted(drops.items())),
                "llm_s": sorted(r["llm_s"] for r in rows if r["llm_s"] is not None)}

    def human_lines(self, since: float, until: float) -> list[tuple[str, str]]:
        """(channel, text) of the human's own lines in (since, until], oldest first: the stats
        line tells his talk from his bot commands by them (it logs only the counts)."""
        return [(r["channel"], r["text"]) for r in self._all(
            "SELECT channel, text FROM chat_line WHERE origin = 'human' AND ts > ? AND ts <= ? "
            "ORDER BY ts, id", (since, until))]

    def drops_since(self, since: float, reasons) -> list[tuple[float, str]]:
        """(ts, reason) of each turn dropped after `since` for one of `reasons`, oldest first."""
        reasons = list(reasons)
        if not reasons:
            return []
        marks = ",".join("?" * len(reasons))
        return [(r["ts"], r["dropped"]) for r in self._all(
            f"SELECT ts, dropped FROM utterance WHERE ts > ? AND dropped IN ({marks}) "
            "ORDER BY ts, id", (since, *reasons))]
