"""Durable owner photo requests, delivery receipts and their independent borrow allowance.

All writes nest in Store.transaction(). Delivery callers keep that transaction open around
the Discord message, chat line, snap receipt and mark_delivery, so none can commit alone.
No message words, files, model calls or background tasks belong here.
"""
from dataclasses import dataclass, fields
import re

from leeroy_mind.selfies import snaps

MEMBER_GAP = 120.0
LIFETIME = 86400.0
HOUR = 3600.0
HOUR_BORROWS = 6
DAY_BORROWS = 30
REPEAT_WINDOW = 30 * LIFETIME
RETRIES = 2
PHASES = ('morning', 'day', 'evening', 'late', 'dead')
_DELIVERIES = {kind: kind + '_message' for kind in
               ('initial', 'fresh', 'fallback', 'followup', 'failure')}


class RequestError(ValueError):
    """An inconsistent request mutation; callers roll back its enclosing delivery."""


@dataclass(frozen=True)
class Request:
    message: int
    guid: int
    asked_channel: str
    thread: int | None
    photo_channel: str
    audience: str
    created: float
    expires: float
    fresh_snap: int | None
    fallback_snap: int | None
    initial_message: int | None
    fresh_message: int | None
    fallback_message: int | None
    followup_message: int | None
    failure_message: int | None
    failure: str | None
    ended: str | None


_COLS = ','.join(f.name for f in fields(Request))


def get(store, message: int) -> Request | None:
    with store.transaction():
        row = store.conn.execute(f'SELECT {_COLS} FROM selfie_request WHERE message=?',
                                 (message,)).fetchone()
        return None if row is None else Request(*tuple(row))


def snap(store, snap_id: int | None) -> snaps.SnapRow | None:
    if snap_id is None:
        return None
    rows = snaps._read(store, f'SELECT {snaps._COLS} FROM selfie_snap WHERE id=?', (snap_id,))
    return rows[0] if rows else None


def claim(store, *, message: int, guid: int, asked_channel: str, thread: int | None,
          photo_channel: str, audience: str, now: float) -> tuple[Request | None, str]:
    """Claim the original post once; accepted/existing/cooldown. No cooldown for duplicates."""
    from leeroy_mind.discord import layout
    with store.transaction():
        if old := get(store, message):
            return old, 'existing'
        row = store.conn.execute('SELECT channel,thread,origin,author,deleted FROM discord_message '
                                 'WHERE id=?', (message,)).fetchone()
        owner = store.get_meta('human_guid')
        if (row is None or row['deleted'] is not None or row['origin'] != 'human'
                or (owner is not None and str(row['author']) != owner)
                or row['channel'] != asked_channel or row['thread'] != thread
                or type(guid) is not int or guid <= 0):
            raise RequestError('invalid request post')
        peer = layout.dm_peer(asked_channel)
        if ((peer is not None and peer != guid)
                or (peer is None and layout.channel(asked_channel) is None)
                or audience not in ('him', 'anyone')
                or (audience == 'him' and photo_channel != f'dm:{guid}')
                or (audience == 'anyone' and (photo_channel != asked_channel
                                            or layout.channel(photo_channel) is None))):
            raise RequestError('invalid request destination')
        recent = store.conn.execute('SELECT 1 FROM selfie_request WHERE guid=? AND created>? '
                                     'LIMIT 1', (guid, now - MEMBER_GAP)).fetchone()
        if recent:
            return None, 'cooldown'
        store.conn.execute('INSERT INTO selfie_request(message,guid,asked_channel,thread,'
                           'photo_channel,audience,created,expires) VALUES(?,?,?,?,?,?,?,?)',
                           (message, guid, asked_channel, thread, photo_channel, audience,
                            now, now + LIFETIME))
        return get(store, message), 'accepted'


def bind_snap(store, message: int, snap_id: int, *, kind: str) -> None:
    if kind not in ('fresh', 'fallback'):
        raise RequestError('invalid photo role')
    column = kind + '_snap'
    with store.transaction():
        request, photo = get(store, message), snap(store, snap_id)
        if (request is None or photo is None or photo.source != 'request'
                or photo.request != message or photo.guid != request.guid
                or photo.audience != request.audience or request.ended is not None
                or getattr(request, column) not in (None, snap_id)):
            raise RequestError('photo does not belong to request')
        store.conn.execute(f'UPDATE selfie_request SET {column}=? WHERE message=?',
                           (snap_id, message))


