"""Realm diagnostics and the data steps' command surface. No application, migration or service
startup.

status and titles read mind.db read-only. plan dispatches to a step's own plan_cli, which reads
MySQL and mind.db read-only and writes only its plan file; approve and cards touch only files
under mind-data/realm/, never mind.db; canon history-ok dispatches to canon's history_ok_cli. A
step that uses the live model exits 2 (night_window) while P1c's night holds the GPU (M14).
"""

import argparse
from contextlib import closing
from dataclasses import dataclass
from datetime import date, timedelta
import importlib
import importlib.util
import json
from pathlib import Path
import sqlite3

from leeroy_mind.clock import SystemClock, night_of

from . import steps
from .config import load_realm_config
from .ports import armory_from_config, title_holders
from .store import recent_receipts

TABLES = (
    "realm_guild", "realm_actor", "realm_raider", "realm_fact", "realm_knows",
    "realm_progress", "realm_grudge", "realm_post", "realm_line", "realm_issue",
    "realm_stance", "realm_plan", "realm_event", "realm_rsvp", "realm_calendar",
    "realm_mail", "realm_bank", "realm_arena", "realm_match", "realm_recruit",
    "realm_step", "realm_canon", "realm_canon_kill", "realm_kick", "realm_meeting",
    "realm_goal", "realm_notable", "realm_receipt", "realm_direct", "realm_rule_origin",
    "realm_benchmark_seen",
)


@dataclass(frozen=True, slots=True)
class Entry:
    """A dispatched command: leeroy_mind.realm.<module>.<function>(ctx: CommandContext) -> int."""
    module: str
    function: str
    uses_model: bool


# The steps' own modules arrive with their tasks; until one exists, its command exits 1.
PLAN_STEPS = {
    "rivals": Entry("rivals", "plan_cli", True),  # the cards are written by the live model
    "canon": Entry("canon", "plan_cli", False),
    "bank": Entry("bank", "plan_cli", False),
    "bank-stock": Entry("bank", "plan_cli", False),
}
CANON_ACTIONS = {
    "history-ok": Entry("canon", "history_ok_cli", False),
}


@dataclass(frozen=True, slots=True)
class CommandContext:
    """A dispatched command's inputs: its registered name, its own arguments, the data dir."""
    name: str
    argv: tuple[str, ...]
    root: Path
    clock: object

    def connect(self) -> sqlite3.Connection:
        """mind.db read-only (mode=ro and query_only); the caller closes it."""
        return _open(self.root)

    def write_plan(self, step: str, payload) -> Path:
        return steps.write_plan(self.root, step, payload, local=self.clock.local())


class RealmSchemaUnavailable(RuntimeError):
    pass


def _open(root):
    conn = sqlite3.connect((root / "mind.db").resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only=ON")
    except BaseException:
        conn.close()
        raise
    return conn


def _result(text):
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return {"unreadable": True}
    return value


def _status(conn):
    present = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not set(TABLES) <= present:
        raise RealmSchemaUnavailable("realm schema unavailable")
    # Identifiers are the closed constant set above, never supplied by a user.
    counts = {name: conn.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0]
              for name in TABLES}
    approvals = [dict(row, result=_result(row["result"])) for row in conn.execute(
        "SELECT name,sha256,approved,applied,result FROM realm_step "
        "ORDER BY approved DESC,name LIMIT 100")]
    failures = [dict(command_id=row.command_id, kind=row.kind, stage=row.stage,
                     reason=row.reason, value=row.value, ts=row.ts)
                for row in recent_receipts(conn, failures_only=True)]
    deferrals = [dict(row) for row in conn.execute(
        "SELECT achievement,guid,reason,ts,tries,state FROM realm_canon "
        "WHERE state IN ('planned','blocked','refused') ORDER BY ts DESC,achievement,guid LIMIT 100")]
    return dict(counts=counts, approvals=approvals, failures=failures, grant_deferrals=deferrals)


def _titles(conn, config_path):
    cfg = load_realm_config(config_path)
    result = {"titles": title_holders(conn), "guild_id": 21,
              "trial_rank": cfg.trial_rank, "ranks": None,
              "trial_rank_exists": None, "armory": "unavailable"}
    try:
        armory = armory_from_config(config_path)
        if armory is None:
            return result, 1
        ranks = armory.guild_ranks(21)
    except Exception:
        # External tooling/configuration failures may carry credentials or paths.
        return result, 1
    result.update(ranks=ranks, trial_rank_exists=cfg.trial_rank in ranks, armory="available")
    return result, 0


def night_holds(conn, clock) -> str | None:
    """The night whose run holds the GPU, or would as soon as he has been offline its grace;
    None when the GPU is free of it. This is NightScheduler.due() read from night_run, without
    presence or the arbiter (the CLI runs while he is offline), except that a failed run waiting
    out its retry back-off still holds. ports.night_window() cannot answer it: P1c's 06:00-06:00
    night bounds always contain now."""
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='night_run'").fetchone() is None:
        return None
    from leeroy_mind.nightly.scheduler import RESUME_NIGHTS
    from leeroy_mind.nightly.settings import NightlySettings as settings
    now, tonight = clock.now(), night_of(clock.local())
    oldest = (date.fromisoformat(tonight) - timedelta(days=RESUME_NIGHTS - 1)).isoformat()
    row = conn.execute("SELECT night FROM night_run WHERE status IN ('running','interrupted') "
                       "AND night BETWEEN ? AND ? ORDER BY night LIMIT 1", (oldest, tonight)).fetchone()
    night = row[0] if row is not None else tonight
    run = conn.execute("SELECT status,attempts FROM night_run WHERE night=?", (night,)).fetchone()
    if run is None:
        end = conn.execute("SELECT max(window_end) FROM night_run "
                           "WHERE status IN ('done','abandoned')").fetchone()[0]
        if now - (end or 0.0) < settings.min_window_hours * 3600:
            return None
    elif run[0] == "done" or (run[0] == "failed" and run[1] >= settings.max_attempts):
        return None
    finished = conn.execute("SELECT max(finished) FROM night_run WHERE status='done'").fetchone()[0]
    if finished is not None and now - finished < settings.min_gap_hours * 3600:
        return None
    return night


