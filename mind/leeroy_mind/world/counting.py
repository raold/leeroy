"""World planning reads on an owned read-only connection, away from live chat."""
import asyncio
from dataclasses import replace
from leeroy_mind.console.feed import read
from leeroy_mind.storage.store import Store
from . import stage, photos
from .config import load


async def read_counter(ctx, fn):
    path = ctx.store.conn.execute("PRAGMA database_list").fetchone()[2]
    def question(conn):
        conn.execute("BEGIN")
        scoped = replace(ctx,store=Store(conn,clock=ctx.clock))
        scoped.store._depth = 1  # nested readers cannot commit this read snapshot
        return fn(scoped)
    return await asyncio.to_thread(read,path,question)


def profile(ctx):
    cfg = load(ctx.settings.data_dir / "mind.toml")
    if not cfg.enabled:
        return None
    # The plan only lowers caps. Prefix receipts keep their original allowance
    # semantics even when some selected units have already completed tonight.
    allowed = set(stage.eligible(ctx))
    generation = tuple(g in allowed and not ctx.done(stage.STAGE,str(g))
                       for g in stage.selection(ctx))
    stale = not stage.fresh(ctx.store,ctx.clock.now())
    scoped = photos.WorldPhotos(ctx)
    kinds = tuple(not ctx.done(photos.PHOTO,str(g)) for g in photos.photo_units(ctx,scoped))
    from leeroy_mind.selfies.store import queued_sets, needing_first
    eligible = {g for g in scoped.members()
                if photos.render._held(scoped,photos.render._stored(scoped,g)) is None}
    pending = sum(s.guid in eligible for s in queued_sets(ctx.store)) + len(needing_first(ctx.store,eligible))
    return generation,stale,kinds,pending,cfg.selfies


async def prepare(ctx):
    try:
        ctx._memo["world:counts"] = await read_counter(ctx,profile)
    except Exception:
        from leeroy_mind.logs import log
        ctx._memo["world:counts"] = None
        log("nightly_counter_failed",stage="world",code="world_count_unavailable")


def planned(ctx):
    data = ctx._memo.get("world:counts")
    if data is None:
        return {stage.STAGE:0,photos.PHOTO:0,photos.IMAGE:0}
    generation,stale,kinds,pending,selfies = data
    cap = max(0,ctx.settings.world_per_night)
    new = cap if stale else sum(generation[:cap])
    cap = max(0,ctx.settings.world_selfies_per_night) if selfies else 0
    photo = min(cap,sum(kinds[:cap])+new)
    return {stage.STAGE:new,photos.PHOTO:photo,photos.IMAGE:min(cap,pending+photo)}


async def persona_work(ctx):
    return await read_counter(ctx,stage.count)


async def photo_work(ctx):
    return await read_counter(ctx,photos.photo_count)


async def image_work(ctx):
    def actual(ctx):
        from leeroy_mind.selfies.store import needing_first
        scoped, policy = photos.WorldPhotos(ctx), photos.policy(ctx)
        if photos.render.done_sets(ctx,policy) >= policy.cap:
            return False
        if photos.render.todo(scoped,policy):
            return True
        eligible = {g for g in scoped.members()
                    if photos.render._held(scoped,photos.render._stored(scoped,g)) is None}
        return bool(needing_first(ctx.store,eligible))
    return await read_counter(ctx,actual)
