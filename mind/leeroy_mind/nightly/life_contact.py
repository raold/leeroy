"""`nightly life-contact`: the life beats already stored that put him in a member's life away
from the game (2026-11-15: his texts, notes and visits invented at night, at least 12 of them,
surfaced in chat as real; nightly.life refuses new ones).

A dry run (the default) reads mind.db read-only and prints what it would change: ids, owners and
counts, the texts only with --show. With --go, the mind stopped (the instance lock is held
throughout) and a snapshot taken first (label pre-life-contact), it
- retires every active life-beat memory (offscreen, evidence `beat:<id>`) that meets him
  (life.meets_him): status `archived`, never deleted;
- raises every other active open life-beat memory that names him or touches her secret
  (life.private_beat) to private with him, as new beats are stored.
Other offscreen memories (the night's encounters, the romance arc's weekend and visits, which are
meetings by design) are never touched. Chat and extraction memories that repeated an invented
contact stay as they are.
Each change leaves a receipt in meta (RETIRED_KEY, RESCOPED_KEY: the row's earlier status or
scope and the time), so it can be undone by hand. Nothing else is touched: the beats' rows stay,
and an outreach item resting on a retired memory lapses at its next fill (outreach.sources)."""
import json
import sqlite3
from contextlib import nullcontext
from dataclasses import dataclass, field

from leeroy_mind.clock import SystemClock
from leeroy_mind.memory import scopes
from leeroy_mind.nightly.life import his_names, meets_him, private_beat
from leeroy_mind.personas.io import load_persona
from leeroy_mind.storage.store import Store

RETIRED_KEY = "life_contact:retired:{id}"
RESCOPED_KEY = "life_contact:rescoped:{id}"
SNAPSHOT_LABEL = "pre-life-contact"


@dataclass
class Plan:
    """(memory id, owner, text) of the memories to retire and to make private with him."""
    retire: list[tuple[int, int, str]] = field(default_factory=list)
    rescope: list[tuple[int, int, str]] = field(default_factory=list)


def plan(store: Store, ident) -> Plan:
    names = his_names(ident)
    personas: dict[int, object] = {}
    out = Plan()
    rows = store.conn.execute(
        "SELECT m.id, m.owner, m.text, m.scope, (SELECT t.title FROM json_each(m.evidence) e "
        "JOIN life_beat b ON 'beat:' || b.id = e.value JOIN life_thread t ON t.id = b.thread_id "
        "LIMIT 1) AS title FROM memory m WHERE m.source = 'offscreen' AND m.status = 'active' "
        # life beats only: the arc's weekend and visit memories are meetings by design, and
        # encounters are the night's own
        "AND EXISTS (SELECT 1 FROM json_each(m.evidence) e WHERE e.value LIKE 'beat:%') "
        "ORDER BY m.id").fetchall()
    for mid, owner, text, scope, title in rows:
        if meets_him(text, names):
            out.retire.append((mid, owner, text))
            continue
        if scope != "open":
            continue
        if owner not in personas:
            personas[owner] = load_persona(store, owner)
        if private_beat(text, title or "", personas[owner], names):
            out.rescope.append((mid, owner, text))
    return out


def apply(store: Store, p: Plan, *, human: int, now: float) -> tuple[int, int]:
    """The plan's changes and their receipts, in one transaction: (retired, rescoped)."""
    retired = rescoped = 0
    with store.transaction():
        for mid, _, _ in p.retire:
            if store.conn.execute("UPDATE memory SET status = 'archived' WHERE id = ? "
                                  "AND status = 'active'", (mid,)).rowcount:
                store.set_meta(RETIRED_KEY.format(id=mid),
                               json.dumps({"status": "active", "at": now}))
                retired += 1
        for mid, _, _ in p.rescope:
            if store.conn.execute("UPDATE memory SET scope = 'private', scope_with = ? "
                                  "WHERE id = ? AND scope = 'open'", (human, mid)).rowcount:
                store.set_meta(RESCOPED_KEY.format(id=mid),
                               json.dumps({"scope": "open", "scope_with": 0, "at": now}))
                rescoped += 1
        # whatever rests on a beat made private is private from now on too
        if rescoped:
            scopes.after_raise(store.conn, now=now, source="life-contact")
    return retired, rescoped


def run(root, cfg, *, go: bool = False, show: bool = False, clock=None) -> int:
    from leeroy_mind.app import AlreadyRunning, acquire_instance_lock
    from leeroy_mind.nightly.cli import _identity
    from leeroy_mind.storage.db import snapshot

    clock = clock or SystemClock()
    path = (root / "mind.db").resolve()
    try:
        lock = acquire_instance_lock(root) if go else nullcontext()
    except AlreadyRunning:
        print("refused: mind service is running")
        return 1
    with lock:
        conn = None
        try:
            conn = sqlite3.connect(f"{path.as_uri()}?mode={'rw' if go else 'ro'}", uri=True)
            conn.row_factory = sqlite3.Row
            store = Store(conn, clock=clock)
            ident = _identity(cfg, store)
            p = plan(store, ident)
            for what, rows in (("retire", p.retire), ("private", p.rescope)):
                for mid, owner, text in rows:
                    print(f"{what} memory {mid} owner {owner}" + (f": {text}" if show else ""))
            print(f"to retire {len(p.retire)}, to make private {len(p.rescope)}")
            if not go:
                print("dry run, nothing written")
                return 0
            print(f"snapshot {snapshot(conn, root / 'snapshots', SNAPSHOT_LABEL)}")
            retired, rescoped = apply(store, p, human=ident.guid, now=clock.now())
            print(f"retired {retired}, made private {rescoped}; receipts in meta")
            return 0
        except (sqlite3.Error, ValueError, OSError) as err:
            print(f"refused: {type(err).__name__}; nothing written")
            return 1
        finally:
            if conn is not None:
                conn.close()
