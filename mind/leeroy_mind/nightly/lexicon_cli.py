"""`lexicon clean-junk`: retire stored slang that is no slang (nightly/junk_slang.py).

A dry run by default: counts and a sample of the terms, read-only, never migrating. `--go`
needs the mind stopped: it holds mind.lock throughout, snapshots mind.db first and writes a
receipt; `--undo N --go` puts receipt N's changes back the same way. Prints terms (ordinary
words), never memory or chat text."""
import argparse
import sqlite3
import sys
import time
from collections import Counter
from contextlib import nullcontext
from pathlib import Path

from leeroy_mind.config import default_data_dir
from leeroy_mind.nightly import junk_slang

SAMPLE = 25


def _listing(plans, limit):
    if not plans:
        return "none"
    shown = sorted(plans, key=lambda p: (-len(p.adopters), p.term))[:limit]
    more = len(plans) - len(shown)
    text = ", ".join(f"{p.term} ({len(p.adopters)})" for p in shown)
    return (text + f", and {more} more" if text else f"{more} terms") if more else text


def _report(conn, plans, sample):
    junk = [p for p in plans if p.reason is not None]
    kept = [p for p in plans if p.reason is None]
    reasons = Counter(p.reason for p in junk)
    print(f"slang terms: {len(plans)} live; retire {len(junk)}, keep {len(kept)}")
    if reasons:
        print("  retire by reason: " + ", ".join(f"{k} {v}" for k, v in sorted(reasons.items())))
    adopters = sum(len(p.adopters) for p in junk)
    members = len({g for p in junk for g in p.adopters})
    memories = sum(len(p.memories) for p in junk)
    owners = len({o for p in junk for o in _owners(conn, p.memories)})
    print(f"adopters to set inactive: {adopters} ({members} members)")
    print(f"'Picked up' memories to archive: {memories} ({owners} members)")
    print(f"retire (adopters): {_listing(junk, sample)}")
    print(f"keep (adopters): {_listing(kept, sample)}")
    other = conn.execute("SELECT kind,count(*) FROM lexicon WHERE kind!='slang' GROUP BY kind ORDER BY kind").fetchall()
    print("untouched: " + (", ".join(f"{n} {k} terms" for k, n in other) or "no other terms")
          + "; vetoed and already retired slang are left alone")


def _owners(conn, ids):
    for start in range(0, len(ids), 500):
        part = ids[start:start + 500]
        yield from (r[0] for r in conn.execute(
            f"SELECT owner FROM memory WHERE id IN ({','.join('?' * len(part))})", part))


def lexicon_main(argv=None):
    parser = argparse.ArgumentParser(prog="leeroy_mind lexicon")
    sub = parser.add_subparsers(dest="action", required=True)
    p = sub.add_parser("clean-junk", help="retire stored slang that is an ordinary word, and its "
                       "'Picked up' memories: a dry run unless --go (mind stopped)")
    p.add_argument("--data-dir")
    p.add_argument("--go", action="store_true", help="write: holds mind.lock, snapshots first")
    p.add_argument("--undo", type=int, metavar="N", help="put receipt N's changes back")
    p.add_argument("--sample", type=int, default=SAMPLE, help="terms listed per group")
    args = parser.parse_args(argv)
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
            conn = junk_slang.connect(path, write=args.go)
            now = time.time()
            if args.undo is not None:
                return _undo(conn, root, args, now, snapshot)
            plans = junk_slang.plan(conn, now=now)
            _report(conn, plans, max(0, args.sample))
            if not args.go:
                print("dry run, nothing written")
                return 0
            if not any(p.reason for p in plans):
                print("nothing to retire; nothing written")
                return 0
            snap = snapshot(conn, root / "snapshots", "pre-clean-junk")
            conn.execute("BEGIN IMMEDIATE")
            number = junk_slang.apply(conn, junk_slang.plan(conn, now=now), now=now, snapshot=snap.name)
            conn.execute("COMMIT")
            print(f"written: receipt {number}; snapshot {snap}")
            print(f"undo: python -m leeroy_mind lexicon clean-junk --data-dir {root} --undo {number} --go")
            return 0
        except junk_slang.Unavailable:
            print("refused: this mind.db is older than the code (run db migrate); nothing written")
            return 1
        except (sqlite3.Error, ValueError, KeyError, OSError) as err:
            if conn is not None and conn.in_transaction:
                conn.execute("ROLLBACK")
            print(f"refused: {type(err).__name__}; nothing written", file=sys.stderr)
            return 1
        finally:
            if conn is not None:
                conn.close()


def _undo(conn, root, args, now, snapshot):
    receipts = junk_slang.receipts(conn)
    if args.undo not in receipts:
        print(f"refused: no receipt {args.undo}; nothing written")
        return 1
    if receipts[args.undo].get("undone_at") is not None:
        print(f"receipt {args.undo} is already undone; nothing written")
        return 0
    result = junk_slang.undo(conn, args.undo, now=now, write=False)
    print(f"receipt {args.undo}: restore {result.terms} terms ({result.terms_changed} changed since, "
          f"left alone), {result.adopters} adopters, {result.memories} memories "
          f"({result.memories_changed} changed since, left alone)")
    if not args.go:
        print("dry run, nothing written")
        return 0
    snap = snapshot(conn, root / "snapshots", "pre-clean-junk-undo")
    conn.execute("BEGIN IMMEDIATE")
    junk_slang.undo(conn, args.undo, now=now)
    conn.execute("COMMIT")
    print(f"written: receipt {args.undo} undone; snapshot {snap}")
    return 0
