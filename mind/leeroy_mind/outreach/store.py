"""Durable intentions and receipts. No game clocks or activity are written here."""
import logging
import math
from dataclasses import dataclass

from leeroy_mind.discord.channels import DM_PREFIX, is_private
from leeroy_mind.personas.tiers import is_cast
from leeroy_mind.storage.store import clean

log = logging.getLogger(__name__)
KINDS = frozenset({'life', 'plan', 'followup', 'his_day', 'birthday', 'miss_you', 'dm_nudge'})
VIA = frozenset({'whisper', 'dm', 'guild', 'party'})
ENDINGS = frozenset({'expired', 'superseded', 'capped', 'he_raised'})
MAX_OPEN = 5


@dataclass(frozen=True, slots=True)
class Item:
    id: int
    guid: int
    kind: str
    ref: str
    about: str
    audience: str
    weight: float
    created: float
    not_before: float
    expires: float
    state: str
    told_via: str | None
    told_at: float | None
    line_id: int | None
    ended: str | None


def _item(row) -> Item | None:
    return None if row is None else Item(**dict(row))


def _number(value) -> bool:
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def _human(store) -> int | None:
    value = store.get_meta('human_guid')
    try:
        guid = int(value)
    except (TypeError, ValueError):
        return None
    return guid if guid > 0 else None


def _member(store, guid: int) -> bool:
    human = _human(store)
    return (type(guid) is int and 0 < guid < 2**63 and human is not None and guid != human
            and is_cast(store.conn, guid)
            and store._one('SELECT 1 FROM cast_member c JOIN persona_core p ON p.guid=c.guid '
                           'WHERE c.guid=?', (guid,)) is not None)


def get(store, id: int) -> Item | None:
    return _item(store._one('SELECT * FROM outreach_item WHERE id=?', (id,)))


def open_items(store, guid: int | None = None) -> list[Item]:
    where, params = ('', ()) if guid is None else (' AND guid=?', (guid,))
    return [_item(row) for row in store._all(
        "SELECT * FROM outreach_item WHERE state='open'" + where + ' ORDER BY guid,created,id', params)]


def add(store, *, guid: int, kind: str, ref: str, about: str, audience: str,
        weight: float, created: float, not_before: float, expires: float) -> Item | None:
    """Insert once per source, including after it ended. A new low-weight item may be capped."""
    if (not isinstance(kind, str) or kind not in KINDS or audience not in ('pair', 'guild')
            or (kind == 'dm_nudge' and audience != 'pair')
            or not isinstance(ref, str) or not ref.strip()
            or not isinstance(about, str) or not about.strip()
            or not all(_number(v) for v in (weight, created, not_before, expires))
            or not 0.1 <= weight <= 10 or expires <= not_before):
        return None
    with store.transaction():
        if not _member(store, guid):
            return None
        row = store.conn.execute(
            'INSERT INTO outreach_item(guid,kind,ref,about,audience,weight,created,not_before,expires) '
            'VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(guid,ref) DO NOTHING RETURNING id',
            (guid, kind, clean(ref), clean(about.strip())[:200], audience,
             weight, created, not_before, expires)).fetchone()
        if row is None:
            return None
        id = row['id']
        log.info('outreach_added guid=%s kind=%s', guid, kind)
        # Keep older items at the same weight, so refill cannot repeatedly displace them.
        excess = store._all(
            "SELECT id FROM outreach_item WHERE guid=? AND state='open' "
            'ORDER BY weight DESC,created,id LIMIT -1 OFFSET ?', (guid, MAX_OPEN))
        for victim in excess:
            drop(store, victim['id'], 'capped')
        return get(store, id)


def _allowed(item: Item, via: str) -> bool:
    return (via in VIA and (item.audience == 'guild' or via in ('whisper', 'dm'))
            and (item.kind != 'dm_nudge' or via == 'whisper'))


def best(store, guid: int, *, via: str, now: float) -> Item | None:
    if via not in VIA or not _number(now) or not _member(store, guid):
        return None
    rows = store._all(
        "SELECT * FROM outreach_item WHERE guid=? AND state='open' AND not_before<=? AND expires>? "
        'ORDER BY weight DESC,created,id', (guid, now, now))
    return next((item for row in rows if _allowed(item := _item(row), via)), None)


def drop(store, id: int, why: str) -> bool:
    if why not in ENDINGS:
        raise ValueError('outreach_ending')
    with store.transaction():
        row = store.conn.execute(
            "UPDATE outreach_item SET state='dropped',ended=? WHERE id=? AND state='open' "
            'RETURNING guid,kind', (why, id)).fetchone()
        if row is not None:
            log.info('outreach_ended guid=%s kind=%s why=%s', row['guid'], row['kind'], why)
        return row is not None


def expire(store, now: float) -> int:
    if not _number(now):
        raise ValueError('outreach_time')
    with store.transaction():
        rows = store._all("SELECT id FROM outreach_item WHERE state='open' AND expires<=?", (now,))
        return sum(drop(store, row['id'], 'expired') for row in rows)


def told(store, id: int, *, via: str, line_id: int, now: float) -> bool:
    """Join the caller's delivered-line transaction. The caller logs only after its commit.

    A repeat of the same receipt is harmless; a different line can never retell an item.
    Late transport acknowledgements do not reopen intentions that already ended.
    """
    if not store.in_unit:
        raise RuntimeError('outreach_receipt_requires_transaction')
    item = get(store, id)
    if item is None or not _allowed(item, via):
        return False
    if item.state == 'told':
        return item.told_via == via and item.line_id == line_id
    if (item.state != 'open' or not _number(now) or not item.not_before <= now < item.expires
            or not _member(store, item.guid)):
        return False
    line = store._one('SELECT * FROM chat_line WHERE id=?', (line_id,))
    expected = DM_PREFIX + str(item.guid) if via == 'dm' else via
    # Public openers address him in Turn.addressee; their recorded to_guid remains zero.
    recipients = (_human(store),) if via in ('whisper', 'dm') else (0, _human(store))
    if (line is None or line['origin'] != 'mind' or line['from_guid'] != item.guid
            or line['to_guid'] not in recipients or line['channel'] != expected
            or (item.audience == 'pair' and not is_private(line['channel']))
            or store._one('SELECT 1 FROM outreach_item WHERE line_id=?', (line_id,)) is not None):
        return False
    store.conn.execute(
        "UPDATE outreach_item SET state='told',ended='told',told_via=?,told_at=?,line_id=? WHERE id=?",
        (via, now, line_id, id))
    return True
