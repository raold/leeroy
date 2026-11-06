"""Read-only questions the console asks mind.db. Each opens the file read-only (mode=ro) and
closes it again, so the console never holds the mind's database open; writes go through the
mind.

The text feed is what the owner could see in WoW: guild and officer lines while he was online
(in a session), spoken room lines (`voice:<room>`) once they played to him, party and raid
lines of his own group (his, or from a member the mind counted in
the line's audience, which for those channels is his group), and whispers to or from him. Say,
yell and emotes, and whispers between members, are not his to see here."""
import json
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


_VOICE_ROWS = {
    True: ("(SELECT max(CASE v.state WHEN 'sent' THEN 1 WHEN 'started' THEN 2 WHEN 'done' THEN 2 "
           "WHEN 'cut' THEN 2 ELSE 0 END) FROM voice_line v WHERE v.line_id = l.id)",
           "EXISTS (SELECT 1 FROM voice_heard h WHERE h.line_id = l.id)"),
    False: ("NULL", "0"),
}

_FEED = """
WITH base AS (
    SELECT l.id, l.ts, l.channel, l.from_guid, l.from_name, l.to_guid, l.text, l.session_id,
           l.audience, coalesce(c.name, '') AS to_name, {vrank} AS vrank, {vheard} AS vheard
    FROM chat_line l LEFT JOIN cast_member c ON c.guid = l.to_guid
    WHERE l.id > :after AND l.id <= :upto AND l.ts >= :since
      AND (:ids IS NULL OR l.id IN (SELECT value FROM json_each(:ids)))
), shown AS (
    SELECT *, CASE
        WHEN channel = 'guild' OR (channel LIKE 'voice:%' AND channel != 'voice:officers')
            THEN 'guild'
        WHEN channel = 'officer' OR channel = 'voice:officers' THEN 'officer'
        WHEN channel IN ('party', 'raid', 'raid_warning') THEN 'group'
        ELSE 'whisper' END AS view
    FROM base WHERE (
        (channel IN ('guild', 'officer') AND session_id IS NOT NULL)
     OR (channel LIKE 'voice:%' AND session_id IS NOT NULL AND (
            CASE WHEN :human != 0 AND from_guid = :human THEN vheard
                 ELSE coalesce(vrank, 0) > 0 END))
     OR (channel IN ('party', 'raid', 'raid_warning') AND session_id IS NOT NULL AND (
            from_guid = :human
         OR (audience IS NOT NULL AND json_valid(audience) AND EXISTS (
                SELECT 1 FROM json_each(audience) a WHERE a.value = from_guid))))
     OR (channel = 'whisper' AND :human != 0 AND (from_guid = :human OR to_guid = :human)))
), ranked AS (
    SELECT *, row_number() OVER (PARTITION BY view ORDER BY id DESC) AS rn FROM shown
)
SELECT * FROM ranked WHERE rn <= :limit ORDER BY id
"""

_WAITING = """
SELECT l.id FROM chat_line l
WHERE l.id > :after AND l.id <= :upto AND l.channel LIKE 'voice:%' AND l.session_id IS NOT NULL
  AND NOT (CASE WHEN :human != 0 AND l.from_guid = :human
                THEN EXISTS (SELECT 1 FROM voice_heard h WHERE h.line_id = l.id)
                ELSE EXISTS (SELECT 1 FROM voice_line v WHERE v.line_id = l.id) END)
"""


def _has(conn, *tables: str) -> bool:
    n = conn.execute("SELECT count(*) FROM sqlite_master WHERE type = 'table' AND name IN (%s)"
                     % ",".join("?" * len(tables)), tables).fetchone()[0]
    return n == len(tables)


def _line(r, human: int) -> dict:
    channel = r["channel"]
    you = bool(human) and r["from_guid"] == human
    vrank, vheard = r["vrank"] or 0, bool(r["vheard"])
    voice, heard = None, None
    if channel.startswith("voice:"):
        voice = channel[len("voice:"):]
        heard = True if you else vrank == 2
    elif channel == "whisper" and (vrank > 0 or vheard):
        voice = "whisper"
        heard = bool(vheard or vrank == 2)
    return {"id": r["id"], "ts": r["ts"], "channel": channel,
            "from": r["from_name"], "from_guid": r["from_guid"],
            "to": r["to_name"], "to_guid": r["to_guid"], "text": r["text"],
            "you": you, "voice": voice, "heard": heard}


def _query(conn, *, after, upto, since, limit, ids) -> list[dict]:
    human = human_guid(conn)
    vrank, vheard = _VOICE_ROWS[_has(conn, "voice_line", "voice_heard")]
    rows = conn.execute(_FEED.format(vrank=vrank, vheard=vheard), {
        "after": int(after), "human": human, "limit": int(limit),
        "upto": (1 << 62) if upto is None else int(upto),
        "since": -1e18 if since is None else float(since),
        "ids": None if ids is None else json.dumps([int(i) for i in ids])}).fetchall()
    return [_line(r, human) for r in rows]


def feed(conn, *, after: int = 0, upto: int | None = None, since: float | None = None,
         limit: int = FEED_LIMIT) -> list[dict]:
    """The owner-visible lines with ids after `after` (and up to `upto`), stored at `since` or
    later, oldest first: at most `limit` of each view (guild, officer, group, whispers), the
    newest. A spoken line shows once it has played to him (`heard`) or while it is on its way
    (`sent`, heard False); one the voice service dropped never does."""
    return _query(conn, after=after, upto=upto, since=since, limit=limit, ids=None)


def look(conn, ids: list[int]) -> list[dict]:
    """The lines among `ids` the feed shows now, oldest first, whatever their state."""
    ids = list(ids)
    if not ids:
        return []
    return _query(conn, after=0, upto=None, since=None, limit=len(ids), ids=ids)


def recheck(conn, ids: list[int]) -> tuple[list[dict], list[int]]:
    """For lines the page shows as still on their way: the ones that are no longer waiting (now
    heard, or a whisper that turned out typed) and the ids that have gone from the feed."""
    ids = list(ids)
    lines = look(conn, ids)
    found = {line["id"] for line in lines}
    return ([line for line in lines if line["heard"] is not False],
            [i for i in ids if i not in found])


def waiting(conn, after: int, upto: int) -> list[int]:
    """Spoken lines in that id range whose voice row is not written yet (the chat line comes
    first), so a later look can still find them."""
    if not _has(conn, "voice_line", "voice_heard"):
        return []
    return [r["id"] for r in conn.execute(_WAITING, {"after": int(after), "upto": int(upto),
                                                     "human": human_guid(conn)})]


def guild_mood(conn) -> dict | None:
    """The guild's latest mood (night, value, reasons), None before there is one or when this
    mind.db has no such table."""
    if not _has(conn, "guild_mood"):
        return None
    r = conn.execute("SELECT night, value, reasons FROM guild_mood "
                     "ORDER BY night DESC LIMIT 1").fetchone()
    if r is None:
        return None
    try:
        reasons = json.loads(r["reasons"])
    except (TypeError, ValueError):
        reasons = []
    return {"night": r["night"], "value": r["value"],
            "reasons": [x for x in reasons if isinstance(x, str)] if isinstance(reasons, list)
            else []}


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
