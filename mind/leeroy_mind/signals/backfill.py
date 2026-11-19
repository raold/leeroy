"""Replay stored Discord exchanges into the owner-response signals (`learn backfill-signals`).

The live capture (signals.capture) only sees what happens while it runs, and before the Discord
adapter existed it saw nothing of Discord. This walks the stored rows in time order through the
same attribution code: the members' Discord lines, his own posts' chat lines (the desk's
`discord:line:<message>` receipts), the message he replied to by hand, his photos and voice
notes, and his emoji as they stand now. Text is read only inside the classifier; what comes out
is line ids, kinds and numbers. A removed emoji and anything lost with a deleted row are gone:
this is what the database still proves, not what a live capture would have seen.

Which member lines were addressed to him is rebuilt from stored facts only: a DM line to him, a
message that replies to one of his, a line whose recorded trigger or learning parent is a line
of his. Anything else is not waiting for his reply, as live."""
from collections import Counter
import sqlite3

from leeroy_mind.discord.channels import DM_PREFIX, GROUP_PREFIX, discord_sql
from leeroy_mind.discord.transport import PAIR_LINE_TURN

from . import discord as D
from . import store as S
from .capture import Capture, Observation

RETENTION_DAYS = 90  # learning evidence older than this is never replayed
LINE_PREFIX = "discord:line:"
PARENT_PREFIX = "learn:discord-parent:"


class Collected:
    """The capture's sink for a replay: what it would write, keyed as the table is."""

    def __init__(self):
        self.rows = {}

    def put(self, conn, line_id, guid, kind, value, ts):
        if (line_id, kind) in self.rows:
            return 0
        self.rows[line_id, kind] = (guid, value, ts)
        return 1

    def upsert(self, conn, line_id, guid, kind, value, ts):
        self.rows[line_id, kind] = (guid, value, ts)
        return 1

    def drop(self, conn, line_id, kind):
        return int(self.rows.pop((line_id, kind), None) is not None)


def family(channel):
    return "dm" if channel.startswith(DM_PREFIX) else "group_dm" if channel.startswith(GROUP_PREFIX) else "channel"


def _table(conn, name):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def ready(conn):
    """The table takes the Discord kinds (the migration that adds them ran)."""
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='talk_signal'").fetchone()
    return row is not None and "react_positive" in row[0]


def plan(conn, *, since, now, owner):
    """(Collected, facts) for the stored Discord exchanges from `since` to `now`. Read-only."""
    since = max(since, now - RETENTION_DAYS * 86400)
    discord = discord_sql("channel")
    line_of, message_of = {}, {}
    for key, value in conn.execute("SELECT key,value FROM meta WHERE key LIKE ?", (LINE_PREFIX + "%",)):
        tail = key[len(LINE_PREFIX):]
        if tail.isascii() and tail.isdigit() and value.isascii() and value.isdigit():
            line_of[int(tail)] = int(value)
            message_of[int(value)] = int(tail)
    messages = {}
    if _table(conn, "discord_message"):
        messages = {r[0]: r[1:] for r in conn.execute(
            "SELECT id,author,reply_to,turn,thread FROM discord_message WHERE ts>=?", (since - D.WINDOW_S,))}
    his_lines = {r[0] for r in conn.execute(
        f"SELECT id FROM chat_line WHERE origin='human' AND from_guid=? AND {discord}", (owner,))}
    parents = {}
    for key, value in conn.execute("SELECT key,value FROM meta WHERE key LIKE ?", (PARENT_PREFIX + "%",)):
        tail = key[len(PARENT_PREFIX):]
        if tail.isdigit() and str(value).isdigit():
            parents[int(tail)] = int(value)
    triggers = {}
    if messages:
        triggers = dict(conn.execute(
            "SELECT turn_id,trigger_line FROM utterance WHERE ts>=? AND turn_id IS NOT NULL "
            "AND trigger_line IS NOT NULL", (int(since - D.WINDOW_S) - 1,)))
    media = {r[0] for r in conn.execute("SELECT message FROM discord_attachment")} if _table(conn, "discord_attachment") else set()
    voice = dict(conn.execute("SELECT message,transcript_ok FROM discord_voice WHERE origin='human'")) \
        if _table(conn, "discord_voice") else {}

    def addressed(line, to_guid):
        mid = message_of.get(line)
        message = messages.get(mid)
        # As live: a brb/back line and a photo's caption are not answers that wait for him.
        if mid in media or (message is not None and (message[2] or '').startswith(PAIR_LINE_TURN)):
            return False
        if to_guid == owner or parents.get(line) in his_lines:
            return True
        if message is None:
            return False
        author, reply_to, turn, _ = message
        parent = messages.get(reply_to)
        return (parent is not None and parent[0] == owner) or triggers.get(turn) in his_lines

    events = []  # (ts, order, id, Observation)
    channels = {}
    for lid, ts, channel, origin, to_guid in conn.execute(
            f"SELECT id,ts,channel,origin,to_guid FROM chat_line WHERE {discord} AND ts>=? AND ts<=? "
            "AND (origin='mind' OR (origin='human' AND from_guid=?)) ORDER BY ts,id",
            (since - D.WINDOW_S, now, owner)):
        channels[lid] = channel
        thread = (messages.get(message_of.get(lid)) or (0, None, None, None))[3] or 0
        if origin == "mind":
            events.append((ts, 0, lid, Observation(
                "sent", ts, owner, (lid,), (addressed(lid, to_guid), False, True, thread))))
        elif ts >= since:
            mid = message_of.get(lid)
            target = (messages.get(mid) or (0, None, None, None))[1]
            reply = line_of.get(target, -1) if target else 0  # a bot's row has no line: nobody
            # A voice note's words are never read, transcribed or not, as live.
            events.append((ts, 1, lid, Observation(
                "discord_owner", ts, owner, (lid, reply, thread), (mid in media or mid in voice, mid in voice))))
    reactions = {}
    if _table(conn, "discord_reaction"):
        for message, emoji, ts in conn.execute(
                "SELECT message,emoji,ts FROM discord_reaction WHERE member=? AND ts>=? AND ts<=? ORDER BY ts",
                (owner, since, now)):
            line, kind = line_of.get(message), D.reaction_kind(emoji)
            if line is not None and kind is not None:
                entry = reactions.setdefault(line, [ts, False, False])
                entry[1 if kind == "react_positive" else 2] = True
    for line, (ts, positive, negative) in reactions.items():
        events.append((ts, 2, line, Observation("reaction", ts, owner, (line,), (positive, negative))))
    events.sort(key=lambda e: e[:3])

    capture, sink = Capture(None, None), Collected()
    capture.sink = sink
    try:
        for ts, _, _, observation in events:
            capture._expire_discord(ts)
            capture._apply(conn, observation)
    finally:
        capture.executor.shutdown(wait=False)
    facts = dict(since=since, his_messages=sum(e[1] == 1 for e in events),
                 member_lines=sum(e[1] == 0 and e[0] >= since for e in events),
                 his_reactions=len(reactions), channels=channels)
    return sink, facts


