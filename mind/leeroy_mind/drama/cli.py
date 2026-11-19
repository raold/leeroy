"""`python -m leeroy_mind drama threads report|replay`: what the romantic-threads recorder
stored, and the same recorder replayed over stored nights on a scratch copy. `report` opens
mind.db read-only; `replay` never writes the live database. No service, no model."""
import argparse
import json
import sqlite3
import sys
from datetime import date
from pathlib import Path

from leeroy_mind.config import default_data_dir

from . import report as reporting


def _night(text: str) -> str:
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        raise argparse.ArgumentTypeError("a date as YYYY-MM-DD") from None


def _nights(text: str) -> int:
    value = int(text)
    if not 1 <= value <= reporting.NIGHTS_MAX:
        raise argparse.ArgumentTypeError(f"nights must be 1-{reporting.NIGHTS_MAX}")
    return value


def drama_main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="leeroy_mind drama",
                                     description="Member drama: the romantic-threads recorder.")
    parser.add_argument("--data-dir")
    group = parser.add_subparsers(dest="group", required=True)
    threads = group.add_parser("threads", help="what would pull members together (recording only)")
    sub = threads.add_subparsers(dest="action", required=True)
    p = sub.add_parser("report", help="the recorded nights: events, shadow pulls, would-be threads")
    p.add_argument("--nights", type=_nights, default=14)
    p.add_argument("--json", action="store_true")
    p.add_argument("--data-dir", default=argparse.SUPPRESS)
    p = sub.add_parser("replay", help="run the recorder over stored nights on a scratch copy")
    p.add_argument("--from", dest="since", type=_night, required=True, metavar="YYYY-MM-DD")
    p.add_argument("--keep", action="store_true", help="keep the scratch copy (it is private data)")
    p.add_argument("--json", action="store_true")
    p.add_argument("--data-dir", default=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    root = Path(args.data_dir).resolve() if args.data_dir else default_data_dir()
    db = root / "mind.db"
    if not db.is_file():
        print(f"drama: no mind.db in {root}", file=sys.stderr)
        return 2
    try:
        if args.action == "replay":
            from .replay import replay
            data = replay(root, args.since, keep=args.keep)
            if args.json:
                print(json.dumps(data, sort_keys=True))
            else:
                print(f"Replayed {len(data['replayed'])} stored nights from {args.since} on a "
                      f"scratch copy ({data['limits']}). The live database was not written.")
                if data["copy"]:
                    print(f"Copy kept: {data['copy']}")
                print(reporting.render(data))
            return 0
        conn = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True, timeout=5.0)
        try:
            data = reporting.threads_report(conn, nights=args.nights)
        finally:
            conn.close()
        print(json.dumps(data, sort_keys=True) if args.json else reporting.render(data))
        return 0
    except (sqlite3.Error, ValueError, OSError) as exc:
        print(f"drama threads {args.action} failed: {type(exc).__name__}", file=sys.stderr)
        return 1
