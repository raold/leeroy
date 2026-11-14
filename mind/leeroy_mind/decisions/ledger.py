"""The decision ledger: what the guild settled, who argued what, who settled it, when it took
effect and what it changed. Per kind and subject at most one decision is pending (proposed or
settled) and one is in effect: a newer proposal supersedes the pending one, and the rule in force
stays in force until its successor takes effect, which supersedes it in the same transaction (an
overruled, expired or talk-only successor leaves it standing). Every write is its own transaction
on the mind's store; logs carry ids, kinds, subjects (ids) and codes only."""
from collections.abc import Iterable, Mapping
import json
import logging
import math
import re
import sqlite3

log = logging.getLogger(__name__)

KINDS = ('lineup', 'loot_rule', 'bank', 'strategy', 'rank')
STATUSES = ('proposed', 'settled', 'in_effect', 'talk_only', 'overruled', 'superseded', 'expired', 'failed')
LIVE = ('proposed', 'settled', 'in_effect')
# raid:<cal id>, loot, bank:<req>, strategy:<map:boss:diff>, rank:<guid>
SUBJECTS = {'lineup': re.compile(r'raid:[1-9][0-9]{0,18}\Z'),
            'loot_rule': re.compile(r'loot(?::[a-z0-9_-]{1,60})?\Z'),
            'bank': re.compile(r'bank:[A-Za-z0-9_:-]{1,70}\Z'),
            'strategy': re.compile(r'strategy:[A-Za-z0-9_:-]{1,70}\Z'),
            'rank': re.compile(r'rank:[1-9][0-9]{0,9}\Z')}
CODE = re.compile(r'[a-z0-9_]{1,40}\Z')
MAX_JSON = 4000


class DecisionError(ValueError):
    """A write the ledger refuses: its words are a fixed code, never data."""


def _json(value) -> str:
    try:
        text = json.dumps(value, separators=(',', ':'), sort_keys=True, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError):
        raise DecisionError('decision_value_invalid') from None
    if len(text) > MAX_JSON:
        raise DecisionError('decision_value_too_long')
    return text


def _time(now) -> float:
    if type(now) not in (int, float) or not math.isfinite(now):
        raise DecisionError('decision_time_invalid')
    return float(now)


def positions_json(positions: Iterable) -> list[dict]:
    """Positions as stored: [{"guid","side","why"}], each a member guid and two codes, one per
    member. Accepts Position records or plain dicts."""
    out, seen = [], set()
    for p in positions:
        if isinstance(p, Mapping):
            guid, side, why = p.get('guid'), p.get('side'), p.get('why')
        else:
            guid, side, why = getattr(p, 'guid', None), getattr(p, 'side', None), getattr(p, 'why', None)
        if (type(guid) is not int or not 0 < guid <= 0xFFFFFFFF or guid in seen
                or not isinstance(side, str) or not CODE.fullmatch(side)
                or not isinstance(why, str) or not CODE.fullmatch(why)):
            raise DecisionError('decision_positions_invalid')
        seen.add(guid)
        out.append({'guid': guid, 'side': side, 'why': why})
    return out


def _check_kind(kind, subject):
    if kind not in KINDS:
        raise DecisionError('decision_kind_invalid')
    if not isinstance(subject, str) or not SUBJECTS[kind].match(subject):
        raise DecisionError('decision_subject_invalid')


def pending(conn: sqlite3.Connection, kind: str, subject: str):
    """The proposed or settled decision on this kind and subject, still waiting to take effect."""
    return conn.execute("SELECT * FROM guild_decision WHERE kind=? AND subject=? "
                        "AND status IN ('proposed','settled')", (kind, subject)).fetchone()


def current(conn: sqlite3.Connection, kind: str, subject: str):
    """The decision in effect on this kind and subject: it stays in force while a successor waits."""
    return conn.execute("SELECT * FROM guild_decision WHERE kind=? AND subject=? AND status='in_effect'",
                        (kind, subject)).fetchone()


