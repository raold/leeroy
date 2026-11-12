"""Profile photo sets and picks in mind.db (migrations/*_selfies.sql). The images are files
(files.py); these rows say which sets exist, what each candidate was rendered from, and which
photo is in force.

- A set is queued for the night's render (at most one a member), then ready with four
  candidates. A ready set waits for his choice until a photo comes from it (`picked`) or he asks
  for four more (`passed`).
- Every pick and undo is one more selfie_pick row; selfie_current is kept in the same
  transaction. An undo works only while the photo in force is still what that change left,
  as the tuning's does, and is itself undoable.

Each writer runs in `store.transaction()`. A refusal is SelfieError, its text the answer. The
mind writes; the console reads (`picks` and `counts` take a plain connection for that)."""
from dataclasses import dataclass

SOURCES = ("console", "cli")
SLOTS = (0, 1, 2, 3)
_SET_COLS = "guid, set_no, reason, status, requested, rendered, night, persona_version, decided"


class SelfieError(ValueError):
    """A photo change the store refuses; its text is the answer, in plain words."""


@dataclass(frozen=True)
class SetRow:
    guid: int
    set_no: int
    reason: str  # 'first' or 'more'
    status: str  # 'queued' or 'ready'
    requested: float
    rendered: float | None = None
    night: str | None = None
    persona_version: int | None = None
    decided: str | None = None  # None (waiting), 'picked' or 'passed'


@dataclass(frozen=True)
class Candidate:
    idx: int
    seed: int
    tries: int
    nsfw: float
    prompt: str


@dataclass(frozen=True)
class Current:
    guid: int
    set_no: int
    idx: int
    pick: int  # the change that put this photo in force


def _set_row(r) -> SetRow:
    return SetRow(*tuple(r))


def _read(store, sql: str, params=()) -> list:
    with store.transaction():  # ends the read, so it never pins a WAL snapshot
        return store.conn.execute(sql, params).fetchall()


def _source(source: str) -> None:
    if source not in SOURCES:
        raise SelfieError("source is console or cli")


def _photo(set_no, idx) -> tuple[int, int] | None:
    return None if set_no is None else (set_no, idx)


def _current(conn, guid: int) -> Current | None:
    r = conn.execute("SELECT guid, set_no, idx, pick FROM selfie_current WHERE guid=?",
                     (guid,)).fetchone()
    return None if r is None else Current(*tuple(r))


def _next_set(conn, guid: int) -> int:
    return (conn.execute("SELECT max(set_no) FROM selfie_set WHERE guid=?",
                         (guid,)).fetchone()[0] or 0) + 1


def _new_set(conn, guid: int, reason: str, now: float) -> SetRow:
    set_no = _next_set(conn, guid)
    conn.execute("INSERT INTO selfie_set(guid, set_no, reason, status, requested) "
                 "VALUES (?, ?, ?, 'queued', ?)", (guid, set_no, reason, float(now)))
    return SetRow(guid, set_no, reason, "queued", float(now))


def _set_photo(conn, guid: int, photo: tuple[int, int] | None, change: int) -> None:
    if photo is None:
        conn.execute("DELETE FROM selfie_current WHERE guid=?", (guid,))
        return
    conn.execute("INSERT INTO selfie_current(guid, set_no, idx, pick) VALUES (?, ?, ?, ?) "
                 "ON CONFLICT(guid) DO UPDATE SET set_no=excluded.set_no, idx=excluded.idx, "
                 "pick=excluded.pick", (guid, photo[0], photo[1], change))


def _decide(conn, guid: int, set_no: int, decided: str | None) -> None:
    conn.execute("UPDATE selfie_set SET decided=? WHERE guid=? AND set_no=?",
                 (decided, guid, set_no))


# -- the render queue ---------------------------------------------------------------------------

def queued_sets(store) -> list[SetRow]:
    """The sets waiting for a render: his "4 more" first, then first photos, each by guid."""
    return [_set_row(r) for r in _read(
        store, f"SELECT {_SET_COLS} FROM selfie_set WHERE status='queued' "
        "ORDER BY reason='more' DESC, guid, set_no")]


def _needs_first(conn, guid: int) -> bool:
    """No photo, no queued set and no ready set waiting for his choice."""
    return conn.execute(
        "SELECT 1 FROM selfie_current WHERE guid=:g UNION ALL "
        "SELECT 1 FROM selfie_set WHERE guid=:g AND (status='queued' OR "
        "(status='ready' AND decided IS NULL)) LIMIT 1", {"g": guid}).fetchone() is None


def needing_first(store, guids) -> list[int]:
    """Those of `guids` that ensure_first_sets would queue a first set for, by guid."""
    with store.transaction():
        return [g for g in sorted(set(guids)) if _needs_first(store.conn, g)]


def ensure_first_sets(store, guids, *, now: float) -> int:
    """A first set queued for each of `guids` with no photo, no queued set and no ready set
    waiting for his choice; how many were queued."""
    made = 0
    with store.transaction():
        conn = store.conn
        for guid in sorted(set(guids)):
            if _needs_first(conn, guid):
                _new_set(conn, guid, "first", now)
                made += 1
    return made


