"""A bounded, model-free callback selector. Selection never marks a memory used.

The talk caller passes observed participants and privacy context, and records
use in the same transaction as a successfully sent line's recipe.
"""
from dataclasses import dataclass
import json
import math
from pathlib import Path
import re
import sqlite3
import tomllib
from types import SimpleNamespace

from leeroy_mind.discord.channels import is_private, is_officer_channel, is_discord, channel_id
from leeroy_mind.memory.recall import scope_ok, whisper_ok, hidden_ok
from leeroy_mind.memory.privacy import restricted_channels
from leeroy_mind.storage.rows import cosine, unpack_vec

DAY = 86400
MIN_AGE = 2 * DAY
MAX_AGE = 60 * DAY
REUSE = 14 * DAY
MIN_SALIENCE = 6
MAX_CANDIDATES = 300
MAX_PROVENANCE = 500
_REF = re.compile(r"(memory|chat|event):([1-9][0-9]*)\Z")


def load_callbacks_config(path: Path) -> bool:
    """Only a valid [callbacks] enabled=true table enables callers; fail closed."""
    try:
        table = tomllib.loads(path.read_text(encoding="utf-8-sig")).get("callbacks", {})
        return (isinstance(table, dict) and not set(table) - {"enabled"}
                and type(table.get("enabled", False)) is bool and table.get("enabled", False))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        return False


@dataclass(frozen=True)
class CallbackContext:
    enabled: bool = False
    channel: str = ""
    to_guid: int = 0
    # People actually present, including the owner when known to be present.
    present: tuple[int, ...] = ()
    # Captured current room audience, required for non-guild/non-whisper rooms.
    readers: tuple[int, ...] | None = None
    embedding: bytes | None = None  # existing 512d memory-model vector, never computed here
    bond_enabled: bool = False  # the caller's [romance] activities switch and current pair eligibility


@dataclass(frozen=True)
class Callback:
    memory_id: int
    seed: str
    reason: str
    age_days: float
    scope: str


def _rows(conn, sql, params=()):
    cur = conn.execute(sql, params)
    names = [c[0] for c in cur.description]
    return [dict(zip(names, row)) for row in cur]


def _json(raw, default=None):
    try:
        return json.loads(raw)
    except (ValueError, TypeError, RecursionError):
        return default


def _ids(value):
    return (isinstance(value, (list, tuple)) and len(value) <= 500
            and all(type(n) is int and 0 < n <= 0xFFFFFFFF for n in value))


def _valid(ctx, guid, now):
    return (isinstance(ctx, CallbackContext) and ctx.enabled is True
            and type(guid) is int and 0 < guid <= 0xFFFFFFFF
            and type(now) in (int, float) and math.isfinite(now) and now >= 0
            and isinstance(ctx.channel, str) and 0 < len(ctx.channel) <= 100
            and type(ctx.to_guid) is int and 0 <= ctx.to_guid <= 0xFFFFFFFF
            and _ids(ctx.present) and any(g != guid for g in ctx.present)
            and (ctx.readers is None or _ids(ctx.readers)))


def _old_schema(exc):
    return str(exc).startswith(("no such table:", "no such column:"))


def _play_floor(conn, now):
    """Start of the last hour of recorded owner play, spanning relogs/offline gaps.

    No active session means no offering. Merge overlapping sessions defensively;
    a truncated history is refused rather than granting an unproved fresh quota.
    """
    rows = _rows(conn, "SELECT started,ended FROM session WHERE started<=? "
                 "ORDER BY started DESC LIMIT 1001", (now,))
    if not rows or not any(r["ended"] is None or r["ended"] >= now for r in rows):
        return None
    spans = []
    for r in reversed(rows):
        a, b = r["started"], min(now, r["ended"] if r["ended"] is not None else now)
        if not all(type(n) in (int, float) and math.isfinite(n) for n in (a, b)) or b < a:
            return None
        if spans and a <= spans[-1][1]:
            spans[-1][1] = max(spans[-1][1], b)
        else:
            spans.append([a, b])
    left = 3600
    for a, b in reversed(spans):
        if b - a >= left:
            return b - left
        left -= b - a
    return spans[0][0] if len(rows) <= 1000 else None


