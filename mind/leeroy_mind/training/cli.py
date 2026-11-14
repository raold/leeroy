"""Offline training inspection. Mutations hold the service's OS lock throughout."""

import argparse
import asyncio
from contextlib import closing, nullcontext
from datetime import datetime, timezone
import json
import re
import sqlite3
from types import SimpleNamespace

from leeroy_mind.clock import SystemClock
from leeroy_mind.config import ConfigError
from leeroy_mind.guild.raids import CATALOG, validate
from leeroy_mind.wow import BOSS_NAMES, is_raid_fight

from .config import load_training_config
from .experiments import Experimenter
from .inspect import cards_view, checklist, gate_state, trials_view
from .mechanics import boss_mechanics
from .report import training_section
from .service import load_book, rescore_stored
from . import store


def _week():
    year, week, _ = datetime.now(timezone.utc).isocalendar()
    return f"{year}-W{week:02d}"


def reset_gate(conn):
    """A fresh experiment discards gate evidence and raid parity, atomically."""
    with conn:
        conn.execute("DELETE FROM tg_perf WHERE fight GLOB 'gate:*'")
        conn.execute("DELETE FROM meta WHERE key='tg_gate' OR key GLOB 'tg_gate:raid:*' "
                     "OR key GLOB 'tg_gate:family:*' OR key GLOB 'tg_gate:perf:*'")


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


def _context(value):
    """Canonical supported boss or pooled raid scope; never invent a context."""
    if not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]{0,3}:(?:0|[1-9][0-9]?|\*):[0-3]", value):
        raise ValueError("invalid training context")
    map_text, boss, difficulty_text = value.split(":")
    map_id, difficulty = int(map_text), int(difficulty_text)
    raid = next((r for r in CATALOG.values() if r.map_id == map_id), None)
    if (raid is None or validate(raid, 25 if difficulty % 2 else 10, difficulty >= 2, raid.min_tier)
            or boss != "*" and ((map_id, int(boss)) not in BOSS_NAMES or not is_raid_fight(map_id, int(boss)))):
        raise ValueError("unsupported training context")
    return value


def _lab_owned(conn):
    """The lab's own record in brief, with the native receipts `lab-clear` would read."""
    from .lab_runner import NATIVE_PREFIX, NATIVE_START_KEY, OWNED_KEY

    def meta(key):
        row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        try:
            return json.loads(row["value"]) if row is not None else None
        except (TypeError, ValueError):
            return "unreadable"
    owned = meta(OWNED_KEY)
    if type(owned) is not dict:
        return owned
    current = owned.get("current") if type(owned.get("current")) is dict else {}
    trial = current.get("id") if type(current.get("id")) is str else None
    keys = ("phase", "halt", "night", "halted_at", "guids", "attempts", "recovered")
    return {key: owned.get(key) for key in keys} | {
        "trial": trial, "ack": current.get("ack"), "ack_error": current.get("ack_error"),
        "terminal_reason": current["terminal"].get("reason") if type(current.get("terminal")) is dict else None,
        "native_cleanup": meta(NATIVE_PREFIX + trial) if trial else None,
        "native_start": meta(NATIVE_START_KEY)}


async def _rescore(conn, root):
    playbooks = load_training_config(root / "mind.toml").playbooks
    book = await asyncio.to_thread(load_book, root)
    return await rescore_stored(SimpleNamespace(conn=conn), book, playbooks=playbooks)


