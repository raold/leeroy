"""The Guild tab's Decisions: the guild's decision ledger newest first (kind, subject, who settled
it, when it was settled and took effect, its status, what it changed, rendered by kind from its
effect references, and the positions argued) and its polls with their counts and his vote. Read-
only from mind.db; his vote and his overrule go through the mind's `decision` op. A decision in
effect offers Undo only where its kind says the game can undo it; otherwise the page says it
can't be undone in game and offers to overrule its future effect only. A table an older mind.db
lacks is named in `missing` and reads as empty."""
import json

from leeroy_mind.decisions import apply
from leeroy_mind.decisions.ledger import LIVE
from leeroy_mind.decisions.polls import options_of, positions_of, voters_of

from . import views
from .feed import human_guid

TABLES = ("guild_decision", "guild_poll", "guild_poll_vote")
LIMIT = 100
POLLS = 20
CANT_UNDO = "Can't be undone in game: overruling stops its future effect only."


def _positions(value, names) -> list[dict]:
    try:
        items = json.loads(value)
    except (TypeError, ValueError):
        return []
    return [{"guid": p["guid"], "name": names.get(p["guid"], ""), "side": p["side"], "why": p["why"]}
            for p in items if isinstance(p, dict) and type(p.get("guid")) is int
            and isinstance(p.get("side"), str) and isinstance(p.get("why"), str)][:40]


def _action(row) -> dict | None:
    if row["status"] not in LIVE:
        return None
    if row["status"] != "in_effect":
        return {"kind": "overrule", "label": "Overrule", "permanent": False,
                "words": "Not in effect yet: overruling stops it, and the rule in force stays."}
    if apply.reversible(row):
        return {"kind": "undo", "label": "Undo", "permanent": False,
                "words": "Undo restores what it changed in game."}
    return {"kind": "overrule", "label": "Overrule future effect", "permanent": True, "words": CANT_UNDO}


def poll_cards(conn, message_ids, human: int) -> dict[int, dict]:
    """The polls the given Discord messages carry, as cards: options with counts, his vote."""
    if not message_ids or "guild_poll" not in views.tables(conn):
        return {}
    rows = views.rows(conn, "SELECT * FROM guild_poll WHERE message IN (SELECT value FROM json_each(?))",
                      (json.dumps(sorted(set(message_ids))),))
    return {row["message"]: poll_card(conn, row, human) for row in rows}


def poll_card(conn, row, human: int) -> dict:
    counts = dict.fromkeys(options_of(row), 0)
    mine = None
    for r in views.rows(conn, "SELECT member, option FROM guild_poll_vote WHERE poll = ?", (row["id"],)):
        if r["option"] in counts:
            counts[r["option"]] += 1
        if r["member"] == human:
            mine = r["option"]
    eligible = len(voters_of(row))
    if row["kind"] == "rank" and row["status"] == "closed":
        snapshot = views.rows(conn, "SELECT json_extract(value,'$.rank_tally.eligible'), "
                                    "json_extract(value,'$.rank_tally.counts.approve'), "
                                    "json_extract(value,'$.rank_tally.counts.keep') FROM guild_decision "
                                    "WHERE kind='rank' AND poll=?", (row["id"],))
        if snapshot and all(type(n) is int and n >= 0 for n in snapshot[0]):
            eligible, approve, keep = snapshot[0]
            counts = {"approve": approve, "keep": keep}
    return {"id": row["id"], "channel": row["channel"], "question": row["question"],
            "options": [{"code": o, "count": n} for o, n in counts.items()], "status": row["status"],
            "opened": row["opened"], "closes": row["closes"], "result": row["result"], "kind": row["kind"],
            "voters": eligible, "votes": sum(counts.values()), "his_vote": mine,
            "he_votes": human in voters_of(row)}


def decisions_view(conn, *, now: float, enabled: bool = False) -> dict:
    have = views.tables(conn)
    missing = [t for t in TABLES if t not in have]
    human = human_guid(conn)
    out = {"enabled": enabled, "human": human, "missing": missing, "decisions": [], "polls": []}
    if missing:
        return out
    if enabled and "meta" in have:
        from leeroy_mind.decisions.restock import history, deliveries
        out["restocks"] = [dict(week=s["week"], deliveries=deliveries(s, views.names(conn)), no_maker=s["no_maker"])
                           for s in history(conn)]
    apply.load_builtin()
    names = views.names(conn)
    name = lambda g: names.get(g, f"#{g}")  # noqa: E731
    for row in views.rows(conn, "SELECT * FROM guild_decision ORDER BY id DESC LIMIT ?", (LIMIT,)):
        message, poll = apply.channels_of(conn, row)
        changed, action = apply.effects(row), _action(row)
        if row["kind"] == "bank" and "realm_direct" in have:
            from leeroy_mind.decisions.bank import delivery
            irreversible, words = delivery(conn, row)
            if irreversible:
                changed.append(words)
                if action:
                    action.update(words=words, permanent=True, confirm=words + " Block further transfers?")
        if action and action["kind"] == "undo" and row["kind"] in ("rank", "strategy"):
            words = ("Schedule roster rank restoration?" if row["kind"] == "rank"
                     else "Schedule strategy cleanup at the next safe point?")
            action.update(words=words, confirm=words)
        out["decisions"].append({
            "id": row["id"], "kind": row["kind"], "label": apply.LABELS[row["kind"]], "subject": row["subject"],
            "summary": apply.summary(row, name), "status": row["status"], "state": apply.STATES[row["status"]],
            "settled_by": row["settled_by"] if row["settled"] is not None else None,
            "settler": (("a poll" if row["settled_by"] == 0 else name(row["settled_by"]))
                        if row["settled"] is not None else ""),
            "proposed": row["proposed"], "settled": row["settled"], "effect": row["effect"], "ended": row["ended"],
            "changed": changed, "positions": _positions(row["positions"], names),
            "poll": row["poll"], "supersedes": row["supersedes"], "overruled": bool(row["overruled_by_owner"]),
            "officers_only": apply.officers_only(row, message) or apply.private_channel(poll),
            "action": action})
    for row in views.rows(conn, "SELECT * FROM guild_poll ORDER BY (status = 'open') DESC, id DESC LIMIT ?", (POLLS,)):
        card = poll_card(conn, row, human)
        card["positions"] = [{**p, "name": names.get(p["guid"], "")} for p in positions_of(row)]
        card["open"] = row["status"] == "open" and now < row["closes"]
        out["polls"].append(card)
    return out
