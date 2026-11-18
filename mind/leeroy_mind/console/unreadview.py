"""What reached him since the page last looked, for the tab title's count while the page is hidden
(static/js/unread.mjs): whispers to him, messages in his DMs and group DMs, and channel messages
that mention him or answer one of his. Counts, the newest sender's name and row ids only; never a
message's text. Read-only; a table an older mind.db lacks counts as nothing.

`chat` and `discord` are the newest chat_line and discord_message ids the page already holds; a
page that has not read one yet leaves it out and takes this answer's `chat_top` or `discord_top`
as its start, with nothing counted before it."""
import json
import re

from . import feed, views

SCAN = 500  # the newest channel rows after the mark that one read looks at for mentions
# His DMs and group DMs are counted by SQL, whole: never lost behind newer channel rows
PRIVATE = "(substr(m.channel, 1, 3) = 'dm:' OR substr(m.channel, 1, 4) = 'gdm:')"
NEW = "m.id > ? AND m.origin = 'mind' AND m.deleted IS NULL"


def _top(conn, table: str) -> int:
    got = views.rows(conn, f"SELECT max(id) FROM {table}")
    return int(got[0][0] or 0) if got else 0


def _mentions(text: str, name: str) -> bool:
    if not name or not isinstance(text, str):
        return False
    wanted = name.casefold()
    return any(m.casefold() == wanted for m in re.findall(r"@([^\W]+)", text, flags=re.UNICODE))


def unread_view(conn, *, chat: int | None, discord: int | None, now: float | None = None) -> dict:
    human = feed.human_guid(conn)
    names = views.names(conn)
    stored = views.rows(conn, "SELECT value FROM meta WHERE key = 'human_name'")
    hname = stored[0][0] if stored and isinstance(stored[0][0], str) else names.get(human, "")
    chat_top, discord_top = _top(conn, "chat_line"), _top(conn, "discord_message")
    out = {"whispers": 0, "dms": 0, "mentions": 0, "latest": None,
           "chat_top": chat_top, "discord_top": discord_top}
    newest = (0.0, None)
    if chat is not None and human and "chat_line" in views.tables(conn):
        for line in feed._query(conn, after=chat, upto=None, since=None, limit=feed.FEED_LIMIT,
                                ids=None, now=now, only="whispers", settled=True):
            if line["you"]:
                continue
            out["whispers"] += 1
            if line["ts"] >= newest[0]:
                newest = (line["ts"], {"key": f"w:{line['id']}", "kind": "whisper",
                                       "name": line["from"] or names.get(line["from_guid"], "")})
    if discord is not None and human:
        out["dms"] = int(views.rows(conn,
            f"SELECT count(*) FROM discord_message m WHERE {NEW} AND {PRIVATE}", (discord,))[0][0] or 0)
        rows = views.rows(conn,
            f"SELECT m.id, m.ts, m.author FROM discord_message m WHERE {NEW} AND {PRIVATE} "
            "ORDER BY m.id DESC LIMIT 1", (discord,))
        for r in rows:
            if r["ts"] >= newest[0]:
                newest = (r["ts"], {"key": f"d:{r['id']}", "kind": "dm", "name": names.get(r["author"], "")})
        rows = views.rows(conn,
            f"SELECT m.id, m.ts, m.author, m.text, m.reply_to FROM discord_message m "
            f"WHERE {NEW} AND NOT {PRIVATE} ORDER BY m.id DESC LIMIT ?", (discord, SCAN))
        parents = sorted({r["reply_to"] for r in rows if r["reply_to"]})
        his = {r[0] for r in views.rows(conn,
            "SELECT id FROM discord_message WHERE author = ? AND origin = 'human' "
            "AND id IN (SELECT value FROM json_each(?))", (human, json.dumps(parents)))} if parents else set()
        for r in rows:
            if not (r["reply_to"] in his or _mentions(r["text"], hname)):
                continue
            out["mentions"] += 1
            if r["ts"] >= newest[0]:
                newest = (r["ts"], {"key": f"d:{r['id']}", "kind": "mention", "name": names.get(r["author"], "")})
    out["latest"] = newest[1]
    return out
