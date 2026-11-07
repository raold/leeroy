"""Merge an extracted claim without a second model call."""
from leeroy_mind.storage.rows import cosine, unpack_vec

from . import store as claims
from .stance import reversal

# Match memory/writer.py's conservative dedup gate. Synthetic paraphrase fixtures
# exercise the boundary; this is an initial heuristic, not a measured accuracy claim.
T_SAME = 0.92


def merge(store, guid: int, candidate, embedding: bytes, *, now: float) -> tuple[int, str]:
    """Same subject/kind and stance at >= T_SAME reinforces. A reversal supersedes
    even at high cosine (opposites often embed closely); otherwise add. Multiple
    compatible facts may share a subject. All row/vector changes are atomic.
    """
    embedding = claims.vector(embedding)
    subject = claims.normal_subject(candidate.subject)
    expires = (now + candidate.horizon_days * 86400 if candidate.kind == 'plan' else None)
    with store.transaction():
        rows = store.conn.execute(
            'SELECT * FROM member_claim WHERE guid=? AND subject=? AND kind=? '
            'AND superseded_by IS NULL AND (expires IS NULL OR expires>?) ORDER BY id',
            (guid, subject, candidate.kind, now)).fetchall()
        old = [claims.Claim(*tuple(r)) for r in rows]
        replaced = [c for c in old if
                    (c.polarity * candidate.polarity == -1)
                    or reversal(c.kind, c.text, candidate.text, c.polarity)]
        same = [c for c in old if c not in replaced and c.polarity == candidate.polarity
                and cosine(unpack_vec(c.embedding), unpack_vec(embedding)) >= T_SAME]
        if same:
            cid = same[-1].id
            claims.reinforce(store, cid, now=now)
            action = 'reinforce'
        else:
            cid = claims.add(store, guid, kind=candidate.kind, subject=subject,
                             text=candidate.text, polarity=candidate.polarity, embedding=embedding,
                             source_line=candidate.source_line, expires=expires, now=now)
            action = 'supersede' if replaced else 'add'
        for c in replaced:
            claims.supersede(store, c.id, cid)
        return cid, action
