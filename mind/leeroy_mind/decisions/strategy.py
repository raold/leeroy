"""A raid lead's response to two recorded wipes. Training admits it at the resting muster."""
from dataclasses import replace
import json

from leeroy_mind.discord import store as discord
from leeroy_mind.talk.budget import _HOLD
from leeroy_mind.training.cards import Directive, stock_card
from . import ledger
from .bank import members
from .lineup import SETTLE_AFTER_S, server_quiet
from .positions import strategy_positions

MENU = ("lust_earlier", "lust_later", "slower_pulls", "faster_pulls", "wait_at_boss")
LEVERS = {"lust_earlier": {"lust_at": 85}, "lust_later": {"lust_at": 30},
          "slower_pulls": {"PullPlayerReleaseDelay": 125}, "faster_pulls": {"PullPlayerReleaseDelay": 75},
          "wait_at_boss": {"RaidMusterRestTimeoutSecs": 125}}
WEEK = 7 * 86400


def propose_strategy(conn, context: str, *, now) -> dict | None:
    last = conn.execute("SELECT max(proposed) FROM guild_decision WHERE subject=?", ("strategy:"+context,)).fetchone()[0]
    since = int(max(now-WEEK, last or 0) * 1000)
    rows = conn.execute("SELECT p.id,p.covariates,p.end_ms-p.start_ms AS duration,r.lead_guid "
                        "FROM tg_pull p JOIN guild_raid r ON r.raid_id=p.raid_id WHERE p.context=? "
                        "AND p.complete=1 AND p.outcome='wiped' AND p.kind IN ('his','offline') "
                        "AND p.end_ms>? AND p.end_ms<=? ORDER BY p.end_ms DESC LIMIT 2",
                        (context, since, int(now*1000))).fetchall()
    if len(rows) != 2:
        return None
    facts = [json.loads(r["covariates"]) for r in rows]
    if any(f.get("flask_skips", 0) and type(f.get("flask_skips")) is int for f in facts):
        change = "wait_at_boss"
    elif any(f.get("healers") == 1 for f in facts):
        change = "slower_pulls"
    elif all(r["duration"] < 90000 for r in rows):
        change = "lust_earlier"
    elif all(r["duration"] > 360000 for r in rows):
        change = "lust_later"
    else:
        change = "faster_pulls"
    return {"context": context, "change": change, "levers": LEVERS[change], "lead": rows[0]["lead_guid"],
            "wipes": [r["id"] for r in rows], "since_ms": since}


def held(app):
    engine = getattr(app, "engine", None)
    raid = getattr(engine, "raid", None)
    return bool(raid and (raid.phase(app.clock.now()) in _HOLD or engine._in_boss_fight(app.clock.now())))


def layer_for(app, context):
    if held(app):
        return None
    subject = "strategy:" + context
    pending = ledger.pending(app.store.conn, "strategy", subject)
    row = pending if pending and pending["status"] == "settled" else ledger.current(app.store.conn, "strategy", subject)
    if row is None or row["settled_by"] not in members(app):
        return None
    v = json.loads(row["value"])
    if v.get("levers") != LEVERS.get(v.get("change")) or not v.get("levers"):
        return None
    directives = tuple(Directive(k, n) for k, n in v["levers"].items())
    return row["id"], replace(stock_card(context), id=row["id"], directives=directives)


def played(app, ident):
    row = ledger.get(app.store.conn, ident)
    if row["status"] == "settled":
        ledger.take_effect(app.store, ident, effect_ref={"layer": ident}, now=app.clock.now())


class StrategyRules:
    kind = "strategy"

    def __init__(self):
        self.native = False

    def on_hello(self, ev):
        self.native = "training_enabled" in ev.features

    def on_connection(self, connected):
        if not connected:
            self.native = False

    def reversible(self, row):
        return True

    async def undo(self, app, row):
        training = getattr(app, "training", None)
        if training and training.listener._guild_applied == row["id"]:
            # Re-enter normal journalled preparation at the next safe resting status, including
            # one for the same boss. Combat and wipe-silence keep their existing hold.
            training.listener._muster = None

    async def tick(self, app, now):
        if server_quiet(app) or not getattr(getattr(app, "world", None), "members", None):
            return
        conn, cast = app.store.conn, members(app)
        for context, in conn.execute("SELECT DISTINCT context FROM tg_pull WHERE outcome='wiped' AND end_ms>? "
                                     "AND end_ms<=?", ((now-WEEK)*1000, now*1000)).fetchall():
            subject = "strategy:" + context
            row = ledger.pending(conn, "strategy", subject)
            if row is None:
                v = propose_strategy(conn, context, now=now)
                if not v or v["lead"] not in cast:
                    continue
                people = {g: app.personas.get(g) for g in cast}
                positions = strategy_positions(conn, context, v, cast, people)
                ledger.propose(app.store, kind="strategy", subject=subject, value=v, positions=positions, now=now)
            elif row["status"] == "proposed" and now >= row["proposed"] + SETTLE_AFTER_S:
                v = json.loads(row["value"])
                if v["lead"] not in cast:
                    ledger.expire(app.store, row["id"], now=now)
                    continue
                with app.store.transaction():
                    post = discord.post(app.store, channel="strategy", author=v["lead"], origin="mind",
                                        text=f"After two wipes on {context}, let's try {v['change'].replace('_',' ')}.",
                                        now=now)
                    ledger.settle(app.store, row["id"], settled_by=v["lead"], message=post, now=now)
                    training = getattr(app, "training", None)
                    cfg = getattr(getattr(training, "ports", None), "cfg", None)
                    if not self.native or not training or (cfg and (not cfg.telemetry or not cfg.act)):
                        ledger.talk_only(app.store, row["id"], reason="training_off", now=now)


def strategy_rules():
    return StrategyRules()