def report(conn, sink, facts):
    """Counts only: what the replay found, what is new against the table, and what the bandit
    could learn from it (per arm family, the uses a line with a new observation carries)."""
    existing = set()
    if S.available(conn):
        existing = {tuple(r) for r in conn.execute(
            "SELECT line_id,kind FROM talk_signal WHERE source='implicit' AND line_id IS NOT NULL")}
    new = {key: row for key, row in sink.rows.items() if key not in existing}
    lines = {line for line, _ in new}
    channels = facts["channels"]
    out = dict(
        since=facts["since"], his_messages=facts["his_messages"], member_lines=facts["member_lines"],
        his_reactions=facts["his_reactions"],
        observations=len(sink.rows), new_observations=len(new), lines_with_new=len(lines),
        answered_lines=sum(kind == "replied" for _, kind in sink.rows),
        by_kind=dict(sorted(Counter(kind for _, kind in new).items())),
        lines_by_family=dict(sorted(Counter(family(channels.get(line, "")) for line in lines).items())),
        members=len({row[0] for row in new.values()}))
    out["arm_families"] = _arms(conn, lines, facts["since"])
    return out, new


def _arms(conn, lines, since):
    try:
        recipe_of = dict(conn.execute("SELECT line_id,recipe_line_id FROM talk_recipe_line"))
        signalled = {recipe_of.get(line, line) for line in lines}
        families = dict(conn.execute("SELECT id,family FROM learn_arm"))
        rows = conn.execute(
            "SELECT p.line_id,i.value FROM talk_recipe p JOIN chat_line c ON c.id=p.line_id, json_each(p.arm_ids) i "
            f"WHERE c.ts>=? AND {discord_sql('c.channel')}", (since,)).fetchall()
    except sqlite3.OperationalError:
        return {}
    out = {}
    per_arm = Counter()
    for line, arm in rows:
        entry = out.setdefault(families.get(arm, "unknown"), dict(uses=0, uses_with_signal=0, arms=set()))
        entry["uses"] += 1
        entry["arms"].add(arm)
        if line in signalled:
            entry["uses_with_signal"] += 1
            per_arm[arm] += 1
    return {name: dict(uses=e["uses"], uses_with_signal=e["uses_with_signal"], arms=len(e["arms"]),
                       most_for_one_arm=max((per_arm[a] for a in e["arms"]), default=0))
            for name, e in sorted(out.items())}


def write(conn, new):
    """The new observations into the table, in the caller's transaction. Each keeps the time it
    was made, so the rewards' baselines read it where it happened; the rewards stage rescores a
    line whose count of observations changed."""
    written = 0
    for (line, kind), (guid, value, ts) in sorted(new.items()):
        written += S.put(conn, line, guid, kind, value, ts)
    return written