class _Privacy:
    """Conservative complete evidence walk, bounded across all candidates.

    Unsupported roots, missing evidence and cycles are not proof of visibility.
    Public and private evidence mixed together still must all pass. Officer and
    historical group/room audiences restrict even a mistakenly open memory.
    """
    def __init__(self, conn, guid, ctx, now):
        self.conn, self.guid, self.ctx, self.now = conn, guid, ctx, now
        self.cache = {}
        self.count = 0
        rows = _rows(conn, "SELECT json FROM persona_core WHERE guid=?", (guid,))
        p = _json(rows[0]["json"], {}) if rows else {}
        self.officer = isinstance(p, dict) and isinstance(p.get("play"), dict) and p["play"].get("guild_role") == "officer"

    def memory_scope(self, row):
        return (row["scope"] in {"open", "private", "secret"}
                and scope_ok(SimpleNamespace(**row), turn_channel=self.ctx.channel, to_guid=self.ctx.to_guid)
                and (row["scope"] == "open" or row["owner"] == self.guid))

    def visible(self, ref, stack=frozenset()):
        if not isinstance(ref, str) or len(ref) > 100 or ref in stack or len(stack) >= 32:
            return False
        if ref in self.cache:
            return self.cache[ref]
        self.count += 1
        if self.count > MAX_PROVENANCE:
            return False
        match = _REF.fullmatch(ref)
        if match is None:
            return False
        kind, ident = match[1], int(match[2])
        if ident > 0x7FFFFFFFFFFFFFFF:
            return False
        table = {"chat": "chat_line", "event": "game_event", "memory": "memory"}[kind]
        rows = _rows(self.conn, f"SELECT * FROM {table} WHERE id=?", (ident,))
        ok = False
        if rows:
            row = rows[0]
            if kind == "memory":
                refs = _json(row["evidence"])
                hidden = restricted_channels(self.conn, {0: refs}).get(0, ()) if isinstance(refs, list) else ()
                ok = (row["t_created"] <= self.now and self.memory_scope(row)
                      and (not hidden or hidden_ok(hidden, self.ctx.channel))
                      and isinstance(refs, list) and 0 < len(refs) <= 40
                      and all(self.visible(r, stack | {ref}) for r in refs))
            elif kind == "event":
                # Public game observations only; no payload text is read or rendered.
                ok = row["ts"] <= self.now and row["type"] in {"boss", "death", "loot_won", "achievement", "bg_end", "zone"}
                if row['type'] == 'mail_taken' and self.ctx.channel == 'whisper' and row['ts'] <= self.now:
                    payload = _json(row['payload'], {})
                    owner = _rows(self.conn, "SELECT value FROM meta WHERE key='human_guid'")
                    if (owner and str(self.ctx.to_guid) == owner[0]['value'] and isinstance(payload, dict)):
                        ok = bool(_rows(self.conn, "SELECT 1 FROM realm_mail WHERE sender=? AND item_guid=? "
                            "AND state='taken' AND taken BETWEEN ? AND ? LIMIT 1",
                            (self.guid, payload.get('item_guid'), row['ts']-2, row['ts']+2)))
            else:
                ch = row["channel"]
                audience = _json(row.get("audience"))
                audience_ok = row.get("audience") is None or (_ids(audience) and self.guid in audience)
                if is_private(ch):  # a whisper or a DM: one conversation between the two
                    ok = (self.guid in (row["from_guid"], row["to_guid"])
                          and whisper_ok(ch, row["from_guid"], row["to_guid"], speaker=self.guid,
                                         turn_channel=self.ctx.channel, to_guid=self.ctx.to_guid))
                elif is_officer_channel(ch):
                    ok = self.officer and hidden_ok((ch,), self.ctx.channel)
                elif ch == "guild":
                    ok = True
                else:
                    # Every current reader must have been in the historical audience.
                    ok = (ch == self.ctx.channel and _ids(audience) and self.ctx.readers is not None
                          and set(self.ctx.readers).issubset(audience) and self.guid in audience)
                ok = ok and audience_ok and row["ts"] <= self.now
                if ok and is_discord(ch):
                    deleted = self.conn.execute("SELECT 1 FROM meta r JOIN discord_message m "
                        "ON m.id=CAST(substr(r.key,14) AS INTEGER) WHERE r.key GLOB 'discord:line:*' "
                        "AND r.value=? AND m.channel=? AND m.deleted IS NOT NULL LIMIT 1",
                        (str(row['id']), channel_id(ch))).fetchone()
                    ok = deleted is None
                # A mind line can itself carry private recalled memories.
                if ok and row["origin"] == "mind":
                    pairs = _rows(self.conn, "SELECT memory_ids FROM utterance WHERE guid=? AND channel=? "
                        "AND to_guid=? AND text=? AND dropped IS NULL AND ts BETWEEN ? AND ? "
                        "ORDER BY abs(ts-?),id LIMIT 1", (row["from_guid"], ch, row["to_guid"], row["text"],
                        row["ts"] - 2, row["ts"] + 2, row["ts"]))
                    mids = _json(pairs[0]["memory_ids"]) if pairs else None
                    ok = _ids(mids) and all(self.visible(f"memory:{i}", stack | {ref}) for i in mids)
        self.cache[ref] = bool(ok)
        return bool(ok)


