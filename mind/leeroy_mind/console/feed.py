"""Read-only questions the console asks mind.db. Each opens the file read-only (mode=ro) and
closes it again, so the console never holds the mind's database open; writes go through the
mind.

The text feed is what the owner could see in WoW: guild and officer lines while he was online
(in a session), spoken room lines (`voice:<room>`) once they played to him, party and raid
lines of his own group (his, or from a member the mind counted in
the line's audience, which for those channels is his group), and whispers to or from him. A
voiced whisper is never typed in WoW: one the voice service dropped because he was deafened or
had muted the member shows as text marked `unheard` (the owner's ruling, 2026-11-06); one dropped
for any other reason does not show. Say, yell and emotes, and whispers between members, are not
his to see here.

The feed has views: guild, officer, group, voice (every room he heard, the voice transcript) and
one whisper thread per member (`w:<guid>`). chat_line is never pruned, so every view's history
stays readable back to its first line (`history`), and `threads` lists every member he has ever
whispered with (the owner's ruling, 2026-11-12: conversations persist permanently)."""
import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path

FEED_LIMIT = 200
HISTORY_LIMIT, HISTORY_MAX = 100, 200  # lines a history page brings by default, and at most
# Whisper lines one snapshot carries across every thread: with FEED_LIMIT for each other view, its
# event stays well under the page's 1 MiB frame limit (eventstream.mjs); older ones read back
SNAPSHOT_WHISPERS = 400
VIEW_RE = re.compile(r"(?:guild|officer|group|voice|w:[1-9]\d{0,9})\Z")
WHISPER_GRACE_S = 3.0  # a whisper is stored a moment before the voice row that says it was spoken
UNHEARD = ("deafened", "muted")  # drop reasons that leave a voiced whisper shown as text, unheard


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
           "EXISTS (SELECT 1 FROM voice_heard h WHERE h.line_id = l.id)",
           "(SELECT max(v.reason) FROM voice_line v WHERE v.line_id = l.id "
           "AND v.state = 'dropped' AND v.reason IN (%s))" % ",".join(f"'{r}'" for r in UNHEARD)),
    False: ("NULL", "0", "NULL"),
}

_FEED = """
WITH base AS (
    SELECT l.id, l.ts, l.channel, l.from_guid, l.from_name, l.to_guid, l.text, l.session_id, l.origin,
           l.audience, coalesce(c.name, '') AS to_name, {vrank} AS vrank, {vheard} AS vheard,
           {vunheard} AS vunheard
    FROM chat_line l LEFT JOIN cast_member c ON c.guid = l.to_guid
    WHERE l.id > :after AND l.id <= :upto AND l.ts >= :since
      AND (:ids IS NULL OR l.id IN (SELECT value FROM json_each(:ids)))
      AND (:only IS NULL
        OR (:only = 'guild' AND l.channel = 'guild')
        OR (:only = 'officer' AND l.channel = 'officer')
        OR (:only = 'group' AND l.channel IN ('party', 'raid', 'raid_warning'))
        OR (:only = 'voice' AND l.channel LIKE 'voice:%')
        OR (:only = 'whispers' AND l.channel = 'whisper')
        OR (:only = 'peer' AND l.channel = 'whisper' AND (l.from_guid = :peer OR l.to_guid = :peer)))
), shown AS (
    SELECT *, CASE
        WHEN channel LIKE 'voice:%' THEN 'voice'
        WHEN channel IN ('guild', 'officer') THEN channel
        WHEN channel IN ('party', 'raid', 'raid_warning') THEN 'group'
        ELSE 'w:' || CASE WHEN from_guid = :human THEN to_guid ELSE from_guid END END AS view
    FROM base WHERE (
        (channel IN ('guild', 'officer') AND session_id IS NOT NULL)
     OR (channel LIKE 'voice:%' AND session_id IS NOT NULL AND (
            CASE WHEN :human != 0 AND from_guid = :human THEN vheard
                 ELSE coalesce(vrank, 0) > 0 END))
     OR (channel IN ('party', 'raid', 'raid_warning') AND session_id IS NOT NULL AND (
            from_guid = :human
         OR (audience IS NOT NULL AND json_valid(audience) AND EXISTS (
                SELECT 1 FROM json_each(audience) a WHERE a.value = from_guid))))
     OR (channel = 'whisper' AND :human != 0 AND (from_guid = :human OR to_guid = :human) AND (
            coalesce(vrank, 0) > 0 OR vheard OR (to_guid = :human AND vunheard IS NOT NULL)
         OR (vrank IS NULL AND (:grace <= 0 OR :now - ts >= :grace)))))
      AND (:settled = 0 OR coalesce(vrank, 0) != 1 OR vheard)
), ranked AS (
    SELECT *, row_number() OVER (PARTITION BY view ORDER BY id DESC) AS rn FROM shown
)
SELECT * FROM ranked WHERE rn <= :limit ORDER BY id
"""

