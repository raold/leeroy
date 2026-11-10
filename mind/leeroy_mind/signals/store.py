"""Numeric observations; caller owns the transaction. No migration or text logging."""
import math

REPLY_SECONDS = 120.0
REPLY_LEN_SCALE = 500
RETENTION_S = 120 * 86400
REASONS = ('funny', 'in-character', 'boring', 'repetitive', 'weird', 'too-much', 'wrong-facts')
IMPLICIT = ('replied', 'reply_latency_s', 'reply_len', 'thread_continued', 'ignored',
            'emote_friendly', 'emote_romantic', 'emote_rude', 'muted', 'dial_down', 'nudged_against')
JUDGES = ('judge_repetition', 'judge_nonsequitur', 'judge_unawareness', 'judge_contradiction',
          'judge_guard_rewrite', 'judge_guard_drop', 'judge_guard_react')
KINDS = (*IMPLICIT, 'explicit', *('reason_' + r.replace('-', '_') for r in REASONS), *JUDGES)


def available(conn):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='talk_signal'").fetchone() is not None


def put(conn, line_id, guid, kind, value, ts, source='implicit', *, act_id=None):
    if (kind not in KINDS or source not in ('implicit', 'explicit', 'judge')
            or type(guid) is not int or guid <= 0
            or type(value) not in (int, float) or not math.isfinite(value) or not -1 <= value <= 1
            or type(ts) not in (int, float) or not math.isfinite(ts) or ts < 0
            or (line_id is None) == (act_id is None)
            or type(line_id if line_id is not None else act_id) is not int
            or (line_id if line_id is not None else act_id) <= 0):
        raise ValueError('invalid_signal')
    return conn.execute(
        'INSERT OR IGNORE INTO talk_signal(line_id,act_id,guid,kind,value,ts,source) '
        'VALUES (?,?,?,?,?,?,?)', (line_id, act_id, guid, kind, value, ts, source)).rowcount


def prune(conn, now):
    return conn.execute('DELETE FROM talk_signal WHERE ts<?', (now - RETENTION_S,)).rowcount


def feedback_args(args):
    if (type(args) is not dict or set(args) != {'line_id', 'value', 'reasons'}
            or type(args['line_id']) is not int or not 0 < args['line_id'] < 2**63
            or type(args['value']) is not int or args['value'] not in (-1, 1)
            or type(args['reasons']) is not list or len(args['reasons']) > len(REASONS)
            or any(type(r) is not str or r not in REASONS for r in args['reasons'])
            or len(set(args['reasons'])) != len(args['reasons'])):
        raise ValueError('Feedback needs a line id, +1 or -1, and listed unique reasons.')
    return dict(args)


def feedback(conn, args, owner, now):
    args = feedback_args(args)
    row = conn.execute('SELECT from_guid,origin FROM chat_line WHERE id=?', (args['line_id'],)).fetchone()
    if row is None or row[1] != 'mind' or row[0] == owner:
        return {'ok': False, 'error': 'Choose a stored member line.'}
    # One current vote per line. Changing a vote also replaces its reason chips.
    conn.execute("DELETE FROM talk_signal WHERE line_id=? AND source='explicit'", (args['line_id'],))
    put(conn, args['line_id'], row[0], 'explicit', args['value'], now, 'explicit')
    for reason in args['reasons']:
        put(conn, args['line_id'], row[0], 'reason_' + reason.replace('-', '_'), 1, now, 'explicit')
    return {'ok': True, 'line_id': args['line_id'], 'recorded': 1 + len(args['reasons'])}