def training_main(argv=None):
    from leeroy_mind.__main__ import _add_data_dir, _root
    from leeroy_mind.app import AlreadyRunning, acquire_instance_lock
    parser = argparse.ArgumentParser(prog="leeroy_mind training")
    _add_data_dir(parser)
    subs = parser.add_subparsers(dest="command", required=True)
    for name in ("status", "gate", "rescore", "report", "spells", "cards", "lessons", "voicecheck",
                 "trials", "rollback", "stock", "lab", "lab-clear"):
        sub = subs.add_parser(name)
        _add_data_dir(sub, after_action=True)
        if name == "gate":
            sub.add_argument("--reset", action="store_true")
        elif name == "report":
            sub.add_argument("--week", default=_week())
        elif name == "spells":
            actions = sub.add_subparsers(dest="action", required=True)
            _add_data_dir(actions.add_parser("check"), after_action=True)
        elif name == "voicecheck":
            sub.add_argument("--night", required=True)
        elif name in {"cards", "lessons", "trials", "rollback", "stock"}:
            sub.add_argument("--context", required=name in {"lessons", "rollback", "stock"})
    args = parser.parse_args(argv)
    root = _root(args)
    write = args.command in {"rescore", "rollback", "stock", "lab-clear"} or args.command == "gate" and args.reset
    try:
        # Acquisition is the exclusion test. Keep this exact lock until all IO
        # and scoring has completed and the SQLite connection is closed.
        with acquire_instance_lock(root) if write else nullcontext():
            with closing(_open(root, write=write)) as conn:
                if args.command == "gate":
                    if args.reset:
                        reset_gate(conn)
                    result = gate_state(conn)
                elif args.command == "rescore":
                    result = {"rescored": asyncio.run(_rescore(conn, root))}
                elif args.command == "voicecheck":
                    from .voice_lead import voicecheck
                    result = voicecheck(conn, args.night)
                elif args.command == "trials":
                    result = trials_view(conn, _context(args.context) if args.context else None)
                elif args.command == "lab":
                    rotations = [dict(r) for r in conn.execute(
                        "SELECT id,name,class,spec_tab,status,origin,created FROM tg_rotation ORDER BY id DESC LIMIT 200")]
                    nights = []
                    for row in conn.execute("SELECT key,value FROM meta WHERE key GLOB 'tg:lab:summary:*' ORDER BY key DESC LIMIT 8"):
                        try:
                            value = json.loads(row["value"])
                            if type(value) is dict:
                                nights.append(value)
                        except (TypeError, ValueError):
                            continue
                    result = {"rotations": rotations, "nights": nights,
                              "dummy_rows": conn.execute("SELECT count(*) FROM tg_dummy").fetchone()[0],
                              "measurement": "requested_pre_ai proxy; actual DPS and field improvement unavailable",
                              "owned": _lab_owned(conn)}
                elif args.command == "lab-clear":
                    # Stored native receipts only; never the bridge. Refusals change nothing.
                    from .lab_recovery import LabClearRefused, clear_blocked_lab
                    try:
                        result = clear_blocked_lab(conn, SystemClock().now())
                    except LabClearRefused as refused:
                        print(f"training lab-clear: refused: {refused}")
                        return 1
                elif args.command in {"rollback", "stock"}:
                    context = _context(args.context)
                    ports = SimpleNamespace(conn=conn, clock=SystemClock())
                    experimenter = Experimenter(ports, load_training_config(root / "mind.toml"))
                    changed = asyncio.run(getattr(experimenter, args.command)(context))
                    result = {"context": context, "changed": changed,
                              "applied_commands_unchanged": True,
                              "pending_cleanup": conn.execute("SELECT count(*) FROM tg_applied WHERE undone IS NULL").fetchone()[0]}
                elif args.command == "cards":
                    result = cards_view(conn, args.context)
                elif args.command == "lessons":
                    result = {"context": args.context, "lessons": [dict(row) for row in store.lessons(conn, args.context)]}
                    for row in result["lessons"]:
                        row["evidence"] = json.loads(row["evidence"])
                elif args.command == "spells":
                    async def read():
                        return await asyncio.to_thread(load_book, root)
                    book = asyncio.run(read())
                    table = boss_mechanics(load_training_config(root / "mind.toml").playbooks)
                    names = sorted({name for rows in table.values() for row in rows
                                    for name in (*row.names, *row.exempt)})
                    result = {"loaded": sorted(book.loaded), "spells": {name: sorted(book.ids(name)) for name in names}}
                else:
                    week = args.week if args.command == "report" else _week()
                    result = training_section(conn, week, now=SystemClock().now(), names=lambda guid: str(guid))
                    if args.command == "status":
                        result["checklist"] = [
                            f"[{'x' if item['done'] else ' '}] {item['label']}"
                            + (" (waiting for recording)" if item["waiting"] else "")
                            for item in checklist(result["done_when"])]
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
