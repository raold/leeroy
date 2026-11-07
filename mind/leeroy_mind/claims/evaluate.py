"""Chronological replay on a scratch Store; never learn a line before checking it."""
from collections import defaultdict

from .check import check
from .extract import MAX_LINES, Line, extract
from .merge import merge


async def evaluate(ctx) -> dict:
    """Extract once per member from their most recent forty own lines. Activate
    each output only after its cited line is checked. An uncited output waits until
    the batch's end. Existing claims are removed from the scratch copy so a later
    stored belief cannot leak into an earlier check. The report holds ids/counts only.

    Extraction sees the whole batch: this is a retrospective candidate audit, not a
    blind precision/recall benchmark. Citations and semantic correctness need review.
    """
    store = ctx.store
    with store.transaction():
        store.conn.execute('DELETE FROM member_claim_vec')
        store.conn.execute('UPDATE member_claim SET superseded_by=NULL')
        store.conn.execute('DELETE FROM member_claim')
        store.conn.execute('DELETE FROM member_claim_cursor')
    results, lines_seen, claims_seen, members = [], 0, 0, 0
    for guid in ctx.members(include_away=True):
        with store.transaction():
            lines = [Line(*tuple(r)) for r in store.conn.execute(
                "SELECT id,ts,text FROM chat_line WHERE from_guid=? AND origin='mind' "
                'ORDER BY id DESC LIMIT ?', (guid, MAX_LINES))][::-1]
        if not lines:
            continue
        members += 1
        candidates = await extract(ctx, guid, lines)
        claims_seen += len(candidates)
        vectors = await ctx.embed([c.text for c in candidates]) if candidates else []
        by_source = defaultdict(list)
        for candidate, vec in zip(candidates, vectors, strict=True):
            by_source[candidate.source_line or lines[-1].id].append((candidate, vec))
        line_vectors = await ctx.embed([line.text for line in lines])
        for line, vec in zip(lines, line_vectors, strict=True):
            lines_seen += 1
            with store.transaction():
                for conflict in check(store.conn, guid, line.text, vec, now=line.ts):
                    results.append(dict(guid=guid, line_id=line.id, claim_id=conflict.claim_id,
                                        score=round(conflict.score, 4), reason=conflict.reason))
                for candidate, embedding in by_source[line.id]:
                    merge(store, guid, candidate, embedding, now=line.ts)
    return dict(members=members, lines=lines_seen, claims=claims_seen, flagged=len(results),
                conflicts=results)