def live(conn: sqlite3.Connection, kind: str, subject: str):
    """The pending decision on this kind and subject, else the one in effect."""
    return pending(conn, kind, subject) or current(conn, kind, subject)


def get(conn: sqlite3.Connection, id: int):
    return conn.execute('SELECT * FROM guild_decision WHERE id=?', (id,)).fetchone()


def in_effect(conn: sqlite3.Connection, kind: str) -> list:
    return conn.execute("SELECT * FROM guild_decision WHERE kind=? AND status='in_effect' "
                        'ORDER BY effect DESC, id DESC', (kind,)).fetchall()


def history(conn: sqlite3.Connection, *, limit=100) -> list:
    if type(limit) is not int or not 1 <= limit <= 500:
        raise DecisionError('decision_limit_invalid')
    return conn.execute('SELECT * FROM guild_decision ORDER BY id DESC LIMIT ?', (limit,)).fetchall()


def _ref(value):
    if value is not None and (type(value) is not int or value < 1):
        raise DecisionError('decision_ref_invalid')
    return value


def propose(store, *, kind, subject, value: dict, positions: list, now, poll: int | None = None) -> int:
    """A new proposed decision, with the poll that will settle it when one does (so its audience is
    known from the start). A pending one on the same kind and subject is superseded in the same
    transaction; the decision in effect stays in force until this one takes effect."""
    _check_kind(kind, subject)
    if not isinstance(value, dict):
        raise DecisionError('decision_value_invalid')
    text, stored, ts = _json(value), _json(positions_json(positions)), _time(now)
    _ref(poll)
    with store.transaction():
        if poll is not None and store.conn.execute('SELECT 1 FROM guild_poll WHERE id=?', (poll,)).fetchone() is None:
            raise DecisionError('decision_ref_invalid')
        old = pending(store.conn, kind, subject)
        if old is not None:
            store.conn.execute("UPDATE guild_decision SET status='superseded', ended=? WHERE id=?", (ts, old['id']))
        ident = store.conn.execute(
            'INSERT INTO guild_decision(kind,subject,value,positions,status,proposed,poll) '
            "VALUES (?,?,?,?,'proposed',?,?)", (kind, subject, text, stored, ts, poll)).lastrowid
    log.info('decision_proposed id=%d kind=%s subject=%s replaces=%d poll=%d', ident, kind, subject,
             old['id'] if old is not None else 0, poll or 0)
    return ident


def _move(store, id, frm: tuple[str, ...], sql: str, params: tuple) -> sqlite3.Row:
    with store.transaction():
        row = get(store.conn, id)
        if row is None:
            raise DecisionError('decision_unknown')
        if row['status'] not in frm:
            raise DecisionError('decision_state')
        store.conn.execute(f'UPDATE guild_decision SET {sql} WHERE id=?', (*params, id))
    return row


def settle(store, id, *, settled_by: int, poll: int | None = None, message: int | None = None, now) -> None:
    """proposed -> settled, by a member (her guid) or by a poll (settled_by 0, with the poll)."""
    if type(settled_by) is not int or not 0 <= settled_by <= 0xFFFFFFFF:
        raise DecisionError('decision_settler_invalid')
    _ref(poll)
    _ref(message)
    row = _move(store, id, ('proposed',),
                "status='settled', settled=?, settled_by=?, poll=coalesce(?,poll), message=coalesce(?,message)",
                (_time(now), settled_by, poll, message))
    log.info('decision_settled id=%d kind=%s by=%d poll=%d', id, row['kind'], settled_by, poll or row['poll'] or 0)