def load_entry(entry: Entry):
    """The entry's function, or None when its module or function is not in this build."""
    name = f"{__package__}.{entry.module}"
    try:
        spec = importlib.util.find_spec(name)
    except (ImportError, ValueError):
        spec = None
    if spec is None:
        return None
    function = getattr(importlib.import_module(name), entry.function, None)
    return function if callable(function) else None


def _take_data_dir(args):
    """--data-dir may also follow a dispatched command's own arguments."""
    taker = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    taker.add_argument("--data-dir", default=argparse.SUPPRESS)
    known, rest = taker.parse_known_args(args.args)
    if hasattr(known, "data_dir"):
        args.data_dir = known.data_dir
    args.args = rest


def _dispatch(args, clock, root_of):
    _take_data_dir(args)
    registry, name, label = ((PLAN_STEPS, args.step, "step") if args.command == "plan"
                             else (CANON_ACTIONS, args.action, "canon action"))
    entry = registry.get(name)
    if entry is None:  # Never echo what was typed; name only what exists.
        print(f"realm: unknown {label}; known: {', '.join(sorted(registry))}")
        return 1
    try:
        root = root_of(args)
        if entry.uses_model:
            try:
                with closing(_open(root)) as conn:
                    night = night_holds(conn, clock)
            except sqlite3.Error:
                print(f"realm: {label} {name} refused: the night's state is unreadable")
                return 1
            if night is not None:
                print(f"realm: night_window: the night of {night} holds the GPU; "
                      f"run {name} after its run is done")
                return 2
        function = load_entry(entry)
        if function is None:
            print(f"realm: {label} {name} is not available in this build")
            return 1
        code = function(CommandContext(name, tuple(args.args), root, clock))
    except steps.StepError as error:
        print(f"realm: {error}")
        return 1
    except Exception:
        # Tooling and configuration failures may carry credentials or paths.
        print(f"realm: {label} {name} failed")
        return 1
    return 0 if code is None else int(code)


def _plan_path(root, file):
    path = Path(file)
    if not path.exists() and path.name == file:
        return steps.plans_dir(root) / file
    return path


def _approve(args, clock, root_of):
    try:
        root = root_of(args)
        target = steps.approve(root, _plan_path(root, args.file), now=clock.now())
        approval = json.loads(target.read_text(encoding="utf-8"))
    except steps.StepError as error:
        print(f"realm: {error}")
        return 1
    except Exception:
        print("realm: approval failed")
        return 1
    print(json.dumps({"approved": target.name, **approval}, sort_keys=True, indent=2))
    return 0


def _reject_constant(value):
    raise ValueError("non-finite number")


def _cards(root, show):
    directory = root / "realm" / "cards"
    files = sorted(p for p in directory.glob("*.json") if p.is_file()) if directory.is_dir() else []
    if not show:
        return {"cards": [p.stem for p in files], "count": len(files)}
    cards = {}
    for path in files:
        try:
            cards[path.stem] = json.loads(path.read_text(encoding="utf-8"),
                                          parse_constant=_reject_constant)
        except (OSError, UnicodeError, ValueError):
            cards[path.stem] = {"error": "unreadable"}
    return {"cards": cards, "count": len(files)}


def realm_main(argv=None, *, clock=None) -> int:
    from leeroy_mind.__main__ import _add_data_dir, _root

    clock = SystemClock() if clock is None else clock
    parser = argparse.ArgumentParser(prog="leeroy_mind realm")
    _add_data_dir(parser)
    subs = parser.add_subparsers(dest="command", required=True)
    for name in ("status", "titles"):
        _add_data_dir(subs.add_parser(name), after_action=True)
    plan = subs.add_parser("plan", help="dry-run a data step: read-only, writes its plan file")
    plan.add_argument("step")
    plan.add_argument("args", nargs=argparse.REMAINDER)
    approve = subs.add_parser("approve", help="approve one plan file: writes an approval file only")
    approve.add_argument("file")
    cards = subs.add_parser("cards", help="the rival actors' cards")
    cards.add_argument("--show", action="store_true")
    canon = subs.add_parser("canon", help="canon approvals, such as history-ok <seed> <canon>")
    canon.add_argument("action")
    canon.add_argument("args", nargs=argparse.REMAINDER)
    for sub in (plan, approve, cards, canon):
        _add_data_dir(sub, after_action=True)
    args = parser.parse_args(argv)
    if args.command in ("plan", "canon"):
        return _dispatch(args, clock, _root)
    if args.command == "approve":
        return _approve(args, clock, _root)
    try:
        root = _root(args)
        if args.command == "cards":
            print(json.dumps(_cards(root, args.show), sort_keys=True, indent=2, allow_nan=False))
            return 0
        with closing(_open(root)) as conn:
            if args.command == "status":
                result, code = _status(conn), 0
            else:
                result, code = _titles(conn, root / "mind.toml")
            print(json.dumps(result, sort_keys=True, indent=2, allow_nan=False))
            return code
    except RealmSchemaUnavailable:
        print("realm: schema unavailable; database was not migrated")
        return 1
    except Exception:
        print("realm: command unavailable; database was not migrated")
        return 1
