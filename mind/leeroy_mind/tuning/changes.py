"""Writing the tuning: every owner action is one tuning_change row with the tuning_row values it
moved, and tuning_value is kept current in the same transaction.

- set: one value set or cleared (None). Setting what is already there writes nothing.
- undo of change N: one more change with the rows the other way round and `undoes` = N, provided
  every value N set is still what N left; otherwise it is refused and names the later changes
  that moved it. An undo is an ordinary change, so undoing it applies N again.
- revert to change N: the state the log had right after N, written as one change.
- group save and retire: a group's definition, as a row of its own (dial 'definition').

A world change or a change to "everyone" is one row however many members it reaches. What an
undo cannot undo is the guild's history: lines said, memories, jealousy accrued, feuds, rumours
and trait samples recorded while a value was in force.

Validation needs the live picture (the cast, the stored personas and the owner's own guid),
which the caller passes in; nothing here reads persona text or logs a name."""
import json
from dataclasses import dataclass

from . import groups as G
from .dials import MEMBER_DIALS, OFFSET_HI, OFFSET_LO, WORLD_DIALS, applies, bounds, not_applicable
from .state import Tuning

SOURCES = ("console", "cli")
DEFINITION = "definition"
SCOPES = ("world", "group", "member")


class TuneError(ValueError):
    """A change the tuning refuses; its text is the answer, in plain words."""


@dataclass(frozen=True)
class Set:
    scope: str
    target: str
    dial: str
    value: int | None


@dataclass(frozen=True)
class Picture:
    """What validation reads: cast guid -> Member, guid -> stored Persona (base), the owner's
    own guid and the stored persona versions."""
    cast: dict
    personas: dict
    human: int
    versions: dict


@dataclass(frozen=True)
class Result:
    change: int | None  # None: nothing changed, nothing written
    rows: int = 0


def picture(store, personas) -> Picture:
    """The live picture from the store and a {guid: stored persona} mapping."""
    human = store.get_meta("human_guid")
    return Picture(store.cast(), personas, int(human) if human and human.isdigit() else 0,
                   store.persona_versions())


# -- checking -----------------------------------------------------------------------------------

def _int(v) -> bool:
    return type(v) is int


def check_set(s: Set, pic: Picture, live_groups: set[str]) -> Set:
    """The set as it will be stored (an offset of 0 is a clear), or TuneError."""
    if s.scope not in SCOPES:
        raise TuneError("scope is world, group or member")
    v = s.value
    if v is not None and not _int(v):
        raise TuneError("a value is a whole number, or null to clear it")
    if s.scope == "world":
        d = WORLD_DIALS.get(s.dial)
        if d is None or s.target != "":
            raise TuneError("world dials: " + ", ".join(WORLD_DIALS))
        if v is not None and not d.lo <= v <= d.hi:
            raise TuneError(f"{d.label} runs {d.lo}-{d.hi}")
        return s
    if s.dial not in MEMBER_DIALS:
        raise TuneError("member dials: " + ", ".join(MEMBER_DIALS))
    if s.scope == "group":
        if s.target not in live_groups:
            raise TuneError("no such group (it may have been retired)")
        if v is not None and not OFFSET_LO <= v <= OFFSET_HI:
            raise TuneError(f"a group's offset runs {OFFSET_LO} to +{OFFSET_HI}")
        return Set(s.scope, s.target, s.dial, None if v == 0 else v)
    if not s.target.isdigit() or not 0 < int(s.target) < 1 << 32:
        raise TuneError("a member is one guid")
    guid = int(s.target)
    if guid == pic.human:
        raise TuneError("your own character is never tuned")
    p = pic.personas.get(guid)
    if guid not in pic.cast or p is None:
        raise TuneError("only cast members with a stored persona can be tuned")
    if v is not None:
        if not applies(s.dial, p):
            raise TuneError(not_applicable(s.dial, p))
        lo, hi = bounds(s.dial)
        if not lo <= v <= hi:
            raise TuneError(f"{MEMBER_DIALS[s.dial].label} runs {lo}-{hi}")
    return s


