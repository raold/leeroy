"""Retire junk slang: stored slang terms the slang filter (vocab.junk_reason) would never have
promoted, with their holders' "Picked up 'X' from Y." memories.

Status changes only, never a deleted row. A term goes dormant with `retired` set (spread
never revives a retired slang term and the guard rations only active ones), its adopters go
inactive and its keyed memories are archived, as an owner's veto archives them. The vocab row
keeps slang=1, so the word is never promoted again. A receipt in meta (RECEIPT + n) keeps every
prior value for undo. The caller holds mind.lock and the transaction; nothing here commits."""
import json
import sqlite3
from dataclasses import dataclass
from types import SimpleNamespace

from leeroy_mind.nightly import lexicon, vocab
from leeroy_mind.personas.tiers import cast_sql
from leeroy_mind.wow import CAST_MIN_LEVEL

RECEIPT = "lexicon:clean-junk:"


@dataclass(frozen=True)
class TermPlan:
    id: int
    term: str
    status: str
    reason: str | None  # None: kept
    adopters: tuple[int, ...]
    memories: tuple[int, ...]


class Unavailable(ValueError):
    """The database lacks what the step reads (an older schema): run db migrate first."""


def _columns(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def check_schema(conn):
    need = {"lexicon": {"id", "kind", "term", "status", "originator", "retired"},
            "lexicon_adopter": {"term", "guid", "active"}, "memory": {"id", "owner", "key", "status", "evidence"},
            "chat_line": {"ts", "channel", "from_guid", "origin", "text"}, "meta": {"key", "value"}}
    for table, columns in need.items():
        if not columns <= _columns(conn, table):
            raise Unavailable(f"{table} lacks what this step reads")


def _copies(conn, term):
    """Active memory ids holding this term's copy: lexicon's own match (as its veto uses), and
    an early copy without a joke tag too, since a slang key names its word."""
    key = lexicon.memory_key(term)
    ids = []
    for row in conn.execute("SELECT id,key,text,evidence FROM memory WHERE key=? AND status='active' "
                            "ORDER BY id", (key,)):
        try:
            evidence = tuple(json.loads(row["evidence"] or "[]"))
        except (TypeError, ValueError):
            continue
        untagged = not any(isinstance(ref, str) and ref.startswith("joke:") for ref in evidence)
        if untagged or lexicon._matches(SimpleNamespace(key=row["key"], text=row["text"],
                                                        evidence=evidence), term):
            ids.append(row["id"])
    return tuple(ids)


def plan(conn, *, now) -> list[TermPlan]:
    """Every live slang term (not vetoed, not yet retired), and why it is junk (None: kept)."""
    check_schema(conn)
    row = conn.execute("SELECT value FROM meta WHERE key='human_guid'").fetchone()
    human = int(row[0] or 0) if row else 0
    # The cast as audience.cast_names counts it (read here without a Store, which would end
    # the caller's transaction), and the human.
    eligible = {r[0] for r in conn.execute(
        "SELECT p.guid FROM persona_core p JOIN cast_member c ON c.guid=p.guid "
        f"WHERE {cast_sql(conn, 'p.guid')} AND c.level>=?", (CAST_MIN_LEVEL,))} | {human}
    terms = conn.execute("SELECT * FROM lexicon WHERE kind='slang' AND status!='vetoed' "
                         "AND retired IS NULL ORDER BY id").fetchall()
    rest = [t["term"] for t in terms if vocab.junk_reason(t["term"], t["originator"]) is None]
    counts = vocab.corpus(conn, rest, eligible=eligible, human=human, end=now) if rest else None
    plans = []
    for term in terms:
        reason = vocab.junk_reason(term["term"], term["originator"], counts)
        adopters = tuple(r[0] for r in conn.execute(
            "SELECT guid FROM lexicon_adopter WHERE term=? AND active=1 ORDER BY guid", (term["id"],)))
        plans.append(TermPlan(term["id"], term["term"], term["status"], reason, adopters,
                              _copies(conn, term) if reason else ()))
    return plans


def receipts(conn) -> dict[int, dict]:
    out = {}
    for row in conn.execute("SELECT key,value FROM meta WHERE key LIKE ?", (RECEIPT + "%",)):
        suffix = row["key"][len(RECEIPT):]
        if suffix.isdecimal():
            out[int(suffix)] = json.loads(row["value"])
    return out


def apply(conn, plans, *, now, snapshot) -> int | None:
    """Retire the junk terms of `plans`; the new receipt's number, or None with nothing to do."""
    junk = [p for p in plans if p.reason is not None]
    if not junk:
        return None
    for p in junk:
        conn.execute("UPDATE lexicon SET status='dormant',retired=? WHERE id=? AND retired IS NULL", (now, p.id))
        conn.executemany("UPDATE lexicon_adopter SET active=0 WHERE term=? AND guid=? AND active=1",
                         [(p.id, guid) for guid in p.adopters])
        conn.executemany("UPDATE memory SET status='archived' WHERE id=? AND status='active'",
                         [(ident,) for ident in p.memories])
    number = max(receipts(conn), default=0) + 1
    body = {"at": now, "snapshot": snapshot, "undone_at": None,
            "terms": [{"id": p.id, "term": p.term, "status": p.status, "reason": p.reason,
                       "adopters": list(p.adopters), "memories": list(p.memories)} for p in junk]}
    conn.execute("INSERT INTO meta(key,value) VALUES (?,?)", (f"{RECEIPT}{number}", json.dumps(body)))
    return number


@dataclass(frozen=True)
class UndoResult:
    terms: int
    terms_changed: int
    adopters: int
    memories: int
    memories_changed: int


def undo(conn, number, *, now, write=True) -> UndoResult:
    """Put back what receipt `number` changed, where nothing changed it since (a term no longer
    retired by this receipt, a copy no longer archived: left alone). `write=False` only counts."""
    check_schema(conn)
    body = receipts(conn).get(number)
    if body is None:
        raise KeyError(number)
    if body.get("undone_at") is not None:
        raise ValueError("already undone")
    terms = changed = adopters = memories = moved = 0
    for t in body["terms"]:
        row = conn.execute("SELECT status,retired FROM lexicon WHERE id=?", (t["id"],)).fetchone()
        if row is None or row["status"] != "dormant" or row["retired"] != body["at"]:
            changed += 1
        else:
            terms += 1
            if write:
                conn.execute("UPDATE lexicon SET status=?,retired=NULL WHERE id=?", (t["status"], t["id"]))
        for guid in t["adopters"]:
            hit = conn.execute("SELECT 1 FROM lexicon_adopter WHERE term=? AND guid=? AND active=0",
                               (t["id"], guid)).fetchone()
            adopters += hit is not None
            if hit and write:
                conn.execute("UPDATE lexicon_adopter SET active=1 WHERE term=? AND guid=?", (t["id"], guid))
        for ident in t["memories"]:
            if conn.execute("SELECT 1 FROM memory WHERE id=? AND status='archived'", (ident,)).fetchone() is None:
                moved += 1
                continue
            memories += 1
            if write:
                conn.execute("UPDATE memory SET status='active' WHERE id=?", (ident,))
    if write:
        body["undone_at"] = now
        conn.execute("UPDATE meta SET value=? WHERE key=?", (json.dumps(body), f"{RECEIPT}{number}"))
    return UndoResult(terms, changed, adopters, memories, moved)


def connect(path, *, write):
    """mind.db without migrating it: read-only for a dry run."""
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode={'rw' if write else 'ro'}", uri=True,
                           isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn
