"""Optional reflection stage after notes; atomic claims, cursor and unit receipt."""
import json

from leeroy_mind.logs import log
from leeroy_mind.nightly.budget import register_counter
from leeroy_mind.nightly.llm import ReflectionError

from .extract import extract, recent
from .merge import merge
from .store import advance, expire

STAGE = 'claims'


def _key(ctx):
    return f'claims:members:{ctx.night}'


def _todo(ctx) -> list[int]:
    saved = ctx.store.get_meta(_key(ctx))
    if saved is not None:
        return json.loads(saved)
    with ctx.store.transaction():
        members = set(ctx.members(include_away=True))
        due = ctx.store.conn.execute(
            'SELECT l.from_guid,min(l.id) FROM chat_line l LEFT JOIN member_claim_cursor c '
            "ON c.guid=l.from_guid WHERE l.origin='mind' AND l.id>coalesce(c.last_line,0) "
            'AND l.ts<? GROUP BY l.from_guid ORDER BY min(l.id)', (ctx.window[1],)).fetchall()
    # Oldest pending input first prevents a talkative low guid monopolising a cap.
    return [g for g, _ in due if g in members][:ctx.settings.claims_per_night]


async def stage_claims(ctx) -> None:
    with ctx.store.transaction():
        expired = expire(ctx.store)
        members = _todo(ctx)
        ctx.store.set_meta(_key(ctx), json.dumps(members))
    log('claims_expired', count=expired)

    async def one(unit):
        guid = int(unit)
        try:
            with ctx.store.transaction():
                lines = recent(ctx.store.conn, guid, before=ctx.window[1])
            if not lines:
                ctx.skip(STAGE, unit)
                return
            candidates = await extract(ctx, guid, lines)
            vectors = await ctx.embed([c.text for c in candidates]) if candidates else []
            if len(vectors) != len(candidates):
                raise ValueError('claims_embedding_count')
            by_id = {line.id: line for line in lines}

            def write(store):
                try:
                    for candidate, vec in zip(candidates, vectors, strict=True):
                        observed = by_id[candidate.source_line or lines[-1].id].ts
                        merge(store, guid, candidate, vec, now=observed)
                    advance(store, guid, lines[-1].id)
                    expire(store)  # an old backlog plan may already have ended
                except Exception:
                    # NightContext.commit logs exception messages. Never pass a model,
                    # embedding backend, SQL adapter or injected value through to it.
                    raise RuntimeError('claims_write_failed') from None

            ctx.commit(STAGE, unit, write)
        except ReflectionError as exc:
            raise ReflectionError('claims_model_failed', fatal=exc.fatal) from None
        except Exception:
            raise RuntimeError('claims_unit_failed') from None

    await ctx.map_units(STAGE, [str(g) for g in members], one)


def _count(ctx) -> int:
    return sum(not ctx.done(STAGE, str(g)) for g in _todo(ctx))


register_counter(STAGE, _count)
