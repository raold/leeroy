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
        return {"kind": "overrule", "label": "Overrule", "words": "Not in effect yet: overruling stops it."}
    if apply.reversible(row):
        return {"kind": "undo", "label": "Undo", "words": "Undo restores what it changed in game."}
    return {"kind": "overrule", "label": "Overrule future effect", "words": CANT_UNDO}


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
    return {"id": row["id"], "channel": row["channel"], "question": row["question"],
            "options": [{"code": o, "count": n} for o, n in counts.items()], "status": row["status"],
            "opened": row["opened"], "closes": row["closes"], "result": row["result"], "kind": row["kind"],
            "voters": len(voters_of(row)), "votes": sum(counts.values()), "his_vote": mine,
            "he_votes": human in voters_of(row)}


def decisions_view(conn, *, now: float, enabled: bool = False) -> dict:
    have = views.tables(conn)
    missing = [t for t in TABLES if t not in have]
    human = human_guid(conn)
    out = {"enabled": enabled, "human": human, "missing": missing, "decisions": [], "polls": []}
    if missing:
        return out
    apply.load_builtin()
    names = views.names(conn)
    name = lambda g: names.get(g, f"#{g}")  # noqa: E731
    for row in views.rows(conn, "SELECT * FROM guild_decision ORDER BY id DESC LIMIT ?", (LIMIT,)):
        message, poll = apply.channels_of(conn, row)
        out["decisions"].append({
            "id": row["id"], "kind": row["kind"], "label": apply.LABELS[row["kind"]], "subject": row["subject"],
            "summary": apply.summary(row, name), "status": row["status"], "state": apply.STATES[row["status"]],
            "settled_by": row["settled_by"] if row["settled"] is not None else None,
            "settler": (("a poll" if row["settled_by"] == 0 else name(row["settled_by"]))
                        if row["settled"] is not None else ""),
            "proposed": row["proposed"], "settled": row["settled"], "effect": row["effect"], "ended": row["ended"],
            "changed": apply.effects(row), "positions": _positions(row["positions"], names),
            "poll": row["poll"], "supersedes": row["supersedes"], "overruled": bool(row["overruled_by_owner"]),
            "officers_only": apply.officers_only(row, message) or apply.private_channel(poll),
            "action": _action(row)})
    for row in views.rows(conn, "SELECT * FROM guild_poll ORDER BY (status = 'open') DESC, id DESC LIMIT ?", (POLLS,)):
        card = poll_card(conn, row, human)
        card["positions"] = [{**p, "name": names.get(p["guid"], "")} for p in positions_of(row)]
        card["open"] = row["status"] == "open" and now < row["closes"]
        out["polls"].append(card)
    return out
