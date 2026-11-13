"""Inspect durable server receipts without connecting, migrating or taking a lock."""
from contextlib import closing
from dataclasses import asdict
from datetime import datetime
import json
import sqlite3

from leeroy_mind.console.feed import connect
from .clock import in_window
from .config import load_server_config
from .records import COUNTS, DAY_KEY


def status(root, *, local=None):
    local = local or datetime.now().astimezone()
    config = load_server_config(root / 'mind.toml')
    settings = asdict(config)
    settings['quiet_window'] = [t.strftime('%H:%M') for t in config.quiet_window]
    with closing(connect(root / 'mind.db')) as conn:
        # One read transaction keeps counters, recent endings and openers consistent.
        conn.execute('BEGIN')
        meta = dict(conn.execute("SELECT key,value FROM meta WHERE key LIKE 'server:%'"))
    counts = json.loads(meta.get(DAY_KEY.format(date=local.date().isoformat()), '{}'))
    if not isinstance(counts, dict) or any(type(counts.get(k, 0)) is not int or counts.get(k, 0) < 0 for k in COUNTS):
        raise ValueError('invalid_server_counts')
    recent = json.loads(meta.get('server:recent', '[]'))
    if not isinstance(recent, list):
        raise ValueError('invalid_server_recent')
    return dict(config=settings, window=dict(start=settings['quiet_window'][0],
        end=settings['quiet_window'][1], inside=in_window(local, config.quiet_window)),
        day=local.date().isoformat(), counts={k: counts.get(k, 0) for k in COUNTS},
        recent=[{k: row.get(k) for k in ('channel', 'seed', 'messages', 'reason')}
                for row in reversed(recent[-5:])],
        open_dms=[dict(guid=int(key.removeprefix('server:dm:open:')), opened=float(value))
                  for key, value in sorted(meta.items()) if key.startswith('server:dm:open:')])


def run(args):
    from leeroy_mind.__main__ import _root
    try:
        result = status(_root(args))
    except (OSError, ValueError, TypeError, AttributeError, sqlite3.Error) as exc:
        print(json.dumps(dict(error='server_status_unavailable', kind=type(exc).__name__)))
        return 1
    if args.json:
        print(json.dumps(result, sort_keys=True))
    else:
        print('Server: ' + ('enabled' if result['config']['enabled'] else 'disabled'))
        print('Config: ' + json.dumps(result['config'], sort_keys=True))
        w = result['window']
        print(f"Quiet window: {w['start']}-{w['end']} ({'inside' if w['inside'] else 'outside'})")
        print(result['day'] + ': ' + json.dumps(result['counts'], sort_keys=True))
        print('Recent conversations: ' + json.dumps(result['recent'], sort_keys=True))
        print('Open DM openers: ' + json.dumps(result['open_dms'], sort_keys=True))
    return 0
