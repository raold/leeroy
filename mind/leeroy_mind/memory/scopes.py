"""Keeping every memory's scope at least as strict as its evidence (2026-11-17).

memory.privacy.resolve gives the effective scope readers go by. This module writes it down:
- settle checks unchecked memories (derived_at missing) to a fixed point, stores what their
  evidence implies (derived_scope, derived_with, derived_at) and, unless told not to, raises the
  stored scope to it. The store calls it for each memory it writes, in that write's transaction;
- settle_pending works through whatever the triggers of the scope_evidence migration left
  unchecked (the memories resting on one whose scope or evidence changed), bounded;
- backfill is that migration's data step: every stored memory checked once, no stored scope
  changed;
- the night's model-free stage `scopes` (stage_scopes) settles what is pending and checks the
  recently written memories again, bounded a night, resumable (the state is the rows');
- plan/apply/undo are `memory rescope`: counts of what would change, the change with a receipt
  (memory_rescope), and a receipt's stored scopes restored.

A scope only ever gets stricter here. Nothing in this module reads or prints a memory's text.
Standard library and memory.privacy only (storage.store imports it)."""
import json
import logging
import sqlite3
from collections import Counter
from dataclasses import dataclass, field

from leeroy_mind.discord.channels import is_private, is_restricted
from leeroy_mind.memory import privacy
from leeroy_mind.memory.privacy import SCOPES, resolve

log = logging.getLogger("leeroy_mind")

STAGE = "scopes"
AFTER = "forget"
NIGHT_ROWS = 20000     # memories one night's stage checks at most
NIGHT_CHUNK = 2000
RECENT_S = 3 * 86400   # the stage checks memories written this recently again
WRITE_ROWS = 500       # dependants a write path settles in its own transaction; the rest wait
CHUNK = 5000


@dataclass
class Settled:
    checked: int = 0                      # memories derived
    raised: int = 0                       # stored scopes made stricter
    receipt: int | None = None            # the memory_rescope row, when one was written
    changes: list = field(default_factory=list)   # [id, scope, with, new scope, new with]

    def add(self, other: "Settled") -> None:
        self.checked += other.checked
        self.raised += other.raised
        self.changes += other.changes
        self.receipt = other.receipt if other.receipt is not None else self.receipt


def available(conn: sqlite3.Connection) -> bool:
    """Whether the database has the scope_evidence migration."""
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' "
                        "AND name = 'memory_cite'").fetchone() is not None


def pending(conn: sqlite3.Connection, limit: int | None = None) -> list[int]:
    """The unchecked memories, oldest id first."""
    sql = "SELECT id FROM memory WHERE derived_at IS NULL ORDER BY id"
    if limit is not None:
        return [r[0] for r in conn.execute(sql + " LIMIT ?", (int(limit),))]
    return [r[0] for r in conn.execute(sql)]


def pending_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT count(*) FROM memory WHERE derived_at IS NULL").fetchone()[0]


def _receipt(conn: sqlite3.Connection, changes: list, *, now: float, source: str) -> int:
    return conn.execute("INSERT INTO memory_rescope(at, source, changes) VALUES (?, ?, ?)",
                        (now, source, json.dumps(changes, separators=(",", ":")))).lastrowid


def settle(conn: sqlite3.Connection, ids, *, now: float, raise_stored: bool = True,
           recheck: bool = False, source: str | None = "write") -> Settled:
    """Check the memories `ids`, and every unchecked memory they rest on, to a fixed point, and
    store it: derived_scope, derived_with and derived_at, and with `raise_stored` the stored
    scope made as strict as the effective one. Memories of `ids` already checked are left alone
    unless `recheck`. The stored scopes it raises go into one receipt (`source`; None writes
    none: a row being inserted has nothing to restore). The triggers then clear derived_at on
    whatever rests on a memory this changed; those are the caller's or the night's
    (settle_pending). Inside the caller's transaction; no commit."""
    found = resolve(conn, ids, cap=None, recheck=recheck)
    nodes = {mid: r for mid, r in found.items() if not r.checked}
    out = Settled(checked=len(nodes))
    if not nodes:
        return out
    rows = []
    for mid in sorted(nodes):
        r = nodes[mid]
        stored = r.effective if raise_stored else r.stored
        if stored != r.stored:
            out.changes.append([mid, *r.stored, *stored])
        rows.append((*r.derived, *stored, now, mid))
    conn.executemany("UPDATE memory SET derived_scope = ?, derived_with = ?, scope = ?, "
                     "scope_with = ?, derived_at = ? WHERE id = ?", rows)
    # a change above cleared derived_at on what rests on it, these rows among them; they are
    # at a fixed point with each other, so they are checked
    ids = sorted(nodes)
    for i in range(0, len(ids), CHUNK):
        conn.execute("UPDATE memory SET derived_at = ? WHERE derived_at IS NULL AND id IN "
                     "(SELECT value FROM json_each(?))", (now, json.dumps(ids[i:i + CHUNK])))
    out.raised = len(out.changes)
    if out.changes and source is not None:
        out.receipt = _receipt(conn, out.changes, now=now, source=source)
    return out