def plan_fresh(store, request: Request, *, scene: dict, pick: int, looks: str,
               now: float) -> snaps.SnapRow:
    """Bind an immutable request scene before borrowing. Safe to repeat after a restart."""
    with store.transaction():
        latest = get(store, request.message)
        if latest is None or latest.ended or latest.failure:
            raise RequestError('request is no longer open')
        if latest.fresh_snap is not None:
            return snap(store, latest.fresh_snap)
        if now >= latest.expires or scene.get('audience') != latest.audience:
            raise RequestError('request scene is unavailable')
        sid = snaps.plan_snap(store, kind='situation', guid=latest.guid, source='request',
            request=latest.message, ref=f'request:{latest.message}:fresh', night='request',
            scene=scene, audience=latest.audience, phases=PHASES, not_before=latest.created,
            expires=latest.expires, now=now, pick=pick, looks=looks)
        if sid is None:
            raise RequestError('request photo already exists without its binding')
        bind_snap(store, latest.message, sid, kind='fresh')
        return snap(store, sid)


def mark_delivery(store, message: int, kind: str, message_id: int) -> bool:
    """Record one durable delivery. Same receipt is idempotent; a different second is refused.

    fresh/fallback require the already-sent bound snap and matching Discord attachment.
    initial is the ordinary answer in the asked channel; followup is in the photo destination.
    A failure can be said privately or in the asked channel, never an unrelated destination.
    """
    column = _DELIVERIES.get(kind)
    if column is None:
        raise RequestError('invalid delivery role')
    with store.transaction():
        request = get(store, message)
        if request is None:
            raise RequestError('missing request')
        previous = getattr(request, column)
        if previous is not None:
            return previous == message_id
        row = store.conn.execute('SELECT channel,thread,author,origin,deleted FROM discord_message '
                                 'WHERE id=?', (message_id,)).fetchone()
        destinations = ({request.asked_channel, request.photo_channel} if kind == 'failure'
                        else {request.asked_channel if kind == 'initial' else request.photo_channel})
        if (row is None or row['origin'] != 'mind' or row['author'] != request.guid
                or row['deleted'] is not None or row['channel'] not in destinations
                or row['thread'] != (request.thread if row['channel'] == request.asked_channel else None)):
            raise RequestError('delivery does not belong to request')
        if kind in ('fresh', 'fallback'):
            photo = snap(store, getattr(request, kind + '_snap'))
            attachment = store.conn.execute('SELECT snap FROM discord_attachment WHERE message=? '
                                             "AND kind='snap'", (message_id,)).fetchone()
            if (photo is None or photo.status != 'sent' or photo.sender != request.guid
                    or photo.channel != ('dm' if request.audience == 'him' else 'discord')
                    or attachment is None
                    or attachment[0] != photo.id
                    or store.get_meta(f'discord:line:{message_id}') != str(photo.line_id)):
                raise RequestError('photo delivery receipt is incomplete')
        if kind == 'failure' and request.failure is None:
            raise RequestError('request has no failure to report')
        store.conn.execute(f'UPDATE selfie_request SET {column}=? WHERE message=?',
                           (message_id, message))
        return True


def mark_initial(store, message: int, message_id: int) -> bool:
    """A words-only ordinary answer is a receipt too; it does not complete the fresh photo."""
    return mark_delivery(store, message, 'initial', message_id)


def fail(store, message: int, code: str, *, now: float) -> None:
    """Remember a render failure until its single notification commits; retained files stay."""
    if not isinstance(code, str) or not re.fullmatch(r'[a-zA-Z0-9_:-]{1,60}', code):
        raise RequestError('invalid failure code')
    with store.transaction():
        request = get(store, message)
        if request is None:
            raise RequestError('missing request')
        if request.fresh_message is not None or request.ended is not None:
            return
        store.conn.execute('UPDATE selfie_request SET failure=COALESCE(failure,?) WHERE message=?',
                           (code, message))
        photo = snap(store, request.fresh_snap)
        if photo is not None and photo.status == 'planned':
            snaps.expire(store, photo.id, 'flagged' if code == 'flagged' else 'missing', now=now)