def _vector(raw):
    if not isinstance(raw, bytes) or len(raw) != 512 * 4:
        return None
    vec = unpack_vec(raw)
    return vec if all(math.isfinite(x) for x in vec) and any(vec) else None


def _pick(conn, guid, ctx, now):
    floor = _play_floor(conn, now)
    if floor is None:
        return None
    usage = _rows(conn, "SELECT guid FROM callback_use WHERE ts>=? AND ts<=? LIMIT 4", (floor, now))
    if len(usage) >= 3 or any(r["guid"] == guid for r in usage):
        return None
    # Unknown room authorization fails closed, even for public memory seeds.
    if is_private(ctx.channel):
        if (not ctx.to_guid or ctx.to_guid == guid or set(ctx.present) - {guid, ctx.to_guid}
                or (ctx.readers is not None and set(ctx.readers) - {guid, ctx.to_guid})):
            return None
    elif ctx.channel != "guild" and (ctx.readers is None or guid not in ctx.readers
                                     or not set(ctx.present).issubset(ctx.readers)):
        return None
    privacy = _Privacy(conn, guid, ctx, now)
    if is_officer_channel(ctx.channel) and not privacy.officer:
        return None
    rows = _rows(conn, "SELECT * FROM memory m WHERE owner=? AND status='active' "
        "AND kind NOT IN ('plan','story') AND source NOT IN ('hearsay','offscreen') AND confidence>=0.8 "
        "AND importance>=? AND t_event BETWEEN ? AND ? AND t_created<=? "
        "AND NOT EXISTS (SELECT 1 FROM callback_use u WHERE u.memory_id=m.id AND u.ts>? AND u.ts<=?) "
        "ORDER BY importance DESC,t_event DESC,id LIMIT ?",
        (guid, MIN_SALIENCE, now - MAX_AGE, now - MIN_AGE, now, now - REUSE, now, MAX_CANDIDATES))
    query = _vector(ctx.embedding)
    picks = []
    for row in rows:
        if (row.get('key') or '').startswith('bond:') and ctx.bond_enabled is not True:
            continue
        actors = _json(row["actors"])
        if not _ids(actors) or not (set(actors) & (set(ctx.present) - {guid})):
            continue
        if not privacy.visible(f"memory:{row['id']}"):
            continue
        age = (now - row["t_event"]) / DAY
        vec = _vector(row["embedding"])
        relevance = (1 + cosine(query, vec)) / 2 if query is not None and vec is not None else 1 - age / 60
        # Stored valence is the emotional tag; boss_id marks a shared encounter.
        emotional = abs(row["valence"]) >= 0.5 or row["boss_id"] is not None
        score = .6 * relevance + .3 * row["importance"] / 10 + .1 * emotional
        reason = "similarity" if query is not None and vec is not None else "salient_recent"
        if emotional:
            reason += "_emotional"
        picks.append((score, row["t_event"], -row["id"], Callback(row["id"], row["text"], reason, age, row["scope"])))
    return max(picks, key=lambda p: p[:3])[3] if picks else None


def pick_callback(conn, guid, context, *, now) -> Callback | None:
    """Read-only offer; no migration, clock/model calls, writes, commits or logs."""
    if not _valid(context, guid, now):
        return None
    from leeroy_mind.personas.tiers import is_cast
    if not is_cast(conn,guid):
        return None
    try:
        return _pick(conn, guid, context, now)
    except sqlite3.OperationalError as exc:
        if _old_schema(exc):
            return None
        raise


def record_callback(conn, guid, context, callback: Callback, *, now) -> bool:
    """Recheck and record an accepted offer under the caller's write transaction.

    Call inside Store.transaction() with the sent-line write. No commit here.
    A refused/failed send must roll the transaction back. A concurrent writer
    may require the caller to retry its whole transaction, never just this write.
    """
    if not _valid(context, guid, now) or not isinstance(callback, Callback):
        return False
    from leeroy_mind.personas.tiers import is_cast
    if not is_cast(conn,guid):
        return False
    if not conn.in_transaction:
        raise ValueError("callback_transaction_required")
    try:
        # Obtain the write lock before the quota/evidence read; no row is changed.
        conn.execute("UPDATE callback_use SET ts=ts WHERE 0")
        chosen = _pick(conn, guid, context, now)
        if chosen is None or chosen.memory_id != callback.memory_id or chosen.seed != callback.seed:
            return False
        conn.execute("INSERT INTO callback_use(memory_id,guid,ts) VALUES(?,?,?)", (callback.memory_id, guid, now))
        return True
    except sqlite3.OperationalError as exc:
        if _old_schema(exc):
            return False
        raise