def settle_pending(conn: sqlite3.Connection, *, now: float, limit: int | None = None,
                   raise_stored: bool = True, source: str | None = "write",
                   chunk: int = CHUNK) -> Settled:
    """settle whatever is unchecked, oldest first, until nothing is or `limit` memories were
    checked (a settle may leave new ones: what rests on a memory it raised). One receipt for
    all the stored scopes raised."""
    out = Settled()
    while limit is None or out.checked < limit:
        ids = pending(conn, chunk if limit is None else min(chunk, limit - out.checked))
        if not ids:
            break
        out.add(settle(conn, ids, now=now, raise_stored=raise_stored, source=None))
    if out.changes and source is not None:
        out.receipt = _receipt(conn, out.changes, now=now, source=source)
    return out


def after_write(conn: sqlite3.Connection, mid: int, *, now: float, new: bool) -> None:
    """The store's step after it wrote memory `mid` (inserted: `new`), in the same transaction:
    the memory is checked and its stored scope made as strict as its evidence, then what rests
    on it, when that changed anything, up to WRITE_ROWS memories (the night takes the rest;
    until then readers derive them). A database from before the migration is left as it is."""
    try:
        settle(conn, (mid,), now=now, source=None if new else "write")
        if conn.execute("SELECT EXISTS(SELECT 1 FROM memory WHERE derived_at IS NULL)"
                        ).fetchone()[0]:
            settle_pending(conn, now=now, limit=WRITE_ROWS)
    except sqlite3.OperationalError as e:
        if "no such" not in str(e):
            raise


def after_raise(conn: sqlite3.Connection, *, now: float, source: str) -> Settled:
    """After a step that made stored scopes stricter (a cleanup, a line's secrecy): what the
    triggers left unchecked is settled in the caller's transaction, up to WRITE_ROWS."""
    if not available(conn):
        return Settled()
    return settle_pending(conn, now=now, limit=WRITE_ROWS, source=source)


def lines_changed(conn: sqlite3.Connection, lines, *, now: float) -> int:
    """After chat lines were added, threaded or edited: when one of them is told in confidence
    (privacy.secret_lines) every memory resting on a line that ask may cover is derived again,
    in the caller's transaction. How many memories were unchecked."""
    if not available(conn):
        return 0
    lines = list(lines)
    asked = privacy.secret_lines(conn, lines)
    if not asked:
        return 0
    reach: set[int] = set()
    for line in asked:
        reach |= privacy.secrecy_reach(conn, line)
    n = privacy.uncheck_citing(conn, "chat", reach)
    if n:
        done = after_raise(conn, now=now, source="secrecy")
        log.info("scopes_secrecy: lines=%d unchecked=%d raised=%d", len(asked), n, done.raised)
    return n


def backfill(conn: sqlite3.Connection) -> int:
    """The scope_evidence migration's data step: every stored memory checked once, in the
    migration's transaction. Only the derived columns are written; no stored scope changes
    (readers go by the stricter of the two; `memory rescope --go` raises the stored ones with
    a receipt). Returns how many memories derive stricter than they are stored."""
    import time
    done = settle_pending(conn, now=time.time(), raise_stored=False, source=None, chunk=10 ** 9)
    wide = conn.execute(
        "SELECT count(*) FROM memory WHERE derived_scope != 'open' AND (scope = 'open' "
        "OR (scope = 'private' AND derived_scope = 'secret') "
        "OR (scope_with != derived_with AND scope_with != 0))").fetchone()[0]
    log.info("scope_evidence_backfill: checked=%d wider_than_evidence=%d", done.checked, wide)
    return wide


# -- the night's stage -----------------------------------------------------------------------

def night(conn: sqlite3.Connection, *, now: float, rows: int = NIGHT_ROWS) -> dict:
    """One night's work, in the caller's transaction: the memories written or touched within
    RECENT_S are checked again (a line's secrecy, anything a trigger could not see), then
    whatever is unchecked, to a fixed point, at most `rows` memories in all. Counts only."""
    recent = [r[0] for r in conn.execute(
        "SELECT id FROM memory WHERE t_created >= ? ORDER BY id DESC LIMIT ?",
        (now - RECENT_S, rows))]
    out = Settled()
    for i in range(0, len(recent), NIGHT_CHUNK):
        out.add(settle(conn, recent[i:i + NIGHT_CHUNK], now=now, recheck=True, source=None))
    out.add(settle_pending(conn, now=now, limit=max(0, rows - out.checked), source=None,
                           chunk=NIGHT_CHUNK))
    receipt = _receipt(conn, out.changes, now=now, source="night") if out.changes else None
    return {"checked": out.checked, "raised": out.raised, "left": pending_count(conn),
            "receipt": receipt}


