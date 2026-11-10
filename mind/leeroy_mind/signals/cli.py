"""Read-only counts; never migrate, open a model client or print source words."""
import argparse
from pathlib import Path
import sqlite3
import sys

from leeroy_mind.config import default_data_dir
from .store import available


def member_guid(value):
    try:
        guid = int(value)
        if 0 < guid <= 0xFFFFFFFF:
            return guid
    except ValueError:
        pass
    raise argparse.ArgumentTypeError('member guid must be an integer from 1 to 4294967295')


def signals_main(argv=None):
    parser = argparse.ArgumentParser(prog='leeroy_mind signals')
    parser.add_argument('--data-dir')
    sub = parser.add_subparsers(dest='action', required=True)
    for action in ('status', 'member'):
        p = sub.add_parser(action)
        p.add_argument('--data-dir', default=argparse.SUPPRESS)
        if action == 'member':
            p.add_argument('guid', type=member_guid)
    args = parser.parse_args(argv)
    root = Path(args.data_dir).resolve() if args.data_dir else default_data_dir()
    try:
        conn = sqlite3.connect(f'{(root / "mind.db").resolve().as_uri()}?mode=ro', uri=True)
        try:
            if not available(conn):
                print('Signals: not set up in this database.')
                return 0
            where, values = (' WHERE guid=?', (args.guid,)) if args.action == 'member' else ('', ())
            rows = conn.execute("SELECT date(ts,'unixepoch'),kind,source,count(*) FROM talk_signal" +
                where + ' GROUP BY 1,2,3 ORDER BY 1,2,3', values)
            print('day_utc kind source count')
            for row in rows:
                print(*row)
            return 0
        finally:
            conn.close()
    except (sqlite3.Error, ValueError, OSError) as exc:
        print(f'signals failed: {type(exc).__name__}', file=sys.stderr)
        return 1
