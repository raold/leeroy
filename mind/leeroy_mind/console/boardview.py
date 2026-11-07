"""The Board tab's read-only question to mind.db: the Realm's forum threads, the letters that have
reached his mailbox and the write-ups of its events. A table an older mind.db lacks is named in
`missing` and reads as empty. Item names come from the mail, else library.db."""
import json
from pathlib import Path

from leeroy_mind.realm.config import ConfigError, load_realm_config

from . import items as library_items
from . import views
from .feed import human_guid

BOARD_TABLES = ("realm_post", "realm_mail", "realm_event")
THREADS = 50
LETTERS = 50
WRITEUPS = 30
POSTS_PER_THREAD = 50


def _realm_on(data_dir: Path):
    try:
        return load_realm_config(Path(data_dir) / "mind.toml").enabled
    except (ConfigError, OSError):
        return None


def _mail_items(raw) -> list[dict]:
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return []
    out = []
    for x in data if isinstance(data, list) else []:
        if isinstance(x, dict) and type(x.get("item_id")) is int:
            count = x.get("count")
            out.append({"id": x["item_id"], "name": x.get("name") if isinstance(x.get("name"), str) else None,
                        "count": count if type(count) is int else 1})
    return out


def board_view(conn, *, now: float, data_dir: Path) -> dict:
    have = views.tables(conn)
    missing = [t for t in BOARD_TABLES if t not in have]
    names = views.names(conn)
    actors = {r["guid"]: (r["name"], r["gname"]) for r in views.rows(
        conn, "SELECT a.guid, a.name, g.name AS gname FROM realm_actor a "
              "LEFT JOIN realm_guild g ON g.id = a.guild")}

    forum = []
    for t in views.rows(conn, "SELECT thread, max(ts) AS last FROM realm_post GROUP BY thread "
                              "ORDER BY last DESC, thread LIMIT ?", (THREADS,)):
        posts = []
        for p in views.rows(conn, "SELECT id, ts, author, author_kind, text FROM realm_post WHERE thread = ? "
                                  "ORDER BY ts DESC, id DESC LIMIT ?", (t["thread"], POSTS_PER_THREAD)):
            if p["author_kind"] == "actor":
                name, guild = actors.get(p["author"], (None, None))
            else:
                name, guild = names.get(p["author"]), None
            posts.append({"id": p["id"], "ts": p["ts"], "author": p["author"], "author_kind": p["author_kind"],
                          "name": name or f"#{p['author']}", "guild": guild, "text": p["text"]})
        posts.reverse()
        forum.append({"thread": t["thread"], "last": t["last"], "posts": posts})

    letters = []
    ids: set[int] = set()
    for m in views.rows(conn, "SELECT id, ts, sender, kind, subject, body, items, state, taken FROM realm_mail "
                              "WHERE state IN ('sent','taken') ORDER BY ts DESC, id DESC LIMIT ?", (LETTERS,)):
        its = _mail_items(m["items"])
        ids.update(i["id"] for i in its)
        letters.append({"id": m["id"], "ts": m["ts"], "sender": m["sender"], "kind": m["kind"],
                        "subject": m["subject"], "body": m["body"], "items": its,
                        "state": m["state"], "taken": m["taken"]})
    known = library_items.item_names(Path(data_dir) / "library.db", ids) if ids else {}
    for m in letters:
        for i in m["items"]:
            lib = known.get(i["id"])
            i["name"] = i["name"] or (lib["name"] if lib else f"item {i['id']}")
            i["quality"] = lib["quality"] if lib else 1

    writeups = []
    for e in views.rows(conn, "SELECT id, kind, place, organiser, start, end, result FROM realm_event "
                              "ORDER BY start DESC, id DESC"):
        try:
            result = json.loads(e["result"])
        except (TypeError, ValueError):
            continue
        text = result.get("writeup") if isinstance(result, dict) else None
        if isinstance(text, str) and text:
            writeups.append({"id": e["id"], "kind": e["kind"], "place": e["place"], "organiser": e["organiser"],
                             "start": e["start"], "end": e["end"], "text": text})
            if len(writeups) >= WRITEUPS:
                break

    return {"human": human_guid(conn), "names": {str(g): n for g, n in names.items()},
            "missing": missing, "realm_on": _realm_on(data_dir),
            "forum": forum, "letters": letters, "writeups": writeups}
