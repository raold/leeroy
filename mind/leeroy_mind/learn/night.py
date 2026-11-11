"""Capped, resumable scoring that revisits changed numeric evidence."""
import asyncio
from dataclasses import replace
from datetime import datetime
import json
import sqlite3

from leeroy_mind.logs import log
from leeroy_mind.clock import SystemClock
from leeroy_mind.console.feed import read
from leeroy_mind.storage.store import Store
from leeroy_mind.signals import stage as screening
from leeroy_mind.signals.store import REPLY_SECONDS, RETENTION_S
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


def _pending(ctx, *, count=False, open_only=False):
    now = ctx.clock.now()
    fields = "count(*)" if count else "c.id"
    query = (f"SELECT {fields} FROM chat_line c LEFT JOIN talk_reward r ON r.line_id=c.id "
        "WHERE c.origin='mind' AND c.from_guid IN (SELECT value FROM json_each(:members)) "
        "AND c.ts<:end AND c.ts>=:retained AND " +
        ("c.ts>:closed AND " if open_only else "c.ts<=:closed AND ") +
        "((r.line_id IS NULL AND c.ts>=:start) OR " + CHANGED + ")")
    args = dict(members=json.dumps(ctx.members(include_away=True)),end=min(ctx.window[1],now+0.000001),
                start=now-LOOKBACK_DAYS*DAY,retained=now-RETENTION_S,
                closed=now-REPLY_SECONDS,now=now)
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
    line = ctx.store.conn.execute("SELECT ts FROM chat_line WHERE id=?",(lid,)).fetchone()
    if (line is None or not ctx.clock.now()-RETENTION_S <= line[0] <= ctx.clock.now()-REPLY_SECONDS
            or line[0] >= ctx.window[1]):
        return None  # old saved plans also obey the observation and retention fences
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


async def read_work(ctx, question):
    """Each worker owns a read-only SQLite snapshot; no main-loop SQLite scans."""
    with ctx.store.transaction():
        path = ctx.store.conn.execute("PRAGMA database_list").fetchone()[2]
    def owned(conn):
        import sqlite_vec
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        conn.execute("BEGIN")
        store = Store(conn,clock=ctx.clock)
        store._depth = 1  # nested readers must not commit this read snapshot
        return question(replace(ctx,store=store))
    return await asyncio.to_thread(read,path,owned)


def remaining(ctx):
    units = [_unit(ctx,i) for i in _selection(ctx)]
    return sum(u is not None and not ctx.done("rewards",u) for u in units)


async def prepare_count(ctx):
    ctx._memo["rewards:count"] = 0
    if ctx.settings.learn_enabled and ready(ctx.store):
        try:
            ctx._memo["rewards:count"] = await read_work(ctx,remaining)
        except Exception:
            log("rewards_unavailable")


def count(ctx):
    # plan_night prepares this once off-loop; repeated budget cuts use the cache.
    return ctx._memo.get("rewards:count",0) if ctx.settings.learn_enabled else 0


register_counter("rewards",count)


def attach(runner,data_dir,store=None):
    if load_learn_config(data_dir / "mind.toml").enabled and (store is None or ready(store)):
        names = {s.name for s in runner.stages}
        runner.add_stage("rewards",stage_rewards,
                         after="signals" if "signals" in names else "threads",model=None)
        from .bandit_night import attach as attach_bandit
        attach_bandit(runner,data_dir,store)


async def stage_rewards(ctx):
    if not ctx.settings.learn_enabled or not ready(ctx.store):
        return
    conn = ctx.store.conn
    try:
        ids = await read_work(ctx,_selection)
        with ctx.store.transaction():
            if ids and ctx.store.get_meta(PLAN+ctx.night) is None:
                ctx.store.set_meta(PLAN+ctx.night,json.dumps(ids))
            if ids:
                ensure(conn,now=ctx.clock.now())
        units = await read_work(ctx,lambda c:{u:lid for lid in ids if (u:=_unit(c,lid)) is not None})
        def configuration(c):
            path = c.settings.data_dir / "mind.toml"
            guards = screening.load_guards_config(path)
            try:
                claims = screening.load_claims_config(path).enabled
            except screening.ConfigError:
                claims = False
            try:
                facts = screening.unawareness.load_facts(c.store.conn)
            except sqlite3.OperationalError:
                facts = None
            return guards,claims,facts
        config = await read_work(ctx,configuration) if units else None
    except sqlite3.Error:
        log("rewards_unavailable")
        return
    scored_count = 0

    async def one(unit):
        nonlocal scored_count
        lid = units[unit]
        try:
            def evidence(c):
                row = c.store.conn.execute("SELECT * FROM chat_line WHERE id=?",(lid,)).fetchone()
                if row is None:
                    return None
                context,parent = screening.context_for(c.store.conn,row)
                return row,context,parent
            data = await read_work(ctx,evidence)
            if data is None:
                return
            row,context,parent = data
            guards,claims,facts = config
            # Game screening uses exactly the signals adapter. Voice/Discord do
            # not borrow game history or earn an invented clean-quality measure.
            can_screen = row["channel"] in GAME_SCREENED and 0 < len(row["text"]) <= 4000
            vectors = {}
            if can_screen and (claims or guards.semantic_enabled):
                selected = [row,*(context if parent else [])]
                embedded = await ctx.embed([r["text"] for r in selected])
                if len(embedded) != len(selected):
                    raise ValueError("reward_embedding_count")
                vectors = {r["id"]:v for r,v in zip(selected,embedded,strict=True)}
            def calculate(c):
                kinds = screening.screen_row(c.store.conn,row,guards=guards,claims=claims,
                    facts=facts,context=context,parent=parent,vectors=vectors) if can_screen else None
                return reward(c.store.conn,lid,now=c.clock.now(),schema_available=True,screening=kinds,
                    local_at=None if isinstance(c.clock,SystemClock) else
                        lambda ts:datetime.fromtimestamp(ts,c.clock.local().tzinfo))
            scored = await read_work(ctx,calculate)
            if scored.parts.get("available") != 1:
                return
        except Exception:
            raise RuntimeError("reward_screen_failed") from None

        def write(store):
            try:
                store.conn.execute("INSERT INTO talk_reward(line_id,guid,value,cap_weight,parts,weights_version,scored_at) "
                    "VALUES (?,?,?,?,?,?,?) ON CONFLICT(line_id) DO UPDATE SET value=excluded.value,"
                    "cap_weight=excluded.cap_weight,parts=excluded.parts,weights_version=excluded.weights_version,"
                    "scored_at=excluded.scored_at,revision=talk_reward.revision+1", (lid,row["from_guid"],scored.value,scored.cap_weight,
                        json.dumps(scored.parts,sort_keys=True,allow_nan=False),scored.weights_version,ctx.clock.now()))
            except Exception:
                raise RuntimeError("reward_write_failed") from None
        scored_count += ctx.commit("rewards",unit,write)
    await ctx.map_units("rewards",list(units),one)
    try:
        pending,left_over = await read_work(ctx,lambda c:(
            _pending(c,count=True,open_only=True),_pending(c,count=True)))
        ctx._memo["rewards:count"] = await read_work(ctx,remaining)
        log("rewards_pending",night=ctx.night,scored=scored_count,pending=pending,left_over=left_over)
    except sqlite3.Error:
        log("rewards_unavailable")