def queue_more(store, guid: int, *, now: float) -> SetRow:
    """His "4 more": a set queued for tonight, and every set of his still waiting for his
    choice passed (after an undo, more than one can wait)."""
    with store.transaction():
        conn = store.conn
        if conn.execute("SELECT 1 FROM selfie_set WHERE guid=? AND status='queued'",
                        (guid,)).fetchone() is not None:
            raise SelfieError("four more are already queued for tonight")
        conn.execute("UPDATE selfie_set SET decided='passed' WHERE guid=? AND status='ready' "
                     "AND decided IS NULL", (guid,))
        return _new_set(conn, guid, "more", now)


def queue_restyled(store, guid: int, *, now: float) -> SetRow | None:
    """A set queued for a member whose photo kind was chosen again in another style (into the
    lust style), so new candidates come without his "4 more": none when one is already queued,
    a first set when she never had one, else a "more" one. Sets waiting for his choice stay
    waiting. Joins the caller's transaction."""
    with store.transaction():
        conn = store.conn
        if conn.execute("SELECT 1 FROM selfie_set WHERE guid=? AND status='queued'",
                        (guid,)).fetchone() is not None:
            return None
        had = conn.execute("SELECT 1 FROM selfie_set WHERE guid=? LIMIT 1", (guid,)).fetchone()
        return _new_set(conn, guid, "more" if had else "first", now)


def mark_ready(store, guid: int, set_no: int, candidates, *, now: float, night: str,
               persona_version: int | None, lust: bool = False) -> None:
    """A queued set rendered: its four candidates, one a slot, and the night and persona
    version it was rendered from."""
    candidates = list(candidates)
    if sorted(c.idx for c in candidates) != list(SLOTS):
        raise SelfieError("a ready set has four candidates, one for each slot 0-3")
    with store.transaction():
        conn = store.conn
        moved = conn.execute(
            "UPDATE selfie_set SET status='ready', rendered=?, night=?, persona_version=? "
            "WHERE guid=? AND set_no=? AND status='queued'",
            (float(now), night, persona_version, guid, set_no)).rowcount
        if moved != 1:
            raise SelfieError(f"set {set_no} is not queued for this member")
        conn.executemany(
            "INSERT INTO selfie_candidate(guid, set_no, idx, seed, tries, nsfw, prompt) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(guid, set_no, c.idx, c.seed, c.tries, float(c.nsfw), c.prompt)
             for c in candidates])
        if lust:
            store.set_meta(LUST_SET_KEY.format(guid=guid, set_no=set_no), "1")


def ready_set(store, guid: int, set_no: int) -> SetRow | None:
    rows = _read(store, f"SELECT {_SET_COLS} FROM selfie_set WHERE guid=? AND set_no=? "
                 "AND status='ready'", (guid, set_no))
    return _set_row(rows[0]) if rows else None


def current(store, guid: int) -> Current | None:
    with store.transaction():
        return _current(store.conn, guid)


def photos(store) -> dict[int, Current]:
    """Every member's photo in force, by guid."""
    return {r[0]: Current(*tuple(r)) for r in _read(
        store, "SELECT guid, set_no, idx, pick FROM selfie_current ORDER BY guid")}


def counts(conn) -> dict[str, int]:
    """Members with a photo in force, members with a set waiting for his choice, and sets
    queued for the night. Reads any connection, the console's read-only one too."""
    one = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
    from leeroy_mind.personas.tiers import cast_sql
    only = cast_sql(conn)
    return {"chosen": one("SELECT count(*) FROM selfie_current WHERE " + only),
            "waiting": one("SELECT count(DISTINCT guid) FROM selfie_set WHERE status='ready' "
                           "AND decided IS NULL AND " + only),
            "queued": one("SELECT count(*) FROM selfie_set WHERE status='queued' AND " + only)}


# -- the style a set was rendered under ---------------------------------------------------------

LUST_SET_KEY = "selfies:set-lust:{guid}:{set_no}"
LUST_COVER_REFUSED = ("this set was rendered in the lust style and she no longer has it (or her "
                      "looks now code young), so none of its photos can be her face")


def lust_set(conn, guid: int, set_no: int) -> bool:
    """The set was rendered under the lust style (recorded when it became ready; a set with no
    record, from before it, counts as plain). Reads any connection, the console's too."""
    try:
        return conn.execute("SELECT 1 FROM meta WHERE key=?", (LUST_SET_KEY.format(
            guid=guid, set_no=set_no),)).fetchone() is not None
    except Exception:  # a database without the meta table
        return False


# -- picks and undos ----------------------------------------------------------------------------

