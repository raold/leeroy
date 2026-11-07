"""The guild's Discord in mind.db (migrations/*_discord.sql): messages, threads, reactions, pins.

- A message is deleted by setting `deleted`; the row stays so replies keep their quote, and the
  reads give it back with empty text. Deleting clears its reactions and pin.
- A thread hangs off one parent message of a channel; its messages carry `thread`, and the
  channel's own feed leaves them out.

Each writer runs in `store.transaction()`. A refusal is DiscordRefused, its text the code. The
mind writes; the console reads (the reads take a plain connection, read-only is fine)."""
from .text import MAX_THREAD_NAME, TextRefused, clean_text, is_one_emoji

MAX_REACTIONS = 20
MAX_PINS = 50
_COLS = "id, channel, thread, author, origin, text, reply_to, ts, edited, deleted, pinned"


class DiscordRefused(ValueError):
    """A change the store refuses; `code` (also its text) says why."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _text(s: str) -> str:
    try:
        return clean_text(s)
    except TextRefused as e:
        raise DiscordRefused(e.code) from None


def _message(store, message: int):
    row = store.conn.execute(
        "SELECT channel, author, deleted FROM discord_message WHERE id = ?", (message,)).fetchone()
    if row is None:
        raise DiscordRefused("missing")
    return row


def _live(store, message: int):
    row = _message(store, message)
    if row[2] is not None:
        raise DiscordRefused("deleted")
    return row


def post(store, *, channel: str, author: int, origin: str, text: str, reply_to: int | None = None,
         thread: int | None = None, turn: str | None = None, now: float) -> int:
    text = _text(text)
    if origin not in ("human", "mind"):
        raise DiscordRefused("bad_origin")
    if not channel or len(channel) > 40:
        raise DiscordRefused("bad_channel")
    with store.transaction():
        if reply_to is not None:
            p = store.conn.execute(
                "SELECT channel, deleted FROM discord_message WHERE id = ?", (reply_to,)).fetchone()
            if p is None or p[0] != channel:
                raise DiscordRefused("no_parent")
            if p[1] is not None:
                raise DiscordRefused("deleted_parent")
        if thread is not None:
            t = store.conn.execute(
                "SELECT channel FROM discord_thread WHERE id = ?", (thread,)).fetchone()
            if t is None or t[0] != channel:
                raise DiscordRefused("no_thread")
        return store.conn.execute(
            "INSERT INTO discord_message(channel, thread, author, origin, text, reply_to, ts, turn) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (channel, thread, author, origin, text, reply_to, now, turn)).lastrowid


def edit(store, message: int, *, author: int, text: str, now: float) -> None:
    text = _text(text)
    with store.transaction():
        _, owner, deleted = _message(store, message)
        if deleted is not None:
            raise DiscordRefused("deleted")
        if owner != author:
            raise DiscordRefused("not_yours")
        store.conn.execute("UPDATE discord_message SET text = ?, edited = ? "
                           "WHERE id = ? AND deleted IS NULL", (text, now, message))


def delete(store, message: int, *, author: int, now: float) -> None:
    with store.transaction():
        _, owner, deleted = _message(store, message)
        if deleted is not None:
            raise DiscordRefused("deleted")
        if owner != author:
            raise DiscordRefused("not_yours")
        store.conn.execute("UPDATE discord_message SET deleted = ?, pinned = NULL "
                           "WHERE id = ? AND deleted IS NULL", (now, message))
        store.conn.execute("DELETE FROM discord_reaction WHERE message = ?", (message,))


def react(store, message: int, *, member: int, emoji: str, now: float) -> bool:
    if not is_one_emoji(emoji) or len(emoji) > 16:
        raise DiscordRefused("not_emoji")
    with store.transaction():
        _live(store, message)
        have = store.conn.execute(
            "SELECT emoji FROM discord_reaction WHERE message = ? GROUP BY emoji", (message,)
        ).fetchall()
        if emoji not in {r[0] for r in have} and len(have) >= MAX_REACTIONS:
            raise DiscordRefused("too_many")
        return store.conn.execute(
            "INSERT OR IGNORE INTO discord_reaction(message, emoji, member, ts) VALUES (?, ?, ?, ?)",
            (message, emoji, member, now)).rowcount == 1


def unreact(store, message: int, *, member: int, emoji: str, now: float) -> bool:
    with store.transaction():
        return store.conn.execute(
            "DELETE FROM discord_reaction WHERE message = ? AND emoji = ? AND member = ?",
            (message, emoji, member)).rowcount == 1


def pin(store, message: int, *, now: float) -> None:
    with store.transaction():
        channel, _, _ = _live(store, message)
        if store.conn.execute("SELECT pinned FROM discord_message WHERE id = ?",
                              (message,)).fetchone()[0] is not None:
            return
        n = store.conn.execute(
            "SELECT count(*) FROM discord_message WHERE channel = ? AND pinned IS NOT NULL",
            (channel,)).fetchone()[0]
        if n >= MAX_PINS:
            raise DiscordRefused("too_many_pins")
        store.conn.execute("UPDATE discord_message SET pinned = ? "
                           "WHERE id = ? AND deleted IS NULL AND pinned IS NULL", (now, message))


def unpin(store, message: int) -> None:
    with store.transaction():
        store.conn.execute("UPDATE discord_message SET pinned = NULL WHERE id = ?", (message,))


def open_thread(store, parent: int, *, name: str, now: float) -> int:
    name = " ".join(name.split())
    if not 1 <= len(name) <= MAX_THREAD_NAME:
        raise DiscordRefused("bad_name")
    with store.transaction():
        channel, _, _ = _live(store, parent)
        if store.conn.execute("SELECT 1 FROM discord_thread WHERE parent = ?",
                              (parent,)).fetchone():
            raise DiscordRefused("exists")
        return store.conn.execute(
            "INSERT INTO discord_thread(channel, parent, name, created) VALUES (?, ?, ?, ?)",
            (channel, parent, name, now)).lastrowid


_KEYS = ("id", "channel", "thread", "author", "origin", "text", "reply_to", "ts", "edited",
         "deleted", "pinned")


def _dict(r) -> dict:
    d = dict(zip(_KEYS, tuple(r)))
    if d["deleted"] is not None:
        d["text"] = ""
    return d


def messages(conn, channel: str, *, after: int = 0, limit: int = 100,
             thread: int | None = None) -> list[dict]:
    """Oldest first. With `after` the next `limit` messages past that id; without it the latest
    `limit`. The channel's feed leaves thread messages out; `thread` asks for one thread's."""
    where = "channel = ? AND thread IS NULL" if thread is None else "channel = ? AND thread = ?"
    args = (channel,) if thread is None else (channel, thread)
    if after > 0:
        rows = conn.execute(f"SELECT {_COLS} FROM discord_message WHERE {where} AND id > ? "
                            "ORDER BY id LIMIT ?", (*args, after, limit)).fetchall()
    else:
        rows = conn.execute(f"SELECT {_COLS} FROM discord_message WHERE {where} "
                            "ORDER BY id DESC LIMIT ?", (*args, limit)).fetchall()[::-1]
    return [_dict(r) for r in rows]


def reactions(conn, ids) -> dict[int, list[dict]]:
    ids = list(ids)
    out: dict[int, list[dict]] = {}
    if not ids:
        return out
    rows = conn.execute(
        "SELECT message, emoji, member FROM discord_reaction "
        f"WHERE message IN ({','.join('?' * len(ids))}) ORDER BY ts, rowid", ids).fetchall()
    for message, emoji, member in map(tuple, rows):
        entries = out.setdefault(message, [])
        e = next((x for x in entries if x["emoji"] == emoji), None)
        if e is None:
            e = {"emoji": emoji, "members": [], "count": 0}
            entries.append(e)
        e["members"].append(member)
        e["count"] += 1
    return out


def threads(conn, channel: str) -> list[dict]:
    rows = conn.execute(
        "SELECT t.id, t.channel, t.parent, t.name, t.created, "
        "(SELECT count(*) FROM discord_message m WHERE m.thread = t.id AND m.deleted IS NULL) "
        "FROM discord_thread t WHERE t.channel = ? ORDER BY t.id", (channel,)).fetchall()
    return [dict(zip(("id", "channel", "parent", "name", "created", "count"), tuple(r)))
            for r in rows]


def pins(conn, channel: str) -> list[dict]:
    """Pinned live messages, the latest pin first."""
    return [_dict(r) for r in conn.execute(
        f"SELECT {_COLS} FROM discord_message WHERE channel = ? AND pinned IS NOT NULL "
        "AND deleted IS NULL ORDER BY pinned DESC, id DESC", (channel,)).fetchall()]


def last_id(conn) -> int:
    return conn.execute("SELECT coalesce(max(id), 0) FROM discord_message").fetchone()[0]


def changed_since(conn, since_ts: float) -> list[int]:
    """Ids of messages edited, deleted, pinned or reacted to at or after `since_ts`."""
    return [r[0] for r in conn.execute(
        "SELECT id FROM discord_message WHERE edited >= ?1 OR deleted >= ?1 OR pinned >= ?1 "
        "OR id IN (SELECT message FROM discord_reaction WHERE ts >= ?1) ORDER BY id",
        (since_ts,)).fetchall()]
