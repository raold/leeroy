"""The decision ledger: what the guild settled, who argued what, who settled it, when it took
effect and what it changed. One live decision per kind and subject; a newer one supersedes the
old in the same transaction. Every write is its own transaction on the mind's store; logs carry
ids, kinds, subjects (ids) and codes only."""
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


def live(conn: sqlite3.Connection, kind: str, subject: str):
    return conn.execute("SELECT * FROM guild_decision WHERE kind=? AND subject=? "
                        "AND status IN ('proposed','settled','in_effect')", (kind, subject)).fetchone()


def get(conn: sqlite3.Connection, id: int):
    return conn.execute('SELECT * FROM guild_decision WHERE id=?', (id,)).fetchone()


def in_effect(conn: sqlite3.Connection, kind: str) -> list:
    return conn.execute("SELECT * FROM guild_decision WHERE kind=? AND status='in_effect' "
                        'ORDER BY effect DESC, id DESC', (kind,)).fetchall()


def history(conn: sqlite3.Connection, *, limit=100) -> list:
    if type(limit) is not int or not 1 <= limit <= 500:
        raise DecisionError('decision_limit_invalid')
    return conn.execute('SELECT * FROM guild_decision ORDER BY id DESC LIMIT ?', (limit,)).fetchall()


def propose(store, *, kind, subject, value: dict, positions: list, now) -> int:
    """A new proposed decision. A live one on the same kind and subject is superseded in the same
    transaction, and the new row names it."""
    _check_kind(kind, subject)
    if not isinstance(value, dict):
        raise DecisionError('decision_value_invalid')
    text, stored, ts = _json(value), _json(positions_json(positions)), _time(now)
    with store.transaction():
        old = live(store.conn, kind, subject)
        if old is not None:
            store.conn.execute("UPDATE guild_decision SET status='superseded', ended=? WHERE id=?", (ts, old['id']))
        ident = store.conn.execute(
            'INSERT INTO guild_decision(kind,subject,value,positions,status,proposed,supersedes) '
            "VALUES (?,?,?,?,'proposed',?,?)",
            (kind, subject, text, stored, ts, old['id'] if old is not None else None)).lastrowid
    log.info('decision_proposed id=%d kind=%s subject=%s supersedes=%s', ident, kind, subject,
             old['id'] if old is not None else 0)
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
    for ref in (poll, message):
        if ref is not None and (type(ref) is not int or ref < 1):
            raise DecisionError('decision_ref_invalid')
    row = _move(store, id, ('proposed',),
                "status='settled', settled=?, settled_by=?, poll=coalesce(?,poll), message=coalesce(?,message)",
                (_time(now), settled_by, poll, message))
    log.info('decision_settled id=%d kind=%s by=%d poll=%d', id, row['kind'], settled_by, poll or 0)


def take_effect(store, id, *, effect_ref: dict, now) -> None:
    """settled -> in_effect, with what changed in game (direct-ledger refs, ledger ids, receipt keys)."""
    if not isinstance(effect_ref, dict):
        raise DecisionError('decision_effect_invalid')
    row = _move(store, id, ('settled',), "status='in_effect', effect=?, effect_ref=?",
                (_time(now), _json(effect_ref)))
    log.info('decision_in_effect id=%d kind=%s', id, row['kind'])


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


def overrule(store, id, *, now) -> None:
    """His overrule: any live decision stops here (overruled_by_owner=1, status overruled). Undoing
    what it already changed in game is the kind's own (apply.KindRules.undo), before this."""
    row = _move(store, id, LIVE, "status='overruled', overruled_by_owner=1, ended=?", (_time(now),))
    log.info('decision_overruled id=%d kind=%s', id, row['kind'])