def take_effect(store, id, *, effect_ref: dict, now) -> None:
    """settled -> in_effect, with what changed in game (direct-ledger refs, ledger ids, receipt
    keys). The decision it replaces (in effect on the same kind and subject) is superseded in the
    same transaction and named in `supersedes`, so an undo can restore it."""
    if not isinstance(effect_ref, dict):
        raise DecisionError('decision_effect_invalid')
    ts, ref = _time(now), _json(effect_ref)
    with store.transaction():
        row = get(store.conn, id)
        if row is None:
            raise DecisionError('decision_unknown')
        if row['status'] != 'settled':
            raise DecisionError('decision_state')
        old = current(store.conn, row['kind'], row['subject'])
        if old is not None:
            store.conn.execute("UPDATE guild_decision SET status='superseded', ended=? WHERE id=?", (ts, old['id']))
        store.conn.execute("UPDATE guild_decision SET status='in_effect', effect=?, effect_ref=?, supersedes=? "
                           'WHERE id=?', (ts, ref, old['id'] if old is not None else None, id))
    log.info('decision_in_effect id=%d kind=%s supersedes=%d', id, row['kind'], old['id'] if old is not None else 0)


def talk_only(store, id, *, reason: str, now) -> None:
    """A decision with nothing in game to carry it stays talk."""
    if not isinstance(reason, str) or not CODE.fullmatch(reason):
        raise DecisionError('decision_reason_invalid')
    row = _move(store, id, ('proposed', 'settled'), "status='talk_only', ended=?, effect_ref=?",
                (_time(now), _json({'talk_only': reason})))
    log.info('decision_talk_only id=%d kind=%s reason=%s', id, row['kind'], reason)


def expire(store, id, *, now) -> None:
    """A proposal that was never settled: the status quo stands."""
    row = _move(store, id, ('proposed', 'settled'), "status='expired', ended=?", (_time(now),))
    log.info('decision_expired id=%d kind=%s', id, row['kind'])


def fail(store, id, *, reason: str, now) -> None:
    """Its effect could not be carried out (or its outcome is unknown and never resent)."""
    if not isinstance(reason, str) or not CODE.fullmatch(reason):
        raise DecisionError('decision_reason_invalid')
    with store.transaction():
        row = get(store.conn, id)
        if row is None:
            raise DecisionError('decision_unknown')
        if row['status'] not in LIVE:
            raise DecisionError('decision_state')
        ref = json.loads(row['effect_ref']) if row['effect_ref'] else {}
        ref = {**(ref if isinstance(ref, dict) else {}), 'failed': reason}
        store.conn.execute("UPDATE guild_decision SET status='failed', ended=?, effect_ref=? WHERE id=?",
                           (_time(now), _json(ref), id))
    log.info('decision_failed id=%d kind=%s reason=%s', id, row['kind'], reason)


def restore(store, id, *, now) -> int | None:
    """Put back in effect the decision this one replaced (its `supersedes`), once this one no longer
    is: an undo that restores the previous rule. None when there is nothing to restore."""
    with store.transaction():
        row = get(store.conn, id)
        if row is None:
            raise DecisionError('decision_unknown')
        if row['status'] in LIVE or row['supersedes'] is None:
            return None
        old = get(store.conn, row['supersedes'])
        if (old is None or old['status'] != 'superseded' or old['effect'] is None
                or current(store.conn, old['kind'], old['subject']) is not None):
            return None
        store.conn.execute("UPDATE guild_decision SET status='in_effect', ended=NULL WHERE id=?", (old['id'],))
    log.info('decision_restored id=%d kind=%s after=%d', old['id'], old['kind'], id)
    return old['id']


def overrule(store, id, *, now, restore_previous: bool = False) -> int | None:
    """His overrule: any live decision stops here (overruled_by_owner=1, status overruled). A
    pending one leaves the decision in effect in force. Undoing what an in-effect one changed in
    game is its kind's own (apply.KindRules.undo), before this; with restore_previous the rule it
    replaced is put back in effect in the same transaction. Returns the restored decision's id."""
    with store.transaction():
        row = _move(store, id, LIVE, "status='overruled', overruled_by_owner=1, ended=?", (_time(now),))
        back = restore(store, id, now=now) if restore_previous and row['status'] == 'in_effect' else None
    log.info('decision_overruled id=%d kind=%s restored=%d', id, row['kind'], back or 0)
    return back
