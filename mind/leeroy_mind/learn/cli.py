"""Read-only aggregate report, including schemas that predate learning."""
import argparse
from collections import defaultdict
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import time

from leeroy_mind.config import default_data_dir
from .reward import DAY, channel_kind, decayed
from .weights import available


def report(conn, *, days=14, now=None):
    now = time.time() if now is None else now
    if not available(conn):
        return {"available": False, "lines": 0, "members": [], "channels": [], "recipes": []}
    rows = conn.execute("SELECT r.guid,r.value,r.cap_weight,c.ts,c.channel,p.template_id,p.template_version,"
        "p.dials_hash,p.model_id,p.guard_verdict FROM talk_reward r "
        "JOIN chat_line c ON c.id=r.line_id LEFT JOIN talk_recipe p ON p.line_id=c.id "
        "WHERE c.ts>=? AND c.ts<=? ORDER BY c.id", (now - days * DAY, now)).fetchall()
    members, channels, recipes = (defaultdict(list) for _ in range(3))
    for guid, value, cap_weight, ts, channel, template, version, dials, model, verdict in rows:
        item = (value, decayed(value * (cap_weight if value > 0 else 1), age_seconds=now - ts))
        members[guid].append(item)
        channels[channel_kind(channel)].append(item)
        for key, field in (("template_id", template), ("template_version", version),
                           ("dials_hash", dials), ("model_id", model), ("guard_verdict", verdict)):
            recipes[(key, field)].append(item)
    def stats(values):
        return {"count": len(values), "mean": sum(v[0] for v in values) / len(values),
                "decayed_total": sum(v[1] for v in values)}
    return {"available": True, "lines": len(rows), "days": days,
            "members": [{"guid": k, **stats(v)} for k, v in sorted(members.items())],
            "channels": [{"kind": k, **stats(v)} for k, v in sorted(channels.items())],
            "recipes": [{"field": k[0], "value": k[1], **stats(v)} for k, v in
                        sorted(recipes.items(), key=lambda item: (item[0][0], str(item[0][1])))]}


def learn_main(argv=None):
    parser = argparse.ArgumentParser(prog="leeroy_mind learn")
    parser.add_argument("--data-dir")
    sub = parser.add_subparsers(dest="action", required=True)
    command = sub.add_parser("report")
    command.add_argument("--data-dir", default=argparse.SUPPRESS)
    command.add_argument("--days", type=int, default=14)
    lesson_command = sub.add_parser("lessons")
    lesson_command.add_argument("--data-dir", default=argparse.SUPPRESS)
    lesson_command.add_argument("--guid", type=int)
    arm_command = sub.add_parser("arms")
    arm_command.add_argument("--data-dir", default=argparse.SUPPRESS)
    version_command = sub.add_parser("versions", help="list saved learning-state versions (read-only)")
    version_command.add_argument("--data-dir", default=argparse.SUPPRESS)
    rollback_command = sub.add_parser("rollback", help="restore a learning-state version, with the mind stopped")
    rollback_command.add_argument("--data-dir", default=argparse.SUPPRESS)
    rollback_command.add_argument("--to", type=int, metavar="VERSION",
                                  help="the version to restore (default: the newest weekly one)")
    rollback_command.add_argument("--with-weights", action="store_true",
                                  help="also restore that version's reward weights (default: keep the current ones)")
    backfill_command = sub.add_parser("backfill-signals", help="replay stored Discord exchanges into the "
                                      "owner-response signals: a dry run unless --go (mind stopped)")
    backfill_command.add_argument("--data-dir", default=argparse.SUPPRESS)
    backfill_command.add_argument("--since", required=True, metavar="YYYY-MM-DD",
                                  help="local date to replay from (never further back than 90 days)")
    backfill_command.add_argument("--go", action="store_true", help="write: holds mind.lock, snapshots first")
    from .preference_cli import add_parsers, run
    add_parsers(sub)
    args = parser.parse_args(argv)
    if args.action == "report" and not 1 <= args.days <= 3650:
        parser.error("--days must be between 1 and 3650")
    if args.action == 'lessons' and args.guid is not None and args.guid <= 0:
        parser.error('--guid must be positive')
    if args.action == 'rollback' and args.to is not None and args.to <= 0:
        parser.error('--to must be positive')
    root = Path(args.data_dir).resolve() if args.data_dir else default_data_dir()
    if args.action == 'rollback':
        return rollback_main(root, args.to, with_weights=args.with_weights)
    if args.action == 'backfill-signals':
        from datetime import datetime
        try:
            since = datetime.strptime(args.since, "%Y-%m-%d").timestamp()
        except (ValueError, OverflowError, OSError):
            parser.error("--since must be a date, YYYY-MM-DD")
        return backfill_main(root, since, go=args.go)
    if args.action not in ('report', 'lessons', 'arms', 'versions'):
        return run(args,root)
    try:
        with closing(sqlite3.connect(f"{(root / 'mind.db').resolve().as_uri()}?mode=ro", uri=True)) as conn:
            if args.action == 'lessons':
                from .lessons import available as have_lessons, read as read_lessons
                result = {'available':have_lessons(conn), 'lessons':read_lessons(conn,guid=args.guid,now=time.time())}
            elif args.action == "arms":
                result = arms_report(conn)
            elif args.action == "versions":
                from .cycle import versions
                result = {"versions": versions(conn, limit=52)}
            else:
                result = report(conn, days=args.days)
            print(json.dumps(result, sort_keys=True, allow_nan=False))
        return 0
    except (sqlite3.Error, ValueError, OSError):
        print("learn_report_unavailable", file=sys.stderr)
        return 1