def attempt(store, message: int, part: str, *, now: float) -> bool:
    """Count one try at delivering `part`: the first and RETRIES more. One more ends the request
    `passed`, its failure `retry_limit` unless it had one, and closes its open photos (files stay)."""
    if part not in ('initial', 'followup', 'failure'):
        raise RequestError('invalid delivery role')
    key = f'photos:request:tries:{message}:{part}'
    with store.transaction():
        request = get(store, message)
        if request is None or request.ended is not None:
            return False
        raw = store.get_meta(key)
        tries = 0 if raw is None else int(raw) if raw.isdigit() else RETRIES + 1
        if tries <= RETRIES:
            store.set_meta(key, str(tries + 1))
            return True
        store.conn.execute("UPDATE selfie_request SET ended='passed',failure=COALESCE(failure,'retry_limit') "
                           'WHERE message=?', (message,))
        for snap_id in (request.fresh_snap, request.fallback_snap):
            photo = snap(store, snap_id)
            if photo is not None and photo.status in ('planned', 'ready'):
                snaps.expire(store, photo.id, 'passed', now=now)
        return False


def expire_unrendered(store, *, now: float) -> list[Request]:
    """Only unrendered requests expire at 24h; a ready photo survives to its delivery receipt."""
    out = []
    with store.transaction():
        rows = store.conn.execute('SELECT r.message FROM selfie_request r LEFT JOIN selfie_snap s '
            'ON s.id=r.fresh_snap WHERE r.ended IS NULL AND r.failure IS NULL AND r.expires<=? '
            "AND (s.id IS NULL OR s.status='planned')", (now,)).fetchall()
        for row in rows:
            request = get(store, row[0])
            for snap_id in (request.fresh_snap, request.fallback_snap):
                photo = snap(store, snap_id)
                if photo is not None and photo.status in ('planned', 'ready'):
                    snaps.expire(store, photo.id, 'passed', now=now)
            store.conn.execute("UPDATE selfie_request SET ended='passed' WHERE message=?", (row[0],))
            out.append(get(store, row[0]))
    return out


def due(store, *, now: float) -> list[Request]:
    """Pending preparation/render/delivery and unreported failures, oldest first.

    Presence, lease and night eligibility belong to the caller. Rendered photos remain due
    after 24h; only still-unrendered requests are expired here.
    """
    expire_unrendered(store, now=now)
    with store.transaction():
        rows = store.conn.execute(f'SELECT {_COLS} FROM selfie_request WHERE ended IS NULL AND '
            '((failure IS NOT NULL AND failure_message IS NULL) OR (failure IS NULL AND '
            '(initial_message IS NULL OR fresh_message IS NULL))) ORDER BY created,message').fetchall()
        return [Request(*tuple(row)) for row in rows]


def borrow_available(store, *, now: float, local_date: str) -> bool:
    """A read-only preflight. admit_borrow repeats it at the actual serialized admission."""
    with store.transaction():
        hour, day = store.conn.execute('SELECT '
            'sum(started>?),sum(local_date=?) FROM selfie_request_borrow',
            (now - HOUR, local_date)).fetchone()
        return (hour or 0) < HOUR_BORROWS and (day or 0) < DAY_BORROWS


def admit_borrow(store, message: int, *, now: float, local_date: str) -> bool:
    """Record an actual borrow, including a subsequent failed render, under one transaction."""
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', local_date):
        raise RequestError('invalid local date')
    with store.transaction():
        request = get(store, message)
        if (request is None or request.ended or request.failure or request.fresh_message is not None
                or now >= request.expires
                or not borrow_available(store, now=now, local_date=local_date)):
            return False
        store.conn.execute('INSERT INTO selfie_request_borrow(request,started,local_date) VALUES(?,?,?)',
                           (message, now, local_date))
        return True


def remember_image(store, snap_id: int, *, identity: str, digest: str) -> None:
    with store.transaction():
        store.conn.execute('INSERT INTO selfie_request_image(snap,identity,digest) VALUES(?,?,?)',
                           (snap_id, identity, digest))


def image_sent(store, *, guid: int, audience: str, identity: str, digest: str, now: float) -> bool:
    """Copies of this image already delivered to this audience in the preceding 30 days."""
    with store.transaction():
        return store.conn.execute('SELECT 1 FROM selfie_request_image i JOIN selfie_snap s ON s.id=i.snap '
            "WHERE s.guid=? AND s.audience=? AND s.status='sent' AND s.sent>? "
            'AND (i.identity=? OR i.digest=?) LIMIT 1',
            (guid, audience, now - REPEAT_WINDOW, identity, digest)).fetchone() is not None
