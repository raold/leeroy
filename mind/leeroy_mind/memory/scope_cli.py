"""`memory rescope`: make every stored memory's scope as strict as its evidence (memory/scopes.py).

A dry run by default: every memory is derived again from its evidence, to a fixed point, on a
read-only connection (an older mind.db is not migrated), and the counts of what would change are
printed: by old and new scope, by status, kind and source, by the kind of evidence that asks for
it, and how many sent turns of the last `--days` days carried such a memory to an audience it will
no longer reach. `--go` needs the mind stopped and a migrated mind.db: it holds mind.lock
throughout, snapshots first and writes one receipt; `--undo N --go` restores the stored scopes
receipt N changed. A scope is only ever made stricter. Counts and ids only: never a memory's or a
line's text."""
import argparse
import sqlite3
import sys
import time
from contextlib import nullcontext
from pathlib import Path

from leeroy_mind.config import default_data_dir
from leeroy_mind.memory import scopes

SNAPSHOT_LABEL = "pre-rescope"


def _connect(path: Path, *, write: bool) -> sqlite3.Connection:
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode={'rw' if write else 'ro'}", uri=True,
                           isolation_level=None)
    conn.row_factory = sqlite3.Row  # storage.db.snapshot reads its rows by name
    return conn


def _table(title: str, counts) -> None:
    if not counts:
        return
    print(f"{title}:")
    for name, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
        print(f"  {n:6d}  {name}")


def _report(conn, plan, days: float, now: float) -> None:
    print(f"memories {plan.memories}, to make stricter {len(plan.changes)}")
    _table("by scope", plan.by_move)
    _table("by status", plan.by_status)
    _table("by kind", plan.by_kind)
    _table("by source", plan.by_source)
    _table("by evidence (a memory can count under several)", plan.by_evidence)
    if days > 0 and plan.changes:
        r = scopes.recalled(conn, plan, since=now - days * 86400)
        print(f"sent turns in the last {days:g} days: {r['turns']} ({r['public_turns']} public)")
        print(f"  {r['public_refused']:6d}  public turns that carried a memory now refused there")
        print(f"  {r['other_refused']:6d}  private, officers' or group turns likewise")
        print(f"  {r['other_members']:6d}  of those turns carried another member's memory")
        print(f"  {r['memories']:6d}  distinct memories in them")


def memory_main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="leeroy_mind memory")
    sub = parser.add_subparsers(dest="action", required=True)
    p = sub.add_parser("rescope", help="make stored scopes as strict as their evidence: a dry "
                       "run unless --go (mind stopped)")
    p.add_argument("--data-dir")
    p.add_argument("--dry-run", action="store_true", help="the default: counts only")
    p.add_argument("--go", action="store_true", help="write: holds mind.lock, snapshots first")
    p.add_argument("--undo", type=int, metavar="N", help="restore the stored scopes of receipt N")
    p.add_argument("--receipts", action="store_true", help="list the receipts")
    p.add_argument("--days", type=float, default=7.0,
                   help="count the sent turns of this many days that carried such a memory")
    args = parser.parse_args(argv)
    if args.go and args.dry_run:
        parser.error("--go and --dry-run exclude each other")
    root = Path(args.data_dir).resolve() if args.data_dir else default_data_dir()
    path = root / "mind.db"
    from leeroy_mind.app import AlreadyRunning, acquire_instance_lock
    from leeroy_mind.storage.db import snapshot
    try:
        lock = acquire_instance_lock(root) if args.go else nullcontext()
    except AlreadyRunning:
        print("refused: mind service is running; nothing written")
        return 1
    with lock:
        conn = None
        try:
            conn = _connect(path, write=args.go)
            now = time.time()
            ready = scopes.available(conn)
            if args.receipts:
                for rid, at, source, n, undone in (scopes.receipts(conn) if ready else ()):
                    print(f"receipt {rid}: {source}, {n} memories, at {at:.0f}"
                          + (f", undone at {undone:.0f}" if undone is not None else ""))
                return 0
            if (args.go or args.undo is not None) and not ready:
                print("refused: this mind.db is older than the code (run db migrate); "
                      "nothing written")
                return 1
            if args.undo is not None:
                return _undo(conn, root, args, now, snapshot)
            plan = scopes.plan(conn)
            _report(conn, plan, max(0.0, args.days), now)
            if not args.go:
                print("dry run, nothing written")
                return 0
            if not plan.changes and not scopes.pending_count(conn):
                print("every stored scope is as strict as its evidence; nothing written")
                return 0
            snap = snapshot(conn, root / "snapshots", SNAPSHOT_LABEL)
            conn.execute("BEGIN IMMEDIATE")
            done = scopes.apply(conn, now=now)
            conn.execute("COMMIT")
            print(f"written: {done.raised} memories made stricter, receipt {done.receipt}; "
                  f"snapshot {snap}")
            if done.receipt is not None:
                print(f"undo: python -m leeroy_mind memory rescope --data-dir {root} "
                      f"--undo {done.receipt} --go")
            return 0
        except (sqlite3.Error, ValueError, KeyError, OSError) as err:
            if conn is not None and conn.in_transaction:
                conn.execute("ROLLBACK")
            print(f"refused: {type(err).__name__}; nothing written", file=sys.stderr)
            return 1
        finally:
            if conn is not None:
                conn.close()


def _undo(conn, root, args, now, snapshot) -> int:
    try:
        would = scopes.undo(conn, args.undo, now=now, write=False)
    except KeyError:
        print(f"refused: no receipt {args.undo}; nothing written")
        return 1
    except ValueError:
        print(f"refused: receipt {args.undo} is already undone; nothing written")
        return 1
    print(f"receipt {args.undo}: {would['restored']} stored scopes to restore, "
          f"{would['changed_since']} changed since and left, {would['gone']} gone")
    if not args.go:
        print("dry run, nothing written")
        return 0
    snap = snapshot(conn, root / "snapshots", SNAPSHOT_LABEL + "-undo")
    conn.execute("BEGIN IMMEDIATE")
    done = scopes.undo(conn, args.undo, now=now)
    conn.execute("COMMIT")
    print(f"written: {done['restored']} stored scopes restored; snapshot {snap}")
    print("readers still go by each memory's evidence, and the night's scopes stage raises a "
          "stored scope again when it next checks that memory")
    return 0
