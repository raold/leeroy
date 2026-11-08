"""Claims replay: check each line before learning anything cited to that line."""
from collections import defaultdict

from leeroy_mind.claims.check import check, judge
from leeroy_mind.claims.extract import LINE_CHARS, MAX_LINES, Line as ClaimLine, extract
from leeroy_mind.claims.merge import merge

from .common import Flag, require_scratch


def check_line(conn, guid, text, embedding, *, now=None):
    """Single outgoing line against a member's stored claims: `claims.check.check`.

    No writes, no model call; safe on a live read connection.
    """
    return check(conn, guid, text, embedding, now=now)


async def detect(ctx, lines, vectors, *, use_judge=False):
    """SCRATCH ONLY. Deletes every claim, claim vector and claim cursor in `ctx.store`
    before replaying history, so it raises unless the store was marked by
    `common.mark_scratch` (the eval's copy helper). Never call it on the live store;
    use `check_line` for a single draft.
    """
    store = ctx.store
    require_scratch(store)
    with store.transaction():
        store.conn.execute('DELETE FROM member_claim_vec')
        store.conn.execute('UPDATE member_claim SET superseded_by=NULL')
        store.conn.execute('DELETE FROM member_claim')
        store.conn.execute('DELETE FROM member_claim_cursor')
    members = defaultdict(list)
    for line in sorted(lines, key=lambda item: item.id):
        members[line.guid].append(line)
    flags, claims, checked, long_lines = [], 0, 0, 0
    for guid, own in sorted(members.items()):
        for start in range(0, len(own), MAX_LINES):
            batch = own[start:start + MAX_LINES]
            long_lines += sum(len(line.text) > LINE_CHARS for line in batch)
            candidates = await extract(ctx, guid, [ClaimLine(line.id, line.ts, line.text) for line in batch])
            claim_vectors = await ctx.embed([candidate.text for candidate in candidates]) if candidates else []
            claims += len(candidates)
            learned = defaultdict(list)
            for candidate, vector in zip(candidates, claim_vectors, strict=True):
                learned[candidate.source_line or batch[-1].id].append((candidate, vector))
            for line in batch:
                if line.id in vectors:
                    checked += 1
                    conflicts = check(store.conn, guid, line.text, vectors[line.id], now=line.ts)
                    for conflict in conflicts:
                        if not use_judge or await judge(ctx.reasoner, line.text, conflict):
                            flags.append(Flag(line.id, guid, conflict.reason, conflict.claim_id,
                                              round(conflict.score, 6)))
                with store.transaction():
                    for candidate, vector in learned[line.id]:
                        merge(store, guid, candidate, vector, now=line.ts)
    return flags, dict(checked=checked, extracted_claims=claims, extraction_long_lines=long_lines,
                       batch_lines=MAX_LINES, judged=use_judge)