async def stage_scopes(ctx) -> None:
    """Model-free. One unit a night; a night cut short leaves the rest unchecked, which the next
    night (and every reader meanwhile) takes up."""
    if ctx.done(STAGE, "settle"):
        return
    now = ctx.clock.now()
    result: dict = {}

    def write(s):
        result.update(night(s.conn, now=now))
    if ctx.commit(STAGE, "settle", write):
        from leeroy_mind.logs import log as event
        event("scopes_settled", night=ctx.night, checked=result.get("checked", 0),
              raised=result.get("raised", 0), left=result.get("left", 0),
              receipt=result.get("receipt") or 0)


def attach(runner) -> None:
    """Always on: the stage costs one pass over the recent and unchecked memories."""
    runner.add_stage(STAGE, stage_scopes, after=AFTER, model=None)


# -- memory rescope ----------------------------------------------------------------------------

@dataclass
class Plan:
    """What `memory rescope` would do. Counts and ids only, never text."""
    memories: int = 0
    changes: list = field(default_factory=list)      # [id, scope, with, new scope, new with]
    derived: dict = field(default_factory=dict)      # id -> (derived scope, derived with)
    by_move: Counter = field(default_factory=Counter)      # "open -> private" ...
    by_status: Counter = field(default_factory=Counter)
    by_kind: Counter = field(default_factory=Counter)
    by_source: Counter = field(default_factory=Counter)
    by_evidence: Counter = field(default_factory=Counter)


def _partner(with_: int, human: int) -> str:
    if with_ == privacy.OFFICERS:
        return "officers"
    if with_ == privacy.GROUP:
        return "group"
    if with_ == 0:
        return "nobody"
    return "him" if with_ == human else "a member"


def _label(scope: tuple[str, int], human: int) -> str:
    return "open" if scope[0] == "open" else f"{scope[0]} with {_partner(scope[1], human)}"


def plan(conn: sqlite3.Connection) -> Plan:
    """Every memory derived again from its evidence (nothing stored is trusted), and what that
    would change. Read-only."""
    ids = [r[0] for r in conn.execute("SELECT id FROM memory ORDER BY id")]
    found = resolve(conn, ids, cap=None, recheck=True)
    out = Plan(memories=len(ids), derived={i: r.derived for i, r in found.items()})
    human = privacy._human_guid(conn)
    raised = {i: r for i, r in found.items() if r.effective != r.stored}
    if not raised:
        return out
    info = {r[0]: r[1:] for r in conn.execute(
        "SELECT id, status, kind, source, evidence FROM memory "
        "WHERE id IN (SELECT value FROM json_each(?))", (json.dumps(sorted(raised)),))}
    refs = {i: privacy._refs_of(info[i][3]) for i in raised}
    lines = privacy._lines(conn, {c for r in refs.values() for c in privacy._ids(r, "chat")})
    secret = privacy.secret_lines(conn, lines)
    hidden = privacy.restricted_channels(conn, refs)
    for mid in sorted(raised):
        r = raised[mid]
        out.changes.append([mid, *r.stored, *r.effective])
        out.by_move[f"{_label(r.stored, human)} -> {_label(r.effective, human)}"] += 1
        status, kind, source, _ = info[mid]
        out.by_status[status] += 1
        out.by_kind[kind] += 1
        out.by_source[source] += 1
        chats = privacy._ids(refs[mid], "chat")
        why = set()
        if any(c in secret for c in chats):
            why.add("a line told in confidence, cited directly")
        if any(c in lines and is_private(lines[c].channel) and c not in secret for c in chats):
            why.add("a private line, cited directly")
        if mid in hidden:
            direct = any(c in lines and is_restricted(lines[c].channel) for c in chats)
            why.add("a restricted line, cited directly" if direct
                    else "a restricted line, through cited memories")
        for c in privacy._ids(refs[mid], "memory"):
            cited = found.get(c)
            if cited is None or cited.effective[0] == "open":
                continue
            why.add("a cited memory stored private or secret" if cited.stored[0] != "open"
                    else "a cited memory that itself rests on scoped evidence (deeper)")
        for w in why or {"its own earlier check"}:
            out.by_evidence[w] += 1
    return out


