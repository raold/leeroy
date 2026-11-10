"""Capped, resumable numeric scoring. No model, embeddings or live service calls."""
import json
from leeroy_mind.nightly.budget import register_counter
from .config import load_learn_config
from .reward import DAY, reward
from .weights import available, ensure

PER_NIGHT = 500
PLAN = "nightly:rewards:plan:"


def _selection(ctx):
    saved = ctx.store.get_meta(PLAN + ctx.night)
    if saved is not None:
        try:
            ids = json.loads(saved)
            if (type(ids) is not list or any(type(i) is not int or i <= 0 for i in ids)
                    or len(ids) != len(set(ids))):
                return []
            return ids[:PER_NIGHT]
        except (ValueError, TypeError, RecursionError):
            return []
    members = ctx.members(include_away=True)
    return [r[0] for r in ctx.store.conn.execute(
        "SELECT id FROM chat_line WHERE origin='mind' AND from_guid IN "
        "(SELECT value FROM json_each(?)) AND ts>=? AND ts<? ORDER BY ts,id LIMIT ?",
        (json.dumps(members), max(ctx.window[0], ctx.window[1] - DAY), ctx.window[1], PER_NIGHT))]


def count(ctx):
    if (not load_learn_config(ctx.settings.data_dir / "mind.toml").enabled
            or not available(ctx.store.conn)):
        return 0
    return sum(not ctx.done("rewards", str(i)) for i in _selection(ctx))


register_counter("rewards", count)


def attach(runner, data_dir):
    if load_learn_config(data_dir / "mind.toml").enabled:
        names = {s.name for s in runner.stages}
        runner.add_stage("rewards", stage_rewards,
                         after="signals" if "signals" in names else "threads", model=None)


async def stage_rewards(ctx):
    if not load_learn_config(ctx.settings.data_dir / "mind.toml").enabled:
        return
    conn = ctx.store.conn
    if not available(conn):
        return
    with ctx.store.transaction():
        ids = _selection(ctx)
        if not ids:
            return
        if ctx.store.get_meta(PLAN + ctx.night) is None:
            ctx.store.set_meta(PLAN + ctx.night, json.dumps(ids))
        ensure(conn, now=ctx.clock.now())

    async def one(unit):
        def write(store):
            try:
                lid = int(unit)
                row = conn.execute("SELECT from_guid FROM chat_line WHERE id=?", (lid,)).fetchone()
                if row is None:
                    return
                scored = reward(conn, lid, now=ctx.clock.now())
                if scored.parts.get("available") != 1:
                    return
                conn.execute("INSERT OR IGNORE INTO talk_reward"
                    "(line_id,guid,value,parts,weights_version,scored_at) VALUES (?,?,?,?,?,?)",
                    (lid, row[0], scored.value, json.dumps(scored.parts, sort_keys=True,
                        allow_nan=False), scored.weights_version, ctx.clock.now()))
            except Exception:
                raise RuntimeError("reward_write_failed") from None
        ctx.commit("rewards", unit, write)

    await ctx.map_units("rewards", [str(i) for i in ids], one)
