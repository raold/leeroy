"""Read-only Realm diagnostics. No application, migration or service startup."""

import argparse
from contextlib import closing
import json
import sqlite3

from .config import load_realm_config
from .ports import armory_from_config, title_holders
from .store import recent_receipts

TABLES = (
    "realm_guild", "realm_actor", "realm_raider", "realm_fact", "realm_knows",
    "realm_progress", "realm_grudge", "realm_post", "realm_line", "realm_issue",
    "realm_stance", "realm_plan", "realm_event", "realm_rsvp", "realm_calendar",
    "realm_mail", "realm_bank", "realm_arena", "realm_match", "realm_recruit",
    "realm_step", "realm_canon", "realm_canon_kill", "realm_kick", "realm_meeting",
    "realm_goal", "realm_notable", "realm_receipt",
)


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


def _status(conn):
    present = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not set(TABLES) <= present:
        raise RealmSchemaUnavailable("realm schema unavailable")
    # Identifiers are the closed constant set above, never supplied by a user.
    counts = {name: conn.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0]
              for name in TABLES}
    approvals = [dict(row) for row in conn.execute(
        "SELECT name,sha256,approved,applied FROM realm_step ORDER BY approved DESC,name LIMIT 100")]
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


def realm_main(argv=None) -> int:
    from leeroy_mind.__main__ import _add_data_dir, _root

    parser = argparse.ArgumentParser(prog="leeroy_mind realm")
    _add_data_dir(parser)
    subs = parser.add_subparsers(dest="command", required=True)
    for name in ("status", "titles"):
        _add_data_dir(subs.add_parser(name), after_action=True)
    args = parser.parse_args(argv)
    try:
        root = _root(args)
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