def recalled(conn: sqlite3.Connection, p: Plan, *, since: float) -> dict:
    """How many sent turns since `since` carried a memory the plan makes stricter that the
    turn's audience could then no longer be shown (recall.scope_ok on the new scope), by the
    kind of turn. Counts only."""
    from types import SimpleNamespace

    from leeroy_mind.discord.channels import is_group
    from leeroy_mind.memory.recall import scope_ok
    new = {c[0]: (c[3], c[4]) for c in p.changes}
    owners = {r[0]: r[1] for r in conn.execute(
        "SELECT id, owner FROM memory WHERE id IN (SELECT value FROM json_each(?))",
        (json.dumps(sorted(new)),))} if new else {}
    out = {"turns": 0, "public_turns": 0, "public_refused": 0, "other_refused": 0,
           "memories": set(), "other_members": 0}
    for guid, channel, to_guid, raw in conn.execute(
            "SELECT guid, channel, to_guid, memory_ids FROM utterance "
            "WHERE ts >= ? AND dropped IS NULL", (since,)):
        out["turns"] += 1
        public = not (is_private(channel) or is_restricted(channel) or is_group(channel))
        out["public_turns"] += public
        try:
            mids = [i for i in json.loads(raw) if type(i) is int]
        except (ValueError, TypeError):
            continue
        hit = [i for i in mids if i in new and not scope_ok(
            SimpleNamespace(owner=owners.get(i, 0), scope=new[i][0], scope_with=new[i][1], key=None),
            turn_channel=channel, to_guid=to_guid or 0)]
        if hit:
            out["public_refused" if public else "other_refused"] += 1
            out["memories"] |= set(hit)
            out["other_members"] += any(owners.get(i) != guid for i in hit)
    out["memories"] = len(out["memories"])
    return out


def apply(conn: sqlite3.Connection, *, now: float, source: str = "cli") -> Settled:
    """`memory rescope --go`: every memory derived again and its stored scope made as strict as
    its evidence, in the caller's transaction, with one receipt."""
    out = Settled()
    ids = [r[0] for r in conn.execute("SELECT id FROM memory ORDER BY id")]
    out.add(settle(conn, ids, now=now, recheck=True, source=None))
    out.add(settle_pending(conn, now=now, source=None))
    seen: dict[int, list] = {}
    for mid, scope, scope_with, new, new_with in out.changes:  # one row a memory: first old, last new
        if mid in seen:
            seen[mid][3:] = [new, new_with]
        else:
            seen[mid] = [mid, scope, scope_with, new, new_with]
    out.changes = [seen[i] for i in sorted(seen)]
    out.raised = len(out.changes)
    if out.changes:
        out.receipt = _receipt(conn, out.changes, now=now, source=source)
    return out


def receipts(conn: sqlite3.Connection) -> list[tuple]:
    """(id, at, source, how many memories, undone_at) of every receipt, newest first."""
    return [(r[0], r[1], r[2], r[3], r[4]) for r in conn.execute(
        "SELECT id, at, source, json_array_length(changes), undone_at FROM memory_rescope "
        "ORDER BY id DESC")]


def undo(conn: sqlite3.Connection, receipt: int, *, now: float, write: bool = True) -> dict:
    """Restore the stored scopes receipt `receipt` changed, where they are still what it made
    them (one changed since is left). Only what is stored goes back: the derived columns stay,
    so readers still go by the evidence, and a later settle of a memory raises it again.
    Raises KeyError for an unknown receipt and ValueError for one already undone."""
    row = conn.execute("SELECT changes, undone_at FROM memory_rescope WHERE id = ?",
                       (receipt,)).fetchone()
    if row is None:
        raise KeyError(receipt)
    if row[1] is not None:
        raise ValueError("already undone")
    out = {"restored": 0, "changed_since": 0, "gone": 0}
    for mid, scope, scope_with, new, new_with in json.loads(row[0]):
        now_row = conn.execute("SELECT scope, scope_with FROM memory WHERE id = ?",
                               (mid,)).fetchone()
        if now_row is None:
            out["gone"] += 1
        elif tuple(now_row) != (new, new_with) or scope not in SCOPES:
            out["changed_since"] += 1
        else:
            out["restored"] += 1
            if write:
                conn.execute("UPDATE memory SET scope = ?, scope_with = ? WHERE id = ?",
                             (scope, scope_with, mid))
    if write:
        conn.execute("UPDATE memory_rescope SET undone_at = ? WHERE id = ?", (now, receipt))
        # the triggers unchecked what rests on the restored rows; check it again without
        # raising anything stored, or the undo would not hold until the command ends
        settle_pending(conn, now=now, raise_stored=False, source=None)
    return out