# -- writing ------------------------------------------------------------------------------------

def _current(conn, scope, target, dial):
    row = conn.execute("SELECT value FROM tuning_value WHERE scope=? AND target=? AND dial=?",
                       (scope, target, dial)).fetchone()
    return None if row is None else row[0]


def _new_change(conn, now, source, label, *, undoes=None, previewed=False) -> int:
    if source not in SOURCES:
        raise TuneError("source is console or cli")
    cur = conn.execute("INSERT INTO tuning_change(ts,source,label,undoes,previewed) "
                       "VALUES (?,?,?,?,?)",
                       (float(now), source, label[:120], undoes, 1 if previewed else 0))
    return cur.lastrowid


def _write_value(conn, change, scope, target, dial, before, after, version) -> None:
    conn.execute("INSERT INTO tuning_row(change,scope,target,dial,before,after,persona_version) "
                 "VALUES (?,?,?,?,?,?,?)", (change, scope, target, dial, before, after, version))
    if after is None:
        conn.execute("DELETE FROM tuning_value WHERE scope=? AND target=? AND dial=?",
                     (scope, target, dial))
    else:
        conn.execute("INSERT INTO tuning_value(scope,target,dial,value,change,persona_version) "
                     "VALUES (?,?,?,?,?,?) ON CONFLICT(scope,target,dial) DO UPDATE SET "
                     "value=excluded.value, change=excluded.change, "
                     "persona_version=excluded.persona_version",
                     (scope, target, dial, after, change, version))


def _version(pic: Picture, s: Set):
    return pic.versions.get(int(s.target)) if s.scope == "member" else None