def arms_report(conn, *, now=None):
    from . import arms, bandit
    if not arms.available(conn):
        return {"available": False, "arms": [], "contexts": []}
    registry = arms.read(conn)
    keys = conn.execute("SELECT context FROM bandit_share UNION SELECT context FROM bandit_credit "
                        "UNION SELECT context FROM bandit_rejection").fetchall()
    contexts = []
    for (key,) in keys:
        posteriors = bandit.posterior(conn,None,key,now=now)
        shares = dict(conn.execute("SELECT arm_id,turns FROM bandit_share WHERE context=?",(key,)))
        for arm in registry:
            total = sum(shares.get(a.id,0) for a in registry if a.family == arm.family)
            contexts.append({"context":json.loads(key),"arm_id":arm.id,
                **posteriors.get(arm.id,dict(alpha=1.,beta=1.,uses=0,bad=0,attempts=0)),
                "turns":shares.get(arm.id,0),"share":shares.get(arm.id,0)/total if total else 0.})
    from dataclasses import asdict
    return {"available":True,"arms":[asdict(a) for a in registry],"contexts":contexts}


def backfill_main(root, since, *, go=False, now=None):
    """Replay the stored Discord exchanges since `since` into talk_signal (signals.backfill).
    A dry run opens mind.db read-only and prints counts. --go needs the mind stopped: it holds
    mind.lock throughout, snapshots first, never migrates, and writes numbers only."""
    from contextlib import nullcontext
    from leeroy_mind.app import AlreadyRunning, acquire_instance_lock
    from leeroy_mind.signals import backfill
    from leeroy_mind.signals import store as signals
    from leeroy_mind.storage.db import snapshot
    now = time.time() if now is None else now
    try:
        (root / "mind.db").open("rb").close()  # never a lock file in a data dir without a mind
        lock = acquire_instance_lock(root) if go else nullcontext()
    except AlreadyRunning:
        print("learn backfill-signals: mind.lock is held; stop the mind before --go. Nothing written.")
        return 1
    except OSError:
        print("learn backfill-signals: no mind.db in that data directory")
        return 1
    with lock:
        try:
            with closing(sqlite3.connect((root / "mind.db").resolve().as_uri() + ("?mode=rw" if go else "?mode=ro"),
                                         uri=True, timeout=5.0)) as conn:
                conn.row_factory = sqlite3.Row
                owner = conn.execute("SELECT value FROM meta WHERE key='human_guid'").fetchone()
                if owner is None or not str(owner[0]).isdigit() or int(owner[0]) <= 0:
                    print("learn backfill-signals: this mind does not know its owner yet")
                    return 1
                if not signals.available(conn):
                    print("learn backfill-signals: signals are not set up in this database")
                    return 1
                sink, facts = backfill.plan(conn, since=since, now=now, owner=int(owner[0]))
                result, new = backfill.report(conn, sink, facts)
                result["written"] = 0
                if go and new:
                    if not backfill.ready(conn):
                        print("learn backfill-signals: this mind.db is older than the code (run db migrate); "
                              "nothing written")
                        return 1
                    conn.commit()
                    snap = snapshot(conn, root / "snapshots", "pre-backfill-signals")
                    with conn:
                        result["written"] = backfill.write(conn, new)
                        conn.execute("INSERT INTO meta(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE "
                                     "SET value=excluded.value", (f"signals:backfill:{int(now)}", json.dumps(
                                         dict(since=facts["since"], written=result["written"]))))
                    result["snapshot"] = snap.name
                print(json.dumps(result, sort_keys=True, allow_nan=False))
                print("written" if result["written"] else "dry run, nothing written" if not go
                      else "nothing new; nothing written")
                return 0
        except (sqlite3.Error, ValueError, OSError) as error:
            print(f"learn backfill-signals: unavailable ({type(error).__name__}); nothing written", file=sys.stderr)
            return 1


def rollback_main(root, version=None, *, with_weights=False):
    """With the mind stopped: hold mind.lock throughout, never create or migrate mind.db."""
    from leeroy_mind.app import AlreadyRunning, acquire_instance_lock
    from leeroy_mind.logs import log
    from .cycle import rollback
    try:
        (root / "mind.db").open("rb").close()  # never a lock file in a data dir without a mind
        with acquire_instance_lock(root), closing(sqlite3.connect(
                (root / "mind.db").resolve().as_uri() + "?mode=rw", uri=True, timeout=5.0)) as conn:
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name='learn_version'").fetchone() is None:
                print("learn rollback: no learning-state versions in this database; it was not migrated")
                return 1
            try:
                with conn:
                    result = rollback(conn, version, now=time.time(), with_weights=with_weights)
            except ValueError as error:  # fixed words only; nothing typed is echoed
                print(f"learn rollback refused: {error}")
                return 1
        log("learn_rollback", restored=result["restored"], saved=result["saved"], kept=result["kept"],
            weights=int(result["weights"]), source="cli")
        print(json.dumps(result, sort_keys=True))
        return 0
    except AlreadyRunning:
        print("learn rollback: mind.lock is held; stop the mind before restoring a learning state")
    except (OSError, sqlite3.Error):
        print("learn rollback: command unavailable; nothing changed")
    return 1
