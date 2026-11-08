"""Terminal-only inspection; mode=ro, no migration, model client or logging setup."""
import argparse
from pathlib import Path
import sqlite3
import sys

from leeroy_mind.config import default_data_dir
from .card import CardContext, card


def awareness_main(argv=None):
    parser = argparse.ArgumentParser(prog="leeroy_mind awareness")
    parser.add_argument("--data-dir")
    sub = parser.add_subparsers(dest="action", required=True).add_parser("card")
    sub.add_argument("guid", type=int)
    sub.add_argument("--data-dir", default=argparse.SUPPRESS)
    sub.add_argument("--budget-tokens", type=int, default=600)
    args = parser.parse_args(argv)
    root = Path(args.data_dir).resolve() if args.data_dir else default_data_dir()
    try:
        conn = sqlite3.connect(f"{(root / 'mind.db').resolve().as_uri()}?mode=ro", uri=True,
                               autocommit=False)
        try:
            print(card(conn, args.guid, context=CardContext()).render(args.budget_tokens))
        finally:
            conn.close()
        return 0
    except (sqlite3.Error, ValueError, OSError) as exc:
        print(f"awareness failed: {type(exc).__name__}", file=sys.stderr)
        return 1
