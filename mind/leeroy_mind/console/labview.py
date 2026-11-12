"""Bounded, read-only Lab projections. Chat text is read only for an explicit line request."""
from dataclasses import asdict
from datetime import datetime
from contextlib import closing
import json
import sqlite3
import time

from . import feed, views
from .opsview import database, Missing
from leeroy_mind.learn import arms, bandit, control, lessons, weights
from leeroy_mind.learn.config import load_learn_config
from leeroy_mind.signals.store import REASONS
from leeroy_mind.discord.channels import is_discord, channel_id
from leeroy_mind.discord import layout

DAYS, LIMIT = 14, 300


def feedback(conn, ids):
    """Current votes for an already-visible, bounded page; no message text here."""
    ids = list(ids)
    if not ids or 'talk_signal' not in views.tables(conn):
        return {}
    encoded = json.dumps(ids)
    revisions = {int(k.removeprefix('learn:feedback:')): int(v) for k,v in conn.execute(
        "SELECT key,value FROM meta WHERE key IN (SELECT 'learn:feedback:'||value FROM json_each(?))",
        (encoded,))} if 'meta' in views.tables(conn) else {}
    out = {ident: {'value': None, 'reasons': [], 'revision': revision} for ident,revision in revisions.items()}
    kinds = {'reason_'+r.replace('-', '_'): r for r in REASONS}
    for lid, kind, value in conn.execute(
            "SELECT line_id,kind,value FROM talk_signal WHERE source='explicit' AND line_id IN "
            "(SELECT value FROM json_each(?))", (encoded,)):
        item = out.setdefault(lid, {'value': None, 'reasons': [], 'revision': 0})
        if kind == 'explicit':
            item['value'] = int(value)
        elif kind in kinds:
            item['reasons'].append(kinds[kind])
    return out


def discord_lines(conn, messages):
    """Never confuse a Discord message ID with a chat_line ID, including deleted messages."""
    out = {}
    if not {'meta', 'chat_line'} <= views.tables(conn):
        return out
    for m in messages:
        if m['origin'] != 'mind' or m['deleted'] is not None:
            continue
        peer = layout.dm_peer(m['channel'])
        if peer is not None and peer != m['author'] or peer is None and layout.channel(m['channel']) is None:
            continue
        channel = m['channel'] if peer is not None else 'discord:'+m['channel']
        row = conn.execute("SELECT c.id FROM meta x JOIN chat_line c ON c.id=CAST(x.value AS INTEGER) "
                           "WHERE x.key=? AND c.origin='mind' AND c.from_guid=? AND c.channel=?",
                           (f'discord:line:{m["id"]}', m['author'], channel)).fetchone()
        if row is None:
            # Before Lab the receipt existed only with preference capture enabled.
            # Require uniqueness in BOTH stores, never a closest/approximate text match.
            candidates = conn.execute("SELECT id FROM chat_line WHERE channel=? AND from_guid=? "
                "AND origin='mind' AND text=? AND ts BETWEEN ? AND ? LIMIT 2",
                (channel,m['author'],m['text'],m['ts']-1,m['ts']+1)).fetchall()
            peers = conn.execute("SELECT id FROM discord_message WHERE channel=? AND author=? "
                "AND origin='mind' AND text=? AND ts BETWEEN ? AND ? LIMIT 2",
                (m['channel'],m['author'],m['text'],m['ts']-2,m['ts']+2)).fetchall()
            if len(candidates)==len(peers)==1 and peers[0][0]==m['id']:
                row=candidates[0]
        if row:
            out[m['id']] = row[0]
    return out


