"""Offline training inspection. Mutations hold the service's OS lock throughout."""

import argparse
import asyncio
from contextlib import closing, nullcontext
from datetime import datetime, timezone
import json
import sqlite3
from types import SimpleNamespace

from leeroy_mind.clock import SystemClock
from leeroy_mind.config import ConfigError

from .mechanics import BOSS_MECHANICS
from .report import training_section
from .service import load_book, rescore_stored
from . import store


def _week():
    year, week, _ = datetime.now(timezone.utc).isocalendar()
    return f"{year}-W{week:02d}"


def _gate(conn):
    row = conn.execute("SELECT value FROM meta WHERE key='tg_gate'").fetchone()
    return json.loads(row[0]) if row else {"phase": "ab", "raids": 0, "verdict_ms": None}


def reset_gate(conn):
    """A fresh experiment discards gate evidence and raid parity, atomically."""
    with conn:
        conn.execute("DELETE FROM tg_perf WHERE fight GLOB 'gate:*'")
        conn.execute("DELETE FROM meta WHERE key='tg_gate' OR key GLOB 'tg_gate:raid:*' "
                     "OR key GLOB 'tg_gate:perf:*'")


def _open(root, *, write):
    # mode=rw also refuses a missing DB; neither path migrates or initializes it.
    conn = sqlite3.connect((root / "mind.db").resolve().as_uri() + ("?mode=rw" if write else "?mode=ro"), uri=True)
    conn.row_factory = sqlite3.Row
    if not write:
        conn.execute("PRAGMA query_only=ON")
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='tg_pull'").fetchone() is None:
        conn.close()
        raise ValueError("training schema is unavailable; this command never migrates databases")
    return conn


async def _rescore(conn, root):
    book = await asyncio.to_thread(load_book, root)
    return await rescore_stored(SimpleNamespace(conn=conn), book)


def training_main(argv=None):
    from leeroy_mind.__main__ import _add_data_dir, _root
    from leeroy_mind.app import AlreadyRunning, acquire_instance_lock
    parser = argparse.ArgumentParser(prog="leeroy_mind training")
    _add_data_dir(parser)
    subs = parser.add_subparsers(dest="command", required=True)
    for name in ("status", "gate", "rescore", "report", "spells", "cards", "lessons", "voicecheck"):
        sub = subs.add_parser(name)
        _add_data_dir(sub, after_action=True)
        if name == "gate":
            sub.add_argument("--reset", action="store_true")
        elif name == "report":
            sub.add_argument("--week", default=_week())
        elif name == "spells":
            actions = sub.add_subparsers(dest="action", required=True)
            _add_data_dir(actions.add_parser("check"), after_action=True)
        elif name in {"cards", "lessons"}:
            sub.add_argument("--context", required=name == "lessons")
        elif name == "voicecheck":
            sub.add_argument("--night", required=True)
    args = parser.parse_args(argv)
    root = _root(args)
    write = args.command == "rescore" or args.command == "gate" and args.reset
    try:
        # Acquisition is the exclusion test. Keep this exact lock until all IO
        # and scoring has completed and the SQLite connection is closed.
        with acquire_instance_lock(root) if write else nullcontext():
            with closing(_open(root, write=write)) as conn:
                if args.command == "gate":
                    if args.reset:
                        reset_gate(conn)
                    result = _gate(conn)
                elif args.command == "rescore":
                    result = {"rescored": asyncio.run(_rescore(conn, root))}
                elif args.command == "voicecheck":
                    from .voice_lead import voicecheck
                    result = voicecheck(conn, args.night)
                elif args.command == "cards":
                    fields = "id,context,version,lineage,status,origin,directives,call,assignment,reason,evidence,vetoes,prev_champion,created,changed"
                    rows = conn.execute("SELECT " + fields + " FROM tg_card"
                        + (" WHERE context=?" if args.context else "") + " ORDER BY context,version,id LIMIT 200",
                        (args.context,) if args.context else ()).fetchall()
                    result = {"cards": [dict(row) for row in rows]}
                    for row in result["cards"]:
                        for field in ("directives", "assignment", "evidence", "vetoes"):
                            row[field] = json.loads(row[field])
                elif args.command == "lessons":
                    result = {"context": args.context, "lessons": [dict(row) for row in store.lessons(conn, args.context)]}
                    for row in result["lessons"]:
                        row["evidence"] = json.loads(row["evidence"])
                elif args.command == "spells":
                    async def read():
                        return await asyncio.to_thread(load_book, root)
                    book = asyncio.run(read())
                    names = sorted({name for rows in BOSS_MECHANICS.values() for row in rows
                                    for name in (*row.names, *row.exempt)})
                    result = {"loaded": sorted(book.loaded), "spells": {name: sorted(book.ids(name)) for name in names}}
                else:
                    week = args.week if args.command == "report" else _week()
                    result = training_section(conn, week, now=SystemClock().now(), names=lambda guid: str(guid))
                    if args.command == "status":
                        monday = datetime.fromisocalendar(int(week[:4]), int(week[6:]), 1).replace(tzinfo=timezone.utc).timestamp()
                        result["week"] = week
                        result["pulls_by_kind"] = {row[0]: row[1] for row in conn.execute(
                            "SELECT kind,count(*) FROM tg_pull WHERE start_ms>=? AND start_ms<? GROUP BY kind",
                            (int(monday * 1000), int((monday + 7 * 86400) * 1000)))}
                        result["farm_contexts"] = sorted({row["context"] for row in result["contexts"] if row["farm"]})
                print(json.dumps(result, sort_keys=True, indent=2, allow_nan=False))
        return 0
    except AlreadyRunning:
        print("training: mind.lock is held; stop the mind before changing training data")
    except (OSError, sqlite3.Error, ValueError, ConfigError) as error:
        print(f"training: command unavailable ({type(error).__name__}); database was not migrated")
    return 1
