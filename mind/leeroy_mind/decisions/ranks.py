"""Officers review observed admissions and attendance. The roster owns all SetRank commands."""
import json

from leeroy_mind.discord import store as discord
from . import ledger, polls
from .bank import members
from .lineup import server_quiet
from .strategy import held

DAY = 86400
CAST_KEY = "decisions:rank:cast"


def observe_cast(store, cast, *, now):
    """Baseline the existing cast once. Only later additions have a known join time."""
    with store.transaction():
        raw = store.get_meta(CAST_KEY)
        previous = json.loads(raw) if raw else {}
        current = {str(g): previous.get(str(g), now if raw else None) for g in cast}
        if raw is None or current != previous:
            store.set_meta(CAST_KEY, json.dumps(current))
        for key in previous.keys() - current.keys():
            for row in (ledger.pending(store.conn, "rank", "rank:"+key), ledger.current(store.conn, "rank", "rank:"+key)):
                if row is not None:
                    (ledger.retire if row["status"] == "in_effect" else ledger.expire)(store, row["id"], now=now)


def rank_overlay(conn) -> dict[int, int]:
    owner = conn.execute("SELECT value FROM meta WHERE key='human_guid'").fetchone()
    if not owner:
        return {}
    cast = {g for g, in conn.execute("SELECT guid FROM cast_member WHERE rank>0 AND guid<>?", (int(owner[0]),))}
    out = {}
    for row in ledger.in_effect(conn, "rank"):
        guid = int(row["subject"][5:])
        rank = json.loads(row["value"]).get("rank")
        if guid in cast and type(rank) is int and 1 <= rank <= 9:
            out[guid] = rank
    return out


def review_due(conn, cast, personas, ranks, trial_rank, *, now):
    human = conn.execute("SELECT value FROM meta WHERE key='human_guid'").fetchone()
    if not human:
        return []
    saved = conn.execute("SELECT value FROM meta WHERE key=?", (CAST_KEY,)).fetchone()
    joined = json.loads(saved[0]) if saved else {}
    raids = dict(conn.execute("SELECT a.guid,count(*) FROM guild_attendance a JOIN guild_raid r USING(raid_id) "
                              "WHERE a.status='raided' AND r.started>=? AND r.ended<=? GROUP BY a.guid",
                              (now-30*DAY, now)))
    out = []
    for g, m in sorted(cast.items()):
        p = personas.get(g)
        if g == int(human[0]) or m.rank in (0, ranks["officer"]) or not p or p.play.guild_role == "officer":
            continue
        n, join = raids.get(g, 0), joined.get(str(g))
        if m.rank == trial_rank and n >= 4:
            rank, why = ranks["raider"], "four_raids"
        elif join is not None and 0 <= now-join <= 7*DAY and n < 4:
            rank, why = trial_rank, "newcomer"
        elif m.rank == ranks["raider"] and n == 0:
            rank, why = ranks["social"], "no_raids"
        else:
            continue
        if rank != m.rank and 1 <= rank <= 9:
            out.append(dict(guid=g, rank=rank, prior=m.rank, why=why, raids=n, joined=join))
    return out


class RankRules:
    kind = "rank"
    restore_previous = False

    def __init__(self):
        self.seen = False

    def on_connection(self, connected):
        self.seen = False

    def reversible(self, row):
        return True

    async def undo(self, app, row):
        if getattr(app, "guild", None):
            app.guild.roster._ranks_due = True

    def due(self, app, cast, now):
        trial = getattr(getattr(getattr(app, "realm", None), "cfg", None), "trial_rank", 4)
        return {row["guid"]: row for row in review_due(app.store.conn, cast, app.personas, app.guild.cfg.ranks,
                                                       trial, now=now)}

    def authorized(self, app, poll, target):
        if not self.seen or target not in members(app):
            return False
        voters = polls.voters_of(poll)
        current = {g for g, m in members(app).items()
                   if m.rank == app.guild.cfg.ranks["officer"] and app.personas.get(g)}
        votes = dict(app.store.conn.execute("SELECT member,option FROM guild_poll_vote WHERE poll=?", (poll["id"],)))
        if not current:
            return False
        if app.human_guid in votes and app.human_guid in voters:
            current.add(app.human_guid)
        return sum(votes.get(g) == "approve" for g in current & voters) > len(current) // 2

    async def tick(self, app, now):
        if not self.seen or not getattr(app, "guild", None):
            return
        store, cast = app.store, members(app)
        ranks = app.guild.cfg.ranks
        due = self.due(app, cast, now)
        for row in store.conn.execute("SELECT * FROM guild_decision WHERE kind='rank' AND status='settled'").fetchall():
            if held(app):
                break
            v = json.loads(row["value"])
            poll = polls.get_poll(store.conn, row["poll"])
            if (due.get(v["guid"], {}).get("rank") != v["rank"] or not poll
                    or not self.authorized(app, poll, v["guid"])):
                ledger.expire(store, row["id"], now=now)
                continue
            ledger.take_effect(store, row["id"], now=now, effect_ref={"rank": v["rank"]})
            app.guild.roster._ranks_due = True
        officers = sorted(g for g, m in cast.items() if m.rank == ranks["officer"] and app.personas.get(g))
        if not officers or server_quiet(app):
            return
        for guid, v in due.items():
            subject = f"rank:{guid}"
            recent = store.conn.execute("SELECT * FROM guild_decision WHERE subject=? ORDER BY id DESC LIMIT 1",
                                        (subject,)).fetchone()
            if recent and (recent["status"] in ("proposed", "settled") or
                           json.loads(recent["value"]).get("rank") == v["rank"] and
                           (recent["status"] == "in_effect" or recent["proposed"] >= now-7*DAY)):
                continue
            text = f"{cast[guid].name}, rank {v['prior']} to {v['rank']}: {v['why'].replace('_',' ')} ({v['raids']} raids in 30 days)."
            positions = [dict(guid=g, side="keep" if v["why"] == "no_raids" and
                              app.personas.get(g).big_five.agreeableness.score >= 7 else "approve", why=v["why"])
                         for g in officers]
            with store.transaction():
                poll = polls.open_poll(store, channel="applications", question=text, options=("approve", "keep"),
                                       voters=officers, closes=now+DAY, now=now, kind="rank", subject=subject,
                                       positions=positions)
                message = discord.post(store, channel="applications", author=officers[0], origin="mind",
                                       text="Rank review. " + text, now=now)
                polls.attach_message(store, poll, message)
                ident = ledger.propose(store, kind="rank", subject=subject, value=v, positions=positions, now=now, poll=poll)
                ledger.announce(store, ident, message)

    async def closed(self, app, poll, winner):
        store, now = app.store, app.clock.now()
        row = ledger.pending(store.conn, "rank", poll["subject"])
        with store.transaction():
            if row and row["poll"] == poll["id"] and row["status"] == "proposed":
                value = json.loads(row["value"])
                target = value["guid"]
                if (winner == "approve" and self.authorized(app, poll, target)
                        and self.due(app, members(app), now).get(target, {}).get("rank") == value["rank"]):
                    ledger.settle(store, row["id"], settled_by=0, poll=poll["id"], now=now)
                else:
                    ledger.expire(store, row["id"], now=now)
            polls.mark_handled(store, poll["id"], now=now)


def rank_rules():
    return RankRules()