def _stamp(conn, change: int) -> None:
    conn.execute("INSERT INTO meta(key,value) VALUES ('tuning_version',?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(change),))


def live_groups(conn) -> set[str]:
    return {G.EVERYONE} | {str(r[0]) for r in conn.execute(
        "SELECT id FROM tuning_group WHERE retired IS NULL")}


def apply_sets(store, sets: list[Set], pic: Picture, *, source: str, now: float,
               label: str = "set", previewed: bool = False) -> Result:
    """Check every set, then write them as one change (nothing when none moves a value)."""
    with store.transaction():
        conn = store.conn
        groups = live_groups(conn)
        checked = [check_set(s, pic, groups) for s in sets]
        moved = [(s, _current(conn, s.scope, s.target, s.dial)) for s in checked]
        moved = [(s, before) for s, before in moved if before != s.value]
        if not moved:
            return Result(None)
        change = _new_change(conn, now, source, label, previewed=previewed)
        for s, before in moved:
            _write_value(conn, change, s.scope, s.target, s.dial, before, s.value,
                         _version(pic, s))
        _stamp(conn, change)
        return Result(change, len(moved))


# -- groups -------------------------------------------------------------------------------------

def _definition(conn, gid: int) -> dict | None:
    row = conn.execute("SELECT name,predicate,retired FROM tuning_group WHERE id=?",
                       (gid,)).fetchone()
    if row is None or row[2] is not None:
        return None
    return {"name": row[0], "where": json.loads(row[1])}


def _set_definition(conn, gid: int, d: dict | None, now: float) -> None:
    if d is None:
        conn.execute("UPDATE tuning_group SET retired=? WHERE id=? AND retired IS NULL",
                     (float(now), gid))
        return
    taken = conn.execute("SELECT id FROM tuning_group WHERE name=? AND retired IS NULL AND id!=?",
                         (d["name"], gid)).fetchone()
    if taken is not None:
        raise TuneError(f"another group is already called {d['name']!r}")
    predicate = json.dumps(d["where"], sort_keys=True, separators=(",", ":"))
    if conn.execute("SELECT 1 FROM tuning_group WHERE id=?", (gid,)).fetchone() is None:
        conn.execute("INSERT INTO tuning_group(id,name,predicate,created) VALUES (?,?,?,?)",
                     (gid, d["name"], predicate, float(now)))
    else:
        conn.execute("UPDATE tuning_group SET name=?, predicate=?, retired=NULL WHERE id=?",
                     (d["name"], predicate, gid))


def _def_row(conn, change, gid, before, after) -> None:
    dump = (lambda d: None if d is None else json.dumps(d, sort_keys=True, separators=(",", ":")))
    conn.execute("INSERT INTO tuning_row(change,scope,target,dial,before_def,after_def) "
                 "VALUES (?,?,?,?,?,?)", (change, "group", str(gid), DEFINITION, dump(before),
                                          dump(after)))


def save_group(store, gid: int | None, name, where, *, source: str, now: float) -> Result:
    """Create a group (gid None) or change one's name or predicate."""
    d = {"name": G.parse_name(name), "where": G.parse_where(where)}
    with store.transaction():
        conn = store.conn
        if gid is None:
            gid = (conn.execute("SELECT max(id) FROM tuning_group").fetchone()[0] or 0) + 1
            before = None
        else:
            before = _definition(conn, gid)
            if before is None:
                raise TuneError("no such group (it may have been retired)")
            if before == d:
                return Result(None)
        _set_definition(conn, gid, d, now)
        change = _new_change(conn, now, source, "group saved" if before else "group created")
        _def_row(conn, change, gid, before, d)
        _stamp(conn, change)
        return Result(change, 1)


def retire_group(store, gid: int, *, source: str, now: float) -> Result:
    """Retire a group: its offsets stop reaching anyone (they stay in the log, and an undo
    brings the group and them back)."""
    with store.transaction():
        conn = store.conn
        before = _definition(conn, gid)
        if before is None:
            raise TuneError("no such group (it may have been retired)")
        _set_definition(conn, gid, None, now)
        change = _new_change(conn, now, source, "group retired")
        _def_row(conn, change, gid, before, None)
        _stamp(conn, change)
        return Result(change, 1)


# -- undo and revert ----------------------------------------------------------------------------

def _rows(conn, change: int) -> list:
    return conn.execute("SELECT scope,target,dial,before,after,before_def,after_def,"
                        "persona_version FROM tuning_row WHERE change=? "
                        "ORDER BY scope,target,dial", (change,)).fetchall()


def _now_def(conn, target: str):
    d = _definition(conn, int(target))
    return None if d is None else json.dumps(d, sort_keys=True, separators=(",", ":"))


def undo(store, change: int, pic: Picture, *, source: str, now: float) -> Result:
    from . import nudges
    with store.transaction():
        conn = store.conn
        if conn.execute("SELECT 1 FROM tuning_change WHERE id=?", (change,)).fetchone() is None:
            raise TuneError(f"there is no change #{change}")
        rows = _rows(conn, change)
        nudge_moves = nudges.undo_moves(conn, change)
        moved = []
        for r in rows:
            scope, target, dial = r[0], r[1], r[2]
            now_value = _now_def(conn, target) if dial == DEFINITION else _current(
                conn, scope, target, dial)
            left = r[6] if dial == DEFINITION else r[4]
            if now_value != left:
                moved.append((scope, target, dial))
        if moved:
            later = sorted({row[0] for s, t, d in moved for row in conn.execute(
                "SELECT change FROM tuning_row WHERE scope=? AND target=? AND dial=? AND change>?",
                (s, t, d, change))})
            names = ", ".join(f"#{n}" for n in later) or "a later change"
            raise TuneError(f"changed since by {names}: undo that first, or revert to a time")
        new = _new_change(conn, now, source, f"undo #{change}", undoes=change)
        for r in rows:
            scope, target, dial = r[0], r[1], r[2]
            if dial == DEFINITION:
                before = None if r[5] is None else json.loads(r[5])
                after = None if r[6] is None else json.loads(r[6])
                _set_definition(conn, int(target), before, now)
                _def_row(conn, new, int(target), after, before)
            else:
                if r[3] is not None and scope == "member":
                    check_set(Set(scope, target, dial, r[3]), pic, live_groups(conn))
                _write_value(conn, new, scope, target, dial, r[4], r[3], r[7])
        nudges.write_moves(conn, new, nudge_moves)
        _stamp(conn, new)
        return Result(new, len(rows) + len(nudge_moves))


def state_after(conn, change: int) -> tuple[dict, dict]:
    """The values and group definitions the log held right after `change` (0: before any)."""
    values, defs = {}, {}
    for r in conn.execute("SELECT scope,target,dial,after,after_def FROM tuning_row "
                          "WHERE change<=? ORDER BY change", (change,)):
        if r[2] == DEFINITION:
            defs[r[1]] = r[4]
        else:
            values[(r[0], r[1], r[2])] = r[3]
    return values, defs


def revert_to(store, change: int, pic: Picture, *, source: str, now: float) -> Result:
    """Every value and group back to how they stood right after `change` (0: before any), as
    one change; nothing when they already are."""
    from . import nudges
    with store.transaction():
        conn = store.conn
        if change and conn.execute("SELECT 1 FROM tuning_change WHERE id=?",
                                   (change,)).fetchone() is None:
            raise TuneError(f"there is no change #{change}")
        then, then_defs = state_after(conn, change)
        current = {(r[0], r[1], r[2]): r[3] for r in conn.execute(
            "SELECT scope,target,dial,value FROM tuning_value")}
        ever_defs = {r[0] for r in conn.execute(
            "SELECT target FROM tuning_row WHERE dial=?", (DEFINITION,))}
        def_moves = [(t, _now_def(conn, t), then_defs.get(t)) for t in sorted(ever_defs)
                     if _now_def(conn, t) != then_defs.get(t)]
        keys = sorted(set(then) | set(current))
        moves = [(k, current.get(k), then.get(k)) for k in keys if current.get(k) != then.get(k)]
        nudge_moves = nudges.revert_moves(conn, change)
        if not moves and not def_moves and not nudge_moves:
            return Result(None)
        new = _new_change(conn, now, source, f"revert to #{change}")
        for target, before, after in def_moves:  # groups first: their offsets need them
            _set_definition(conn, int(target), None if after is None else json.loads(after), now)
            _def_row(conn, new, int(target), None if before is None else json.loads(before),
                     None if after is None else json.loads(after))
        for (scope, target, dial), before, after in moves:
            version = pic.versions.get(int(target)) if scope == "member" else None
            _write_value(conn, new, scope, target, dial, before, after, version)
        nudges.write_moves(conn, new, nudge_moves)
        _stamp(conn, new)
        return Result(new, len(moves) + len(def_moves) + len(nudge_moves))


# -- reading the log ----------------------------------------------------------------------------

def history(conn, limit: int = 50) -> list[dict]:
    """The newest changes first, each with its rows (values and group definitions), as the page
    and the CLI show them. Nothing here is persona text."""
    out = []
    for c in conn.execute("SELECT id,ts,source,label,undoes,previewed FROM tuning_change "
                          "ORDER BY id DESC LIMIT ?", (limit,)).fetchall():
        undone = conn.execute("SELECT max(id) FROM tuning_change WHERE undoes=?",
                              (c[0],)).fetchone()[0]
        rows = []
        for r in _rows(conn, c[0]):
            row = {"scope": r[0], "target": r[1], "dial": r[2]}
            if r[2] == DEFINITION:
                row["before"] = None if r[5] is None else json.loads(r[5])
                row["after"] = None if r[6] is None else json.loads(r[6])
            else:
                row["before"], row["after"] = r[3], r[4]
            rows.append(row)
        out.append({"id": c[0], "ts": c[1], "source": c[2], "label": c[3], "undoes": c[4],
                    "previewed": bool(c[5]), "undone_by": undone, "rows": rows})
    return out


def load(conn) -> Tuning:
    return Tuning.load(conn)