_WAITING = """
SELECT l.id FROM chat_line l
WHERE l.id > :after AND l.id <= :upto AND (
    (l.channel LIKE 'voice:%' AND l.session_id IS NOT NULL
     AND NOT (CASE WHEN :human != 0 AND l.from_guid = :human
                   THEN EXISTS (SELECT 1 FROM voice_heard h WHERE h.line_id = l.id)
                   ELSE EXISTS (SELECT 1 FROM voice_line v WHERE v.line_id = l.id) END))
 OR (l.channel = 'whisper' AND :human != 0 AND (l.from_guid = :human OR l.to_guid = :human)
     AND :grace > 0 AND :now - l.ts < :grace
     AND NOT EXISTS (SELECT 1 FROM voice_line v WHERE v.line_id = l.id)
     AND NOT EXISTS (SELECT 1 FROM voice_heard h WHERE h.line_id = l.id)))
"""


def _has(conn, *tables: str) -> bool:
    n = conn.execute("SELECT count(*) FROM sqlite_master WHERE type = 'table' AND name IN (%s)"
                     % ",".join("?" * len(tables)), tables).fetchone()[0]
    return n == len(tables)


def _line(r, human: int) -> dict:
    channel = r["channel"]
    you = bool(human) and r["from_guid"] == human
    vrank, vheard = r["vrank"] or 0, bool(r["vheard"])
    voice, heard, unheard = None, None, None
    if channel.startswith("voice:"):
        voice = channel[len("voice:"):]
        heard = True if you else vrank == 2
    elif channel == "whisper" and (vrank > 0 or vheard):
        voice = "whisper"
        heard = bool(vheard or vrank == 2)
    elif channel == "whisper" and not you and r["vunheard"] in UNHEARD:
        unheard = r["vunheard"]  # shown as text, never as heard
    return {"id": r["id"], "ts": r["ts"], "channel": channel,
            "from": r["from_name"], "from_guid": r["from_guid"],
            "to": r["to_name"], "to_guid": r["to_guid"], "text": r["text"],
            "you": you, "voice": voice, "heard": heard, "unheard": unheard,
            "feedback_id": r["id"] if r['origin']=='mind' and not you else None}


def _query(conn, *, after, upto, since, limit, ids, now=None, only=None, peer=0,
           settled=False) -> list[dict]:
    human = human_guid(conn)
    voiced = _has(conn, "voice_line", "voice_heard")
    vrank, vheard, vunheard = _VOICE_ROWS[voiced]
    rows = conn.execute(_FEED.format(vrank=vrank, vheard=vheard, vunheard=vunheard), {
        "after": int(after), "human": human, "limit": int(limit),
        "only": only, "peer": int(peer), "settled": 1 if settled else 0,
        "upto": (1 << 62) if upto is None else int(upto),
        "since": -1e18 if since is None else float(since),
        "now": 0.0 if now is None else float(now),
        "grace": WHISPER_GRACE_S if voiced and now is not None else 0.0,
        "ids": None if ids is None else json.dumps([int(i) for i in ids])}).fetchall()
    from .labview import feedback
    votes = feedback(conn, [r['id'] for r in rows if r['origin']=='mind'])
    return [{**_line(r, human), 'feedback': votes.get(r['id'])} for r in rows]


def feed(conn, *, after: int = 0, upto: int | None = None, since: float | None = None,
         limit: int = FEED_LIMIT, now: float | None = None) -> list[dict]:
    """The owner-visible lines with ids after `after` (and up to `upto`), stored at `since` or
    later, oldest first: at most `limit` of each view (guild, officer, group, voice, each whisper
    thread), the newest. A spoken line shows once it has played to him (`heard`) or while it is on its way
    (`sent`, heard False); one the voice service dropped never does, except a whisper dropped
    because he was deafened or had muted the member (`unheard`: the reason; voice and heard
    None). With `now`, a whisper younger than WHISPER_GRACE_S with no voice row yet is held back
    (see `waiting`)."""
    return _query(conn, after=after, upto=upto, since=since, limit=limit, ids=None, now=now)


