"""`python -m leeroy_mind turing report|run|texture`: read-only, on mind.db opened mode=ro. No
service, model, migration or write; an older database reads as not set up."""
import argparse
import json
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

from leeroy_mind.config import default_data_dir

from . import report as reporting
from . import stylo
from .config import DAYS_MAX


def _days(text: str) -> int:
    value = int(text)
    if not 1 <= value <= DAYS_MAX:
        raise argparse.ArgumentTypeError(f"days must be 1-{DAYS_MAX}")
    return value


def _his_names(conn, root: Path) -> tuple[str, ...]:
    """His names for spotting a line that names him: meta human_name, and [human] from
    mind.toml when it reads. Used for counting only; never printed."""
    names = []
    row = conn.execute("SELECT value FROM meta WHERE key='human_name'").fetchone()
    if row and row[0]:
        names.append(row[0])
    try:
        from leeroy_mind.config import load_config
        cfg = load_config(root / "mind.toml")
        names += [cfg.human.nickname, cfg.human.real_name]
    except Exception:
        pass
    return tuple(dict.fromkeys(n for n in names if n and n.strip()))


def _human(conn) -> int:
    row = conn.execute("SELECT value FROM meta WHERE key='human_guid'").fetchone()
    if row is None or not str(row[0]).isdigit() or int(row[0]) <= 0:
        raise ValueError("no human_guid in mind.db")
    return int(row[0])


def _date(text: str) -> float:
    try:
        return datetime.strptime(text, "%Y-%m-%d").timestamp()
    except ValueError:
        raise argparse.ArgumentTypeError("a date as YYYY-MM-DD") from None


def turing_main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="leeroy_mind turing",
                                     description="How distinguishable the members are from you.")
    parser.add_argument("--data-dir")
    sub = parser.add_subparsers(dest="action", required=True)
    for name, text in (("report", "the stored nights: AUC trend, the judge and the top tells"),
                       ("run", "the stylometric part now (no model, nothing written)")):
        p = sub.add_parser(name, help=text)
        p.add_argument("--days", type=_days, default=14)
        p.add_argument("--json", action="store_true")
        p.add_argument("--data-dir", default=argparse.SUPPRESS)
    p = sub.add_parser("texture", help="the typing texture replayed on stored lines (no model, "
                                       "nothing written)")
    p.add_argument("--days", type=_days, default=DAYS_MAX)
    p.add_argument("--until", type=_date, help="only lines before this local date (YYYY-MM-DD): "
                   "the day the texture went on, since a line typed with it would get it twice")
    p.add_argument("--json", action="store_true")
    p.add_argument("--data-dir", default=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    root = Path(args.data_dir).resolve() if args.data_dir else default_data_dir()
    db = root / "mind.db"
    if not db.is_file():
        print(f"turing: no mind.db in {root}", file=sys.stderr)
        return 2
    try:
        conn = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True, timeout=5.0)
        try:
            if args.action == "report":
                data = reporting.report(conn, now=time.time(), days=args.days)
                print(json.dumps(data, sort_keys=True) if args.json else reporting.render_report(data))
                return 0
            if args.action == "texture":
                from . import texture
                until = min(args.until or time.time(), time.time())
                data = texture.replay(conn, human=_human(conn), start=until - args.days * 86400,
                                      until=until, his_names=_his_names(conn, root))
                print(json.dumps(data, sort_keys=True) if args.json else texture.render(data))
                return 0
            data = stylo.compute(conn, human=_human(conn), end=time.time(), days=args.days,
                                 his_names=_his_names(conn, root))
            print(json.dumps(data, sort_keys=True) if args.json else reporting.render_run(data))
            return 0
        finally:
            conn.close()
    except (sqlite3.Error, ValueError, OSError) as exc:
        print(f"turing {args.action} failed: {type(exc).__name__}", file=sys.stderr)
        return 1
