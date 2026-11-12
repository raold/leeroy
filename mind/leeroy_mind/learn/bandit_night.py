"""Model-free, revision-aware bandit receipts after rewards."""
from . import arms, bandit
from .config import load_learn_config
from .control import effective
from .night import read_work
from leeroy_mind.nightly.budget import register_counter
from leeroy_mind.logs import log
import json


def pending(ctx, frozen=(), *, families=(), limit=500):
    excluded = ' AND r.guid NOT IN ('+','.join('?' for _ in frozen)+')' if frozen else ''
    return list(ctx.store.conn.execute(
        "SELECT r.line_id,r.revision FROM talk_reward r JOIN talk_recipe p ON p.line_id=r.line_id "
        "WHERE p.bandit_context IS NOT NULL AND p.arm_ids!='[]' AND r.scored_at<=? "
        "AND EXISTS (SELECT 1 FROM json_each(p.arm_ids) i JOIN learn_arm a ON a.id=i.value "
        "WHERE a.family NOT IN (SELECT value FROM json_each(?)) AND NOT EXISTS "
        "(SELECT 1 FROM bandit_credit b WHERE b.line_id=r.line_id AND b.arm_id=a.id AND b.revision=r.revision)) "
        + excluded + " ORDER BY r.scored_at,r.line_id LIMIT ?", (ctx.clock.now(),json.dumps(families),*frozen,limit)))


def attach(runner, data_dir, store=None):
    cfg = load_learn_config(data_dir / "mind.toml")
    if cfg.enabled and cfg.bandit and (store is None or arms.available(store.conn)):
        runner.add_stage("bandit", stage_bandit, after="rewards", model=None)


async def stage_bandit(ctx):
    if not ctx.settings.learn_bandit or not ctx.settings.learn_enabled or not arms.available(ctx.store.conn):
        return
    cfg = effective(ctx.store.conn, load_learn_config(ctx.settings.data_dir / "mind.toml"))
    if not cfg.enabled or not cfg.bandit or cfg.freeze:
        return
    if not ctx.done('bandit','registry'):
        ctx.commit('bandit','registry',lambda store:arms.ensure(store.conn,now=ctx.clock.now()))
    rows = await read_work(ctx,lambda snapshot:pending(snapshot,cfg.frozen_members,families=cfg.frozen_families))
    credited=0
    async def one(unit):
        lid = int(unit.split(":")[0])
        def write(store):
            nonlocal credited
            try:
                credited+=int(bandit.update(store.conn,lid,settings=cfg,now=ctx.clock.now()))
            except Exception:
                raise RuntimeError("bandit_credit_failed") from None
        ctx.commit("bandit",unit,write)
    freeze_key=','.join(sorted(cfg.frozen_families))
    await ctx.map_units("bandit",[f"{r[0]}:{r[1]}:{freeze_key}" for r in rows],one)
    if not ctx.done('bandit','retire:'+freeze_key):
        candidates=await read_work(ctx,lambda snapshot:bandit.retirement_candidates(snapshot.store.conn,settings=cfg))
        ctx.commit('bandit','retire:'+freeze_key,lambda store:bandit.retire(store.conn,candidates))
    log('bandit_credited',count=credited)


register_counter("bandit",lambda ctx:0)  # No generation calls/GPU reservation.
