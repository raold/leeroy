"""`python -m leeroy_mind tuning --data-dir D status|history|undo N|revert N`: the owner's dials
from a terminal. status and history read mind.db read-only and never migrate it. undo and revert
are for when the mind is stopped (while it runs, the console asks it to): they hold the
instance lock throughout, refuse a database older than this code (run `db migrate` first), and
take a snapshot before writing one change with source "cli". The running mind reads it at its
next start."""
import argparse
import json
import sqlite3
import sys
from contextlib import closing
from datetime import datetime
from pathlib import Path

from leeroy_mind.clock import SystemClock
from leeroy_mind.config import ConfigError, default_data_dir, load_config

from . import changes as C
from . import groups as G
from .dials import MEMBER_DIALS, WORLD_DIALS
from .state import TABLES, Tuning
from .world import file_values


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="leeroy_mind tuning", description=__doc__.split("\n")[0])
    p.add_argument("--data-dir", help="the mind's data dir (mind-data)")
    sub = p.add_subparsers(dest="action", required=True)
    sub.add_parser("status", help="the values in force, read-only")
    h = sub.add_parser("history", help="the change log, newest first, read-only")
    h.add_argument("--limit", type=int, default=30)
    n = sub.add_parser("nudges", help="active private nudges and their history, read-only")
    n.add_argument("--limit", type=int, default=30)
    e = sub.add_parser("edits", help="godmode edits in force and their history, read-only")
    e.add_argument("--limit", type=int, default=30)
    u = sub.add_parser("undo", help="reverse one change (the mind must be stopped)")
    u.add_argument("change", type=int)
    r = sub.add_parser("revert", help="every value back to right after a change (0: none)")
    r.add_argument("to", type=int)
    return p


def _read(db: Path):
    conn = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _has_tables(conn) -> bool:
    have = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    return set(TABLES) <= have