def _rewards(conn, now):
    if not weights.available(conn):
        return {'available': False, 'days': [], 'members': []}
    start = now-DAYS*86400
    names = dict(conn.execute('SELECT guid,name FROM cast_member')) if 'cast_member' in views.tables(conn) else {}
    # Aggregate numbers only. The local date matches the owner's reward budget day.
    conn.create_function('lab_day', 1, lambda ts: datetime.fromtimestamp(ts).date().isoformat())
    daily = [dict(zip(('day', 'average', 'lines'), r)) for r in conn.execute(
        'SELECT lab_day(c.ts),avg(r.value),count(*) FROM talk_reward r JOIN chat_line c ON c.id=r.line_id '
        'WHERE c.ts>=? AND c.ts<=? GROUP BY 1 ORDER BY 1', (start, now))]
    members = {}
    for guid, avg, count in conn.execute('SELECT r.guid,avg(r.value),count(*) FROM talk_reward r '
            'JOIN chat_line c ON c.id=r.line_id WHERE c.ts>=? AND c.ts<=? GROUP BY r.guid ORDER BY r.guid LIMIT ?',
            (start, now, LIMIT)):
        members[guid] = {'guid': guid, 'name': names.get(guid, f'#{guid}'), 'average': avg,
                         'lines': count, 'up': 0, 'down': 0, 'top': [], 'bottom': []}
    for guid, up, down in conn.execute("SELECT s.guid,sum(s.value=1),sum(s.value=-1) FROM talk_signal s "
            "JOIN chat_line c ON c.id=s.line_id WHERE s.kind='explicit' AND s.source='explicit' "
            "AND c.ts>=? AND c.ts<=? GROUP BY s.guid ORDER BY s.guid LIMIT ?", (start, now, LIMIT)):
        item = members.setdefault(guid, {'guid': guid, 'name': names.get(guid, f'#{guid}'), 'average': None,
                                        'lines': 0, 'top': [], 'bottom': []})
        item.update(up=up, down=down)
    for key, order in (('top', 'DESC'), ('bottom', 'ASC')):
        for lid, guid, value, ts, channel in conn.execute(
                'WITH ranked AS (SELECT r.line_id,r.guid,r.value,c.ts,c.channel,row_number() OVER '
                f'(PARTITION BY r.guid ORDER BY r.value {order},c.id DESC) AS n FROM talk_reward r '
                'JOIN chat_line c ON c.id=r.line_id WHERE c.ts>=? AND c.ts<=?) '
                'SELECT line_id,guid,value,ts,channel FROM ranked WHERE n<=3 ORDER BY guid,n LIMIT ?',
                (start, now, LIMIT*3)):
            if guid in members:
                members[guid][key].append({'id': lid, 'value': value, 'ts': ts, 'channel': channel})
    return {'available': True, 'days': daily, 'members': list(members.values())[:LIMIT], 'period_days': DAYS}


def _arms(conn, now):
    if not {'learn_arm', 'bandit_credit', 'bandit_share', 'bandit_rejection'} <= views.tables(conn):
        return {'available': False, 'arms': [], 'contexts': []}
    registry = arms.read(conn)[:LIMIT]
    keys = conn.execute('SELECT context FROM bandit_share UNION SELECT context FROM bandit_credit '
                        'UNION SELECT context FROM bandit_rejection LIMIT 65').fetchall()
    contexts = []
    for (key,) in keys[:64]:
        if not bandit.valid_key(key):
            continue
        stats = bandit.posterior(conn, None, key, now=now)
        shares = dict(conn.execute('SELECT arm_id,turns FROM bandit_share WHERE context=?', (key,)))
        totals = {f: sum(shares.get(a.id, 0) for a in registry if a.family == f) for f in arms.FAMILIES}
        for arm in registry:
            p = stats.get(arm.id, {'alpha': 1., 'beta': 1., 'uses': 0, 'bad': 0, 'attempts': 0})
            contexts.append({'context': json.loads(key), 'arm_id': arm.id, **p,
                             'turns': shares.get(arm.id, 0),
                             'share': shares.get(arm.id, 0)/max(1, totals[arm.family])})
    return {'available': True, 'arms': [asdict(a) for a in registry], 'contexts': contexts,
            'limited': len(keys)>64 or len(registry)==LIMIT}


def _numeric(value, depth=0):
    if depth > 5:
        return None
    if type(value) in (int, float):
        import math
        return value if math.isfinite(value) else None
    if type(value) is dict and len(value)<=100:
        return {k: _numeric(v, depth+1) for k, v in value.items() if type(k) is str and len(k)<=80}
    if type(value) is list and len(value)<=100:
        return [_numeric(v, depth+1) for v in value]
    return None