def history(conn, view: str, before: int, limit: int = HISTORY_LIMIT,
            now: float | None = None) -> dict:
    """One view's shown lines older than line `before`, the newest `limit` of them, oldest first,
    and whether older ones remain (`more`): the page reads a view back a page at a time. A line
    still on its way to him is left out (the feed's own settlement brings it); with `now`, so is a
    whisper still inside WHISPER_GRACE_S with no voice row, as in `feed`."""
    if type(view) is not str or VIEW_RE.match(view) is None:
        raise ValueError("view")
    only, peer = ("peer", int(view[2:])) if view.startswith("w:") else (view, 0)
    lines = _query(conn, after=0, upto=int(before) - 1, since=None, limit=int(limit) + 1,
                   ids=None, now=now, only=only, peer=peer, settled=True)
    more = len(lines) > limit
    return {"lines": lines[1:] if more else lines, "more": more}


def threads(conn, now: float | None = None) -> list[dict]:
    """Every member he has whispered with, newest thread first: their guid, name and the thread's
    newest line the page shows (with `now`, as `feed` shows it; never one still on its way to
    him). The page lists them all and reads a thread's lines when it opens one."""
    out = []
    for line in _query(conn, after=0, upto=None, since=None, limit=1, ids=None, now=now,
                       only="whispers", settled=True):
        peer, name = (line["to_guid"], line["to"]) if line["you"] else (line["from_guid"], line["from"])
        out.append({"guid": peer, "name": name, "last": line})
    out.sort(key=lambda t: t["last"]["id"], reverse=True)
    return out


def view_of(line: dict) -> str:
    """The page's view of a line `feed` gave (model.mjs viewOf)."""
    channel = line["channel"]
    if channel.startswith("voice:"):
        return "voice"
    if channel in ("guild", "officer"):
        return channel
    if channel in ("party", "raid", "raid_warning"):
        return "group"
    return f"w:{line['to_guid'] if line['you'] else line['from_guid']}"


def snapshot_lines(lines: list[dict]) -> tuple[list[dict], list[str]]:
    """A snapshot's lines, read with FEED_LIMIT + 1 of each view, cut to the newest FEED_LIMIT of
    each and SNAPSHOT_WHISPERS whisper lines in all, oldest first, with the views it cut
    (`partial`): for every other view it holds each shown line since its start, so a line the page
    has there and it lacks has gone."""
    views: dict[str, list[dict]] = {}
    for line in lines:
        views.setdefault(view_of(line), []).append(line)
    partial, kept = set(), []
    for view, held in views.items():
        held.sort(key=lambda x: x["id"])
        if len(held) > FEED_LIMIT:
            partial.add(view)
            held = held[-FEED_LIMIT:]
        kept.extend(held)
    whispers = sorted((x for x in kept if view_of(x).startswith("w:")), key=lambda x: x["id"],
                      reverse=True)
    dropped = {x["id"] for x in whispers[SNAPSHOT_WHISPERS:]}
    partial.update(view_of(x) for x in whispers[SNAPSHOT_WHISPERS:])
    return (sorted((x for x in kept if x["id"] not in dropped), key=lambda x: x["id"]),
            sorted(partial))


def look(conn, ids: list[int], now: float | None = None) -> list[dict]:
    """The lines among `ids` the feed shows now, oldest first, whatever their state."""
    ids = list(ids)
    if not ids:
        return []
    return _query(conn, after=0, upto=None, since=None, limit=len(ids), ids=ids, now=now)


def recheck(conn, ids: list[int]) -> tuple[list[dict], list[int]]:
    """For lines the page shows as still on their way: the ones that are no longer waiting (now
    heard, a whisper that turned out typed, or one he did not hear) and the ids that have gone
    from the feed."""
    ids = list(ids)
    lines = look(conn, ids)
    found = {line["id"] for line in lines}
    return ([line for line in lines if line["heard"] is not False],
            [i for i in ids if i not in found])


def waiting(conn, after: int, upto: int, now: float | None = None) -> list[int]:
    """Spoken lines in that id range whose voice row is not written yet (the chat line comes
    first), and with `now` whispers younger than the grace: a later look can still find them."""
    if not _has(conn, "voice_line", "voice_heard"):
        return []
    return [r["id"] for r in conn.execute(_WAITING, {"after": int(after), "upto": int(upto),
                                                     "human": human_guid(conn),
                                                     "now": 0.0 if now is None else float(now),
                                                     "grace": 0.0 if now is None
                                                     else WHISPER_GRACE_S})]


def poll(conn, *, after: int, since: float | None, now: float, ids: list[int],
         limit: int = FEED_LIMIT):
    """One look from a single read snapshot (so a row committed between the queries cannot be
    missed by all of them): (lines after `after`, the last id, ids still waiting for a voice
    row, the current state of `ids` and of those waiting)."""
    conn.execute("BEGIN")
    try:
        upto = last_id(conn)
        lines = feed(conn, after=after, upto=upto, since=since, now=now, limit=limit)
        late = waiting(conn, after, upto, now)
        return lines, upto, late, look(conn, list(ids) + late, now)
    finally:
        conn.rollback()


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
