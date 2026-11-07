"""Claim rows and 512d sqlite-vec vectors. Writers nest in Store.transaction().

Readers take an ordinary connection, including mode=ro, and never commit or
migrate. Pass `now` for replay/tests; otherwise readers use wall-clock seconds.
Expiry retains history and removes only the searchable vector.
"""
import math
import time
from dataclasses import dataclass

from leeroy_mind.storage.db import VEC_DIM
from leeroy_mind.storage.rows import unpack_vec


@dataclass(frozen=True)
class Claim:
    id: int
    guid: int
    kind: str
    subject: str
    text: str
    polarity: int
    source: str
    source_line: int | None
    first_seen: float
    last_seen: float
    seen: int
    expires: float | None
    superseded_by: int | None
    embedding: bytes


def vector(value: bytes) -> bytes:
    if not isinstance(value, bytes) or len(value) != VEC_DIM * 4:
        raise ValueError('claims_vector_dimension')
    parts = unpack_vec(value)
    if not all(math.isfinite(x) for x in parts) or not any(parts):
        raise ValueError('claims_vector_invalid')
    return value


def normal_subject(subject: str) -> str:
    return ' '.join(subject.casefold().split())


def add(store, guid: int, *, kind: str, subject: str, text: str, polarity: int,
        embedding: bytes, source: str = 'chat', source_line: int | None = None,
        expires: float | None = None, now: float | None = None) -> int:
    now = store.clock.now() if now is None else now
    embedding = vector(embedding)
    with store.transaction():
        if source_line is not None and not store.conn.execute(
                "SELECT 1 FROM chat_line WHERE id=? AND from_guid=? AND origin='mind'",
                (source_line, guid)).fetchone():
            raise ValueError('claims_source_invalid')
        cid = store.conn.execute(
            'INSERT INTO member_claim(guid,kind,subject,text,polarity,source,source_line,'
            'first_seen,last_seen,expires,embedding) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
            (guid, kind, normal_subject(subject), text, polarity, source, source_line,
             now, now, expires, embedding)).lastrowid
        if expires is None or expires > now:
            store.conn.execute('INSERT INTO member_claim_vec(claim_id,guid,embedding) VALUES (?,?,?)',
                               (cid, guid, embedding))
        return cid


def reinforce(store, claim_id: int, *, now: float | None = None) -> None:
    now = store.clock.now() if now is None else now
    with store.transaction():
        store.conn.execute('UPDATE member_claim SET seen=seen+1,last_seen=max(last_seen,?) '
                           'WHERE id=? AND superseded_by IS NULL '
                           'AND (expires IS NULL OR expires>?)', (now, claim_id, now))


def supersede(store, claim_id: int, replacement: int) -> None:
    with store.transaction():
        changed = store.conn.execute(
            'UPDATE member_claim SET superseded_by=? WHERE id=? AND id<>? '
            'AND superseded_by IS NULL AND EXISTS (SELECT 1 FROM member_claim n '
            'WHERE n.id=? AND n.guid=member_claim.guid AND n.subject=member_claim.subject '
            'AND n.superseded_by IS NULL)', (replacement, claim_id, replacement, replacement))
        if changed.rowcount != 1:
            raise ValueError('claims_supersede_invalid')
        store.conn.execute('DELETE FROM member_claim_vec WHERE claim_id=?', (claim_id,))


def expire(store, *, now: float | None = None) -> int:
    now = store.clock.now() if now is None else now
    with store.transaction():
        return store.conn.execute('DELETE FROM member_claim_vec WHERE claim_id IN '
                                  '(SELECT id FROM member_claim WHERE expires<=?)',
                                  (now,)).rowcount


def live_claims(conn, guid: int, *, now: float | None = None) -> list[Claim]:
    now = time.time() if now is None else now
    return [Claim(*tuple(r)) for r in conn.execute(
        'SELECT * FROM member_claim WHERE guid=? AND superseded_by IS NULL '
        'AND (expires IS NULL OR expires>?) ORDER BY id', (guid, now))]


def nearest(conn, guid: int, embedding: bytes, k: int = 8, *, now: float | None = None
            ) -> list[tuple[Claim, float]]:
    """Up to k live claims with cosine similarity (larger is closer), in one SQL query.

    Filter inside KNN, so expired rows cannot crowd out eligible neighbours before
    the next night removes their vectors. sqlite-vec scans only this guid's partition.
    """
    now = time.time() if now is None else now
    if k <= 0:
        return []
    rows = conn.execute(
        'SELECT c.*, v.distance FROM member_claim_vec v JOIN member_claim c ON c.id=v.claim_id '
        'WHERE v.embedding MATCH ? AND k=? AND v.guid=? AND v.claim_id IN '
        '(SELECT id FROM member_claim WHERE guid=? AND superseded_by IS NULL '
        'AND (expires IS NULL OR expires>?)) ORDER BY v.distance,c.id',
        (vector(embedding), k, guid, guid, now))
    return [(Claim(*tuple(r)[:-1]), 1.0 - r[-1]) for r in rows]


def cursor(conn, guid: int) -> int:
    row = conn.execute('SELECT last_line FROM member_claim_cursor WHERE guid=?', (guid,)).fetchone()
    return row[0] if row else 0


def advance(store, guid: int, line_id: int) -> None:
    with store.transaction():
        store.conn.execute('INSERT INTO member_claim_cursor VALUES (?,?) ON CONFLICT(guid) '
                           'DO UPDATE SET last_line=max(last_line,excluded.last_line)',
                           (guid, line_id))