def _when(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def _names(conn) -> dict[int, str]:
    from leeroy_mind.personas.tiers import cast_sql
    return {r[0]: r[1] for r in conn.execute("SELECT guid,name FROM cast_member WHERE " + cast_sql(conn))}


def _status(conn, cfg) -> None:
    t = Tuning.load(conn)
    names = _names(conn)
    from leeroy_mind.personas.tiers import cast_sql
    versions = {r[0]: r[1] for r in conn.execute("SELECT guid,version FROM persona_core WHERE " + cast_sql(conn))}
    print(f"tuning version {t.version}")
    files = file_values(cfg) if cfg is not None else {}
    for name in WORLD_DIALS:
        if name in t.world:
            print(f"  world: {name} {t.world[name]} (mind.toml: {files.get(name, '?')})")
    for target, offsets in sorted(t.offsets.items()):
        g = t.groups.get(target)
        label = g.name if g else target
        where = G.describe(g.where) if g else ""
        for dial, v in sorted(offsets.items()):
            print(f"  group {label}{f' ({where})' if where and not g.builtin else ''}: "
                  f"{dial} {v:+d}")
    print(f"  {len(t.pins)} member{'' if len(t.pins) == 1 else 's'} with values of their own")
    for guid, pins in sorted(t.pins.items()):
        for dial, v in sorted(pins.items(), key=lambda kv: list(MEMBER_DIALS).index(kv[0])):
            stale = (t.pin_versions.get((guid, dial)) is not None
                     and versions.get(guid) != t.pin_versions.get((guid, dial)))
            print(f"  {names.get(guid, '?')} ({guid}): {dial} {v}"
                  + ("  [the persona changed since this was set]" if stale else ""))


def _history(conn, limit: int) -> None:
    names = _names(conn)
    for c in C.history(conn, limit):
        head = f"#{c['id']} {_when(c['ts'])} {c['source']} {c['label']}"
        if c["undone_by"]:
            head += f" (undone by #{c['undone_by']})"
        print(head)
        for r in c["rows"]:
            if r["dial"] == C.DEFINITION:
                what = (f"group {r['target']} {'retired' if r['after'] is None else 'saved as '}"
                        + ("" if r["after"] is None else
                           f"{r['after']['name']} ({G.describe(r['after']['where'])})"))
            else:
                who = ("world" if r["scope"] == "world" else
                       f"group {r['target']}" if r["scope"] == "group" else
                       f"{names.get(int(r['target']), '?')} ({r['target']})")
                b = "unset" if r["before"] is None else r["before"]
                a = "unset" if r["after"] is None else r["after"]
                what = f"{who} {r['dial']} {b} -> {a}"
            print(f"    {what}")
        for e in c.get("edits", ()):
            print(f"    edit #{e['id']} {e['kind']} {names.get(e['member'], '?')} ({e['member']}): "
                  f"{e['from']} -> {e['to']}")


def _write(root: Path, args) -> int:
    from leeroy_mind.app import AlreadyRunning, acquire_instance_lock, instance_lock_held
    from leeroy_mind.personas.io import load_persona
    from leeroy_mind.storage.db import latest_version, open_db, snapshot
    from leeroy_mind.storage.store import Store
    db = root / "mind.db"
    if instance_lock_held(root):
        print("the mind is running: change dials from the console, which asks the running "
              "mind", file=sys.stderr)
        return 1
    try:
        lock = acquire_instance_lock(root)
    except AlreadyRunning:
        print("the mind is running", file=sys.stderr)
        return 1
    with lock:
        with closing(sqlite3.connect(db)) as raw:
            raw.row_factory = sqlite3.Row
            have = raw.execute("PRAGMA user_version").fetchone()[0]
            if have < latest_version():
                print(f"mind.db is schema v{have}, this code is v{latest_version()}: run "
                      "`db migrate` first", file=sys.stderr)
                return 1
            print(f"snapshot {snapshot(raw, root / 'snapshots', 'tuning')}")
        store = Store(open_db(db), clock=SystemClock())
        try:
            pic = C.picture(store, {g: p for g in store.persona_versions()
                                    if (p := load_persona(store, g)) is not None})
            now = store.clock.now()
            from leeroy_mind.nightly.settings import load_life
            try:
                dial = load_life(root / "mind.toml")
            except Exception:
                dial = 5
            try:
                if args.action == "undo":
                    r = C.undo(store, args.change, pic, source="cli", now=now, dial=dial)
                    what = f"undo #{args.change}"
                else:
                    r = C.revert_to(store, args.to, pic, source="cli", now=now, dial=dial)
                    what = f"revert to #{args.to}"
            except (C.TuneError, ValueError) as e:  # an edit's refusal (tuning.edits) too
                print(f"{'undo' if args.action == 'undo' else 'revert'}: {e}", file=sys.stderr)
                return 1
            print(f"{what}: " + (f"saved as change #{r.change}" if r.change
                                 else "nothing to change"))
            return 0
        finally:
            store.close()


def tuning_main(argv=None) -> int:
    args = _parser().parse_args(argv)
    root = Path(args.data_dir).resolve() if args.data_dir else default_data_dir().resolve()
    db = root / "mind.db"
    if not db.exists():
        print(f"no mind.db at {db}", file=sys.stderr)
        return 1
    if args.action in ("undo", "revert"):
        return _write(root, args)
    try:
        cfg = load_config(root / "mind.toml")
    except ConfigError:
        cfg = None
    with closing(_read(db)) as conn:
        if not _has_tables(conn):
            print("this mind.db has no tuning tables yet (run `db migrate` with the mind "
                  "stopped)")
            return 0
        if args.action == "status":
            _status(conn, cfg)
        elif args.action == "edits":
            from .edits import view as edit_view
            names = _names(conn)
            data = edit_view(conn, now=SystemClock().now(), limit=max(1, min(200, args.limit)))
            for e in data["active"]:
                ends = f" until {_when(e['ends'])}" if e.get("ends") else ""
                print(f"#{e['id']} {e['kind']} {names.get(e['member'], '?')} ({e['member']}){ends}: "
                      + json.dumps(e["args"], ensure_ascii=False, sort_keys=True))
            if not data["active"]:
                print("No edits in force.")
            for c in data["history"]:
                print(f"change #{c['change']} {_when(c['ts'])} {c['label']}")
                for m in c["edits"]:
                    print(f"  edit #{m['id']} {m['kind']}: {m['from']} -> {m['to']}")
        elif args.action == "nudges":
            from .nudges import view
            data = view(conn, now=SystemClock().now(), limit=max(1, min(200, args.limit)))
            for n in data["active"]:
                print(f"#{n['id']} {n['target_kind']} {n['target']} {n['strength']} "
                      f"{n['scope']} until {_when(n['ends'])}: {n['text']}")
            if not data["active"]:
                print("No active nudges.")
            for c in data["history"]:
                print(f"change #{c['change']} {_when(c['ts'])} {c['label']}")
                for m in c["moves"]:
                    n = m["after"] or m["before"]
                    print(f"  nudge #{m['id']} {n['target_kind']} {n['target']}: {n['text']}")
        else:
            _history(conn, args.limit)
    return 0
