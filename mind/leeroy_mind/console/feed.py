"""Read-only questions the console asks mind.db. Each opens the file read-only (mode=ro) and
closes it again, so the console never holds the mind's database open; writes go through the
mind.

The text feed is what the owner could see in WoW: guild and officer lines while he was online
(in a session), party and raid lines of his own group (his, or from a member the mind counted in
the line's audience, which for those channels is his group), and whispers to or from him. Say,
yell and emotes, and whispers between members, are not his to see here."""
import sqlite3
from contextlib import closing
from pathlib import Path

FEED_LIMIT = 200


def connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True, timeout=2.0)
    conn.row_factory = sqlite3.Row
    return conn


def human_guid(conn) -> int:
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = 'human_guid'").fetchone()
    except sqlite3.Error:
        return 0
    try:
        return int(row["value"]) if row is not None else 0
    except (TypeError, ValueError):
        return 0


def open_session(conn) -> dict | None:
    """The session the mind has open (he is in game, as far as it knows), else None."""
    row = conn.execute("SELECT id, started FROM session WHERE ended IS NULL "
                       "ORDER BY id DESC LIMIT 1").fetchone()
    return None if row is None else {"id": row["id"], "started": row["started"]}


_FEED = """
SELECT l.id, l.ts, l.channel, l.from_guid, l.from_name, l.to_guid, l.text,
       coalesce(c.name, '') AS to_name
FROM chat_line l LEFT JOIN cast_member c ON c.guid = l.to_guid
WHERE l.id > :after AND l.id <= :upto AND l.ts >= :since AND (
    (l.channel IN ('guild', 'officer') AND l.session_id IS NOT NULL)
 OR (l.channel IN ('party', 'raid', 'raid_warning') AND l.session_id IS NOT NULL AND (
        l.from_guid = :human
     OR (l.audience IS NOT NULL AND json_valid(l.audience) AND EXISTS (
            SELECT 1 FROM json_each(l.audience) a WHERE a.value = l.from_guid))))
 OR (l.channel = 'whisper' AND :human != 0 AND (l.from_guid = :human OR l.to_guid = :human)))
ORDER BY l.id DESC LIMIT :limit
"""


def feed(conn, *, after: int = 0, upto: int | None = None, since: float | None = None,
         limit: int = FEED_LIMIT) -> list[dict]:
    """The owner-visible lines with ids after `after` (and up to `upto`), stored at `since` or
    later, oldest first: at most `limit`, the newest."""
    human = human_guid(conn)
    rows = conn.execute(_FEED, {"after": int(after), "human": human, "limit": int(limit),
                                "upto": (1 << 62) if upto is None else int(upto),
                                "since": -1e18 if since is None else float(since),
                                }).fetchall()
    out = []
    for r in reversed(rows):
        out.append({"id": r["id"], "ts": r["ts"], "channel": r["channel"],
                    "from": r["from_name"], "from_guid": r["from_guid"],
                    "to": r["to_name"], "to_guid": r["to_guid"], "text": r["text"],
                    "you": bool(human) and r["from_guid"] == human})
    return out


def last_id(conn) -> int:
    row = conn.execute("SELECT max(id) AS id FROM chat_line").fetchone()
    return int(row["id"] or 0)


def night(conn) -> dict | None:
    """The latest night run: when and how it went (no error text)."""
    try:
        row = conn.execute("SELECT night, status, started, finished, attempts FROM night_run "
                           "ORDER BY started DESC LIMIT 1").fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    return {"night": row["night"], "status": row["status"], "started": row["started"],
            "finished": row["finished"], "attempts": row["attempts"]}


def read(path: Path, question, *args, **kw):
    """question(conn, ...) on a fresh read-only connection, closed afterwards."""
    with closing(connect(path)) as conn:
        return question(conn, *args, **kw)