def lab_view(conn, *, now, config=None, member=None, offset=0):
    tables = views.tables(conn)
    cfg = control.effective(conn, config) if config is not None else control.effective(conn)
    available = weights.available(conn)
    current = weights.read(conn, at=now)
    history = []
    if 'learn_weights' in tables:
        for version, created, value in conn.execute('SELECT version,created,weights FROM learn_weights ORDER BY version DESC LIMIT 100'):
            try:
                history.append({'version': version, 'created': created, 'values': weights.validate(json.loads(value))})
            except (ValueError, TypeError):
                continue
    notes, people, total = [], [], 0
    if lessons.available(conn):
        names = dict(conn.execute('SELECT guid,name FROM cast_member')) if 'cast_member' in tables else {}
        people = [{'guid':r[0], 'name':names.get(r[0],f'#{r[0]}'), 'count':r[1]} for r in conn.execute(
            'SELECT guid,count(*) FROM talk_lesson WHERE expires>? GROUP BY guid ORDER BY guid', (now,))]
        member = member if member is not None else people[0]['guid'] if people else None
        total = next((p['count'] for p in people if p['guid']==member),0)
        notes = [dict(zip(('id','guid','scope','text','status','reinforced','expires','origin_night'), r))
                 for r in conn.execute('SELECT * FROM talk_lesson WHERE expires>? AND guid=? ORDER BY id LIMIT 50 OFFSET ?',
                                       (now, member, offset))]
        for note in notes:
            note['name'] = names.get(note['guid'], f'#{note["guid"]}')
    report = None
    if 'meta' in tables:
        row = conn.execute('SELECT value FROM meta WHERE key=?', (control.REPORT_KEY,)).fetchone()
        if row and len(row[0])<=65536:
            try:
                parsed = json.loads(row[0])
                report = _numeric(parsed) if type(parsed) is dict else None
            except (ValueError, RecursionError):
                pass
    return {'at': now, 'freeze': cfg.freeze, 'families': list(arms.FAMILIES),
            'frozen_families': list(cfg.frozen_families), 'enabled': cfg.enabled,
            'rewards': _rewards(conn, now), 'arms': _arms(conn, now),
            'lessons': {'available': lessons.available(conn), 'items': notes, 'members':people,
                        'member':member, 'offset':offset, 'total':total, 'more':offset+len(notes)<total},
            'weights': {'available': available, 'current': asdict(current), 'history': history}, 'report': report}


def line_view(conn, ident, *, now):
    row = conn.execute("SELECT id,channel,text,from_name,from_guid FROM chat_line WHERE id=? AND origin='mind'",
                       (ident,)).fetchone() if 'chat_line' in views.tables(conn) else None
    if row is None or row['from_guid'] == feed.human_guid(conn):
        return {'line': None}
    if is_discord(row['channel']):
        if 'discord_message' not in views.tables(conn):
            return {'line': None}
        candidates = [dict(r) for r in conn.execute("SELECT id,channel,author,origin,text,ts,deleted FROM discord_message "
            "WHERE channel=? AND author=? AND origin='mind' AND deleted IS NULL AND "
            "(ts BETWEEN (SELECT ts-1 FROM chat_line WHERE id=?) AND (SELECT ts+1 FROM chat_line WHERE id=?) "
            "OR id IN (SELECT CAST(substr(key,14) AS INTEGER) FROM meta WHERE key GLOB 'discord:line:*' AND value=?)) LIMIT 3",
            (channel_id(row['channel']),row['from_guid'],ident,ident,str(ident)))]
        mapping=discord_lines(conn,candidates)
        shown=next((m for m in candidates if mapping.get(m['id'])==ident),None)
        if shown is None:
            return {'line': None}
        words = shown['text']
    else:
        visible = feed.look(conn, [ident], now=now)
        if not visible:
            return {'line': None}
        words = visible[0]['text']
    return {'line': {'id': ident, 'name': row['from_name'], 'text': words, 'channel': row['channel']}}


def read(data_dir, *, now, line=None, member=None, offset=0):
    """No database creation/migrations; query budget also covers an older schema."""
    try:
        with database(data_dir, time.monotonic()+1.0) as conn:
            conn.execute('BEGIN')
            return (lab_view(conn, now=now, config=load_learn_config(data_dir/'mind.toml'), member=member, offset=offset)
                    if line is None else line_view(conn, line, now=now))
    except Missing:
        # Empty in-memory DB preserves exactly the older-schema shape without a disk write.
        with closing(sqlite3.connect(':memory:')) as conn:
            return lab_view(conn, now=now) if line is None else {'line': None}
