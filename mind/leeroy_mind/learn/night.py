"""Capped, resumable scoring that revisits changed numeric evidence."""
from datetime import datetime
import json
import sqlite3

from leeroy_mind.logs import log
from leeroy_mind.clock import SystemClock
from leeroy_mind.discord.channels import is_discord
from leeroy_mind.nightly.budget import register_counter
from .config import load_learn_config
from .reward import DAY, GAME_SCREENED, reward
from .weights import available, ensure

PER_NIGHT = 500
LOOKBACK_DAYS = 14
PLAN = "nightly:rewards:plan:"
CHANGED = """(EXISTS (SELECT 1 FROM talk_signal s WHERE s.line_id=c.id AND s.ts<=:now
    AND (s.ts>r.scored_at OR (s.kind='explicit' AND s.source='explicit'
         AND s.value!=json_extract(r.parts,'$.explicit')))) OR
    (SELECT count(*) FROM talk_signal s WHERE s.line_id=c.id AND s.ts<=:now) >
     json_extract(r.parts,'$.evidence_count'))"""


def ready(store):
    """Schema stays fixed for a running mind; each Store connection checks once."""
    if not hasattr(store, "_learn_available"):
        store._learn_available = available(store.conn)
    return store._learn_available


def _pending(ctx, *, count=False):
    now = ctx.clock.now()
    fields = "count(*)" if count else "c.id"
    query = (f"SELECT {fields} FROM chat_line c LEFT JOIN talk_reward r ON r.line_id=c.id "
        "WHERE c.origin='mind' AND c.from_guid IN (SELECT value FROM json_each(:members)) "
        "AND c.ts<:end AND ((r.line_id IS NULL AND c.ts>=:start) OR " + CHANGED + ")")
    args = dict(members=json.dumps(ctx.members(include_away=True)),end=min(ctx.window[1],now+0.000001),
                start=now-LOOKBACK_DAYS*DAY,now=now)
    if not count:
        query += " ORDER BY (r.line_id IS NOT NULL) DESC,c.ts DESC,c.id DESC LIMIT :limit"
        args["limit"] = PER_NIGHT
    rows = ctx.store.conn.execute(query,args).fetchall()
    return rows[0][0] if count else [r[0] for r in rows]


def _selection(ctx):
    saved = ctx.store.get_meta(PLAN+ctx.night)
    if saved is None:
        return _pending(ctx)
    try:
        ids = json.loads(saved)
        if type(ids) is list and all(type(i) is int and i>0 for i in ids) and len(set(ids)) == len(ids):
            return ids[:PER_NIGHT]
    except (ValueError,TypeError,RecursionError):
        pass
    return []


def _unit(ctx, lid):
    row = ctx.store.conn.execute("SELECT scored_at,json_extract(parts,'$.explicit'),"
        "json_extract(parts,'$.evidence_count'),revision FROM talk_reward WHERE line_id=?",(lid,)).fetchone()
    if row is None:
        return str(lid)
    changed = ctx.store.conn.execute("SELECT max(ts),max(CASE WHEN kind='explicit' AND source='explicit' THEN value END),count(*) "
        "FROM talk_signal WHERE line_id=? AND ts<=?",(lid,ctx.clock.now())).fetchone()
    if changed[0] is not None and (changed[0]>row[0] or changed[2]>row[2] or
                                  (changed[1] is not None and changed[1]!=row[1])):
        return f"{lid}:revision:{row[3]+1}"
    return None


def count(ctx):
    if not ctx.settings.learn_enabled or not ready(ctx.store):
        return 0
    try:
        units = [_unit(ctx,i) for i in _selection(ctx)]
        return sum(u is not None and not ctx.done("rewards",u) for u in units)
    except sqlite3.Error:
        return 0


register_counter("rewards",count)


def attach(runner,data_dir,store=None):
    if load_learn_config(data_dir / "mind.toml").enabled and (store is None or ready(store)):
        names = {s.name for s in runner.stages}
        runner.add_stage("rewards",stage_rewards,
                         after="signals" if "signals" in names else "threads",model=None)


async def stage_rewards(ctx):
    if not ctx.settings.learn_enabled or not ready(ctx.store):
        return
    conn = ctx.store.conn
    try:
        with ctx.store.transaction():
            ids = _selection(ctx)
            if not ids:
                return
            if ctx.store.get_meta(PLAN+ctx.night) is None:
                ctx.store.set_meta(PLAN+ctx.night,json.dumps(ids))
            ensure(conn,now=ctx.clock.now())
            units = {u:lid for lid in ids if (u := _unit(ctx,lid)) is not None}
    except sqlite3.Error:
        log("rewards_unavailable")
        return

    async def one(unit):
        lid = units[unit]
        row = conn.execute("SELECT from_guid,channel,length(text) FROM chat_line WHERE id=?",(lid,)).fetchone()
        if row is None:
            return
        # A skipped/failed signals unit cannot make a clean-quality observation.
        # Discord has no game screening: only its explicit evidence is scored.
        if not is_discord(row[1]) and (row[1] not in GAME_SCREENED or row[2]>4000 or conn.execute(
                "SELECT 1 FROM night_step WHERE stage='signals' AND unit=? AND finished<=? LIMIT 1",
                (str(lid),ctx.clock.now())).fetchone() is None):
            return
        def write(store):
            try:
                scored = reward(conn,lid,now=ctx.clock.now(),schema_available=True,
                    local_at=None if isinstance(ctx.clock,SystemClock) else
                        lambda ts:datetime.fromtimestamp(ts,ctx.clock.local().tzinfo))
                if scored.parts.get("available") != 1:
                    return
                conn.execute("INSERT INTO talk_reward(line_id,guid,value,cap_weight,parts,weights_version,scored_at) "
                    "VALUES (?,?,?,?,?,?,?) ON CONFLICT(line_id) DO UPDATE SET value=excluded.value,"
                    "cap_weight=excluded.cap_weight,parts=excluded.parts,weights_version=excluded.weights_version,"
                    "scored_at=excluded.scored_at,revision=talk_reward.revision+1", (lid,row[0],scored.value,scored.cap_weight,
                        json.dumps(scored.parts,sort_keys=True,allow_nan=False),scored.weights_version,ctx.clock.now()))
            except Exception:
                raise RuntimeError("reward_write_failed") from None
        ctx.commit("rewards",unit,write)
    await ctx.map_units("rewards",list(units),one)
    log("rewards_pending",night=ctx.night,count=_pending(ctx,count=True))
