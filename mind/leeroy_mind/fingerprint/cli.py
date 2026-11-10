"""Read-only inspection without constructing a service, model or logger."""
import argparse
import json
import sqlite3
import sys
from dataclasses import asdict
from pathlib import Path

from leeroy_mind.config import default_data_dir
from . import fingerprint, render_style_hint


def fingerprint_main(argv=None):
    parser = argparse.ArgumentParser(prog='leeroy_mind fingerprint')
    parser.add_argument('--data-dir')
    show = parser.add_subparsers(dest='action', required=True).add_parser('show')
    show.add_argument('guid', type=int)
    show.add_argument('--data-dir', default=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    root = Path(args.data_dir).resolve() if args.data_dir else default_data_dir()
    try:
        conn = sqlite3.connect(f"{(root/'mind.db').resolve().as_uri()}?mode=ro", uri=True)
        try:
            profile = fingerprint(conn,args.guid)
        finally:
            conn.close()
        print(json.dumps({'guid': args.guid, 'profile': asdict(profile),
                          'hint': render_style_hint(profile)}, sort_keys=True))
        return 0
    except (sqlite3.Error, ValueError, OSError) as exc:
        print(f'fingerprint failed: {type(exc).__name__}', file=sys.stderr)
        return 1
