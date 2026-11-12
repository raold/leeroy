"""Small server receipts; callers join them to the transaction that stored a post."""
from datetime import date, timedelta
import json

DAY_KEY = 'server:day:{date}'
COUNTS = ('conversations', 'messages', 'reactions', 'dms')


def counts(store, local_date):
    raw = store.get_meta(DAY_KEY.format(date=local_date))
    value = json.loads(raw) if raw is not None else {}
    if not isinstance(value, dict) or any(type(value.get(k, 0)) is not int or value.get(k, 0) < 0 for k in COUNTS):
        raise ValueError('invalid_server_counts')
    return {k: value.get(k, 0) for k in COUNTS}


def increment(store, local_date, **changes):
    if not store.in_unit:
        raise RuntimeError('server_receipt_without_transaction')
    value = counts(store, local_date)
    for key, amount in changes.items():
        if key not in COUNTS or type(amount) is not int or amount < 0:
            raise ValueError('invalid_server_count')
        value[key] += amount
    store.set_meta(DAY_KEY.format(date=local_date), json.dumps(value))


def posted(store, local_date, now, *, conversation='', dm_guid=None):
    if not store.in_unit:
        raise RuntimeError('server_receipt_without_transaction')
    changes = {'messages': 1}
    if conversation:
        key = 'server:conversation:' + conversation
        if store.get_meta(key) is None:
            store.set_meta(key, str(now))
            changes['conversations'] = 1
    if dm_guid is not None:
        changes['dms'] = 1
        store.set_meta(f'server:dm:open:{dm_guid}', str(now))
        store.set_meta(f'server:dm:day:{local_date}:{dm_guid}', str(now))
    increment(store, local_date, **changes)


def ended(store, value):
    try:
        recent = json.loads(store.get_meta('server:recent') or '[]')
    except (ValueError, TypeError):
        recent = []
    if not isinstance(recent, list):
        recent = []
    with store.transaction():
        store.set_meta('server:recent', json.dumps([*recent[-19:], value]))


def prune(store, local_date, now):
    """The daily sweep owns dated counters and timestamped seed/DM receipts only."""
    cutoff = (date.fromisoformat(local_date) - timedelta(days=7)).isoformat()
    stale = []
    for key, value in store.conn.execute("SELECT key,value FROM meta WHERE key LIKE 'server:%'"):
        if key.startswith(('server:day:', 'server:dm:day:')):
            stamp = key[len('server:dm:day:'):] if key.startswith('server:dm:day:') else key[len('server:day:'):]
            try:
                old = date.fromisoformat(stamp[:10]).isoformat() < cutoff
            except ValueError:
                old = False
        elif key.startswith(('server:seed:', 'server:conversation:', 'server:dm:open:')):
            try:
                old = float(value) < now - 7 * 86400
            except (ValueError, TypeError):
                old = False
        else:
            old = False
        if old:
            stale.append((key,))
    with store.transaction():
        store.conn.executemany('DELETE FROM meta WHERE key=?', stale)