def pick(store, guid: int, set_no: int, idx: int, *, source: str, now: float,
         lust_held: bool = False) -> int | None:
    """Candidate `idx` of a ready set becomes the member's photo; the change's id, or None
    when it already is the photo (nothing written). Any rendered candidate can be a cover,
    except (`lust_held`: she no longer has the lust style) one of a set rendered under it.
    Remember a decided set's prior state so undo does not reopen it in the render queue."""
    _source(source)
    with store.transaction():
        conn = store.conn
        row = conn.execute("SELECT decided FROM selfie_set WHERE guid=? AND set_no=? AND "
                           "status='ready'", (guid, set_no)).fetchone()
        if row is None:
            raise SelfieError(f"set {set_no} is not ready for this member")
        if conn.execute("SELECT 1 FROM selfie_candidate WHERE guid=? AND set_no=? AND idx=?",
                        (guid, set_no, idx)).fetchone() is None:
            raise SelfieError(f"set {set_no} has no photo {idx}")
        if lust_held and lust_set(conn, guid, set_no):
            raise SelfieError(LUST_COVER_REFUSED)
        was = _current(conn, guid)
        before = None if was is None else (was.set_no, was.idx)
        if before == (set_no, idx):
            return None
        change = conn.execute(
            "INSERT INTO selfie_pick(ts, guid, source, before_set, before_idx, after_set, "
            "after_idx) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (float(now), guid, source, *(before or (None, None)), set_no, idx)).lastrowid
        if row["decided"] is not None:
            store.set_meta(f"selfie:pick-decided:{change}", row["decided"])
        _set_photo(conn, guid, (set_no, idx), change)
        _decide(conn, guid, set_no, "picked")
        return change


def _pick_root(conn, row):
    """The pick at the root of `row`'s undo chain."""
    while row["undoes"] is not None:
        earlier = conn.execute("SELECT id, undoes, after_set, after_idx FROM selfie_pick "
                               "WHERE id=?", (row["undoes"],)).fetchone()
        if earlier is None or earlier["id"] >= row["id"]:
            return None  # not a chain this store wrote
        row = earlier
    return row


def undo(store, pick_id: int, *, source: str, now: float) -> int:
    """Change `pick_id` reversed, as one more change naming it, provided the photo in force is
    still what it left. Undoing a pick restores its set's prior decision (waiting for older
    records), unless the restored photo comes from it. Undoing an undo applies the pick again.
    Every change remains an append-only selfie_pick row."""
    _source(source)
    with store.transaction():
        conn = store.conn
        row = conn.execute("SELECT id, guid, undoes, before_set, before_idx, after_set, "
                           "after_idx FROM selfie_pick WHERE id=?", (pick_id,)).fetchone()
        if row is None:
            raise SelfieError(f"there is no change #{pick_id}")
        guid = row["guid"]
        left = _photo(row["after_set"], row["after_idx"])
        was = _current(conn, guid)
        if (None if was is None else (was.set_no, was.idx)) != left:
            later = [r[0] for r in conn.execute(
                "SELECT id FROM selfie_pick WHERE guid=? AND id>? ORDER BY id", (guid, pick_id))]
            names = ", ".join(f"#{n}" for n in later) or "a later change"
            raise SelfieError(f"changed since by {names}: undo that first")
        restored = _photo(row["before_set"], row["before_idx"])
        change = conn.execute(
            "INSERT INTO selfie_pick(ts, guid, source, undoes, before_set, before_idx, "
            "after_set, after_idx) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (float(now), guid, source, pick_id, *(left or (None, None)),
             *(restored or (None, None)))).lastrowid
        _set_photo(conn, guid, restored, change)
        root = _pick_root(conn, row)
        if (left is not None and root is not None
                and left == _photo(root["after_set"], root["after_idx"])
                and (restored is None or restored[0] != left[0])):
            prior = store.get_meta(f"selfie:pick-decided:{root['id']}")
            _decide(conn, guid, left[0], prior if prior in ("passed", "picked") else None)
        if restored is not None:
            _decide(conn, guid, restored[0], "picked")
        return change


def member_of(store, change: int) -> int | None:
    """The member change #`change` was made for; None when there is no such change."""
    rows = _read(store, "SELECT guid FROM selfie_pick WHERE id=?", (change,))
    return rows[0][0] if rows else None


def picks(conn, guid: int | None = None, limit: int = 30) -> list[dict]:
    """The newest changes first (one member's, or everyone's), each with whether an undo of it
    would be taken now. Reads any connection, the console's read-only one too."""
    from leeroy_mind.personas.tiers import cast_sql
    where, params = ("WHERE guid=? ", (guid,)) if guid is not None else ("WHERE " + cast_sql(conn) + " ", ())
    rows = conn.execute(f"SELECT id, ts, guid, undoes, after_set, after_idx FROM selfie_pick "
                        f"{where}ORDER BY id DESC LIMIT ?", (*params, int(limit))).fetchall()
    now = {r[0]: (r[1], r[2]) for r in conn.execute(
        "SELECT guid, set_no, idx FROM selfie_current")}
    return [{"id": r[0], "ts": r[1], "guid": r[2],
             "label": "pick" if r[3] is None else f"undo #{r[3]}",
             "undoable": now.get(r[2]) == _photo(r[4], r[5])} for r in rows]
