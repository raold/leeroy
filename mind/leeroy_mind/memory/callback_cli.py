"""Counterfactual replay on an in-memory SQLite backup; source is mode=ro."""
import argparse
from collections import Counter
import json
from pathlib import Path
import sqlite3
import sys
import time
import uuid

from leeroy_mind.config import default_data_dir
from .callbacks import CallbackContext, _ids, _json, _old_schema, _rows, pick_callback, record_callback

MAX_TURNS = 10000


def replay(conn, *, now, days=7, show_text=False):
    """Caller owns a scratch copy. Replay has no embeddings and invents no audience.

    Offer once at each recorded mind-origin turn, then record the hypothetical
    use. This measures availability, not whether she would say the callback or
    whether the owner would enjoy it. Existing real receipts remain in force.
    """
    report = dict(simulation=True, enabled_in_replay=True, turns=0, offered=0,
                  age_days={}, scopes={}, reasons={}, capped=False, unavailable=False)
    details = []
    try:
        conn.execute("SELECT memory_id,guid,ts FROM callback_use LIMIT 0")
        human_row = conn.execute("SELECT value FROM meta WHERE key='human_guid'").fetchone()
        human = int(human_row[0]) if human_row else 0
        if human <= 0:
            report["unavailable"] = True
            return report, details
        rows = _rows(conn, "SELECT * FROM chat_line WHERE origin='mind' AND from_guid<>? "
                     "AND ts BETWEEN ? AND ? ORDER BY ts,id LIMIT ?", (human, now-days*86400, now, MAX_TURNS+1))
        report["capped"] = len(rows) > MAX_TURNS
        ages, scopes, reasons = Counter(), Counter(), Counter()
        for row in rows[:MAX_TURNS]:
            report["turns"] += 1
            audience = _json(row.get("audience"))
            readers = tuple(audience) if _ids(audience) else None
            if row["channel"] == "whisper":
                present = (row["from_guid"], row["to_guid"])
            elif row["channel"] == "guild":
                # Stored audiences contain cast members only (WorldView.audience).
                # The turn's own session, not an unrelated open session, proves
                # whether the owner was there too, including with a known audience.
                owner_present = conn.execute("SELECT 1 FROM session WHERE id=? AND started<=? "
                    "AND (ended IS NULL OR ended>=?)", (row["session_id"], row["ts"], row["ts"])).fetchone()
                present = tuple(dict.fromkeys((*(readers or (row["from_guid"],)),
                                               *((human,) if owner_present else ()))))
            else:
                present = readers or ()
            ctx = CallbackContext(True, row["channel"], row["to_guid"], present, readers)
            chosen = pick_callback(conn, row["from_guid"], ctx, now=row["ts"])
            if chosen is None or not record_callback(conn, row["from_guid"], ctx, chosen, now=row["ts"]):
                continue
            report["offered"] += 1
            bucket = "2-7" if chosen.age_days < 7 else "7-14" if chosen.age_days < 14 else "14-30" if chosen.age_days < 30 else "30-60"
            ages[bucket] += 1
            scopes[chosen.scope] += 1
            reasons[chosen.reason] += 1
            if show_text:
                details.append(dict(line_id=row["id"], memory_id=chosen.memory_id, seed=chosen.seed))
        report.update(age_days=dict(ages), scopes=dict(scopes), reasons=dict(reasons))
    except sqlite3.OperationalError as exc:
        if not _old_schema(exc):
            raise
        report["unavailable"] = True
    return report, details


def _write_text(root, details):
    # Root is an explicit resolved data directory; only its direct scratch child
    # receives opt-in text. Never follow a scratch symlink or Windows junction.
    scratch = root / "scratch"
    if scratch.is_symlink() or scratch.is_junction() or scratch.resolve().parent != root:
        raise ValueError("callback_scratch_path_invalid")
    scratch.mkdir(exist_ok=True)
    target = scratch / ("callbacks-" + uuid.uuid4().hex + ".json")
    with target.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(details, handle, ensure_ascii=False)
        handle.write("\n")


def callbacks_main(argv=None):
    parser = argparse.ArgumentParser(prog="leeroy_mind callbacks")
    parser.add_argument("--data-dir")
    sub = parser.add_subparsers(dest="action", required=True).add_parser("eval")
    sub.add_argument("--data-dir", default=argparse.SUPPRESS)
    sub.add_argument("--since", type=int, default=7, metavar="DAYS")
    sub.add_argument("--show-text", action="store_true", help="write seeds only to data-dir/scratch")
    args = parser.parse_args(argv)
    if not 1 <= args.since <= 60:
        parser.error("--since must be between 1 and 60")
    root = (Path(args.data_dir) if args.data_dir else default_data_dir()).resolve()
    try:
        source = sqlite3.connect(f"{(root / 'mind.db').as_uri()}?mode=ro", uri=True)
        try:
            copy = sqlite3.connect(":memory:", autocommit=True)
            try:
                # A backup can contain sqlite-vec tables. Loading the extension
                # only enables local reads; it never creates/loads a model.
                import sqlite_vec
                copy.enable_load_extension(True)
                sqlite_vec.load(copy)
                copy.enable_load_extension(False)
                source.backup(copy)
                copy.autocommit = False
                report, details = replay(copy, now=time.time(), days=args.since, show_text=args.show_text)
            finally:
                copy.close()
        finally:
            source.close()
        if args.show_text:
            _write_text(root, details)
        print(json.dumps(report, sort_keys=True))
        return 0
    except (sqlite3.Error, ValueError, OSError) as exc:
        print(f"callbacks failed: {type(exc).__name__}", file=sys.stderr)
        return 1
