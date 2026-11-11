"""Bounded reads of existing observations, without commits, migrations or model calls."""
from dataclasses import dataclass
import json
import math
import re
import sqlite3
import time
from types import SimpleNamespace

from leeroy_mind.bodies import ALL
from leeroy_mind.claims.store import nearest
from leeroy_mind.discord.channels import is_private, is_discord
from leeroy_mind.talk.emote_names import ROMANTIC_EMOTES
from leeroy_mind.memory.recall import scope_ok, whisper_ok
from leeroy_mind.text import estimate_tokens, strip_wow_markup

RECENT_S = 15 * 60
DAY_S = 86400
SIGNUPS_PER_EVENT = 4
PRIORITY = ("game", "context", "recent", "claims", "raid", "calendar", "standing", "bank", "drama")
HOLD_CODES = frozenset({"raid", "offline_raid", "training", "benchmark", "realm_event", "realm_day"})
_CODE = re.compile(r"[a-zA-Z0-9_-]{1,48}\Z")


@dataclass(frozen=True)
class ContextItem:
    """Caller-supplied text with its original channel and audience.

    Unknown/Discord channel names require explicit reader guids. Whisper items also
    require the original two participants and a matching private reply context.
    """
    text: str
    channel: str = "guild"
    from_guid: int = 0
    to_guid: int = 0
    readers: tuple[int, ...] | None = None


@dataclass(frozen=True)
class CardContext:
    """Inputs owned by the future conversation caller; all content slots default empty.

    channel/purpose describe the current room; readers authorizes a non-WoW room.
    to_guid is the private reply partner. thread is chronological visible context;
    reply_to is the particular message answered; pins are relevant pinned messages;
    discord_events are reactions, mentions or replies directed at this member.
    embedding is an optional already-computed 512d query vector in the claims model's
    space. No embedding is computed here. now pins expiry and recency for replay.
    """
    channel: str = ""
    purpose: str = ""
    to_guid: int = 0
    readers: tuple[int, ...] | None = None
    thread: tuple[ContextItem, ...] = ()
    reply_to: ContextItem | None = None
    pins: tuple[ContextItem, ...] = ()
    discord_events: tuple[ContextItem, ...] = ()
    embedding: bytes | None = None
    now: float | None = None
    names: dict[int,str] | None = None  # live rendering; CLI retains its diagnostic identifiers


@dataclass(frozen=True)
class CardSources:
    """A borrowed connection plus optional already-observed runtime state.

    world follows WorldView's read surface. game is a per-guid observation mapping
    for area/instance/combat facts a future bridge observer actually receives.
    bank_snapshot follows the control endpoint's cached bank reply, never a fetch.
    holds, when supplied, is a detached sequence of BodyHold-like records. None
    requests the durable reservation view; it never invokes BodyLedger callbacks.
    """
    conn: sqlite3.Connection
    world: object | None = None
    game: object | None = None
    bank_snapshot: dict | None = None
    holds: tuple | None = None
    clock: object | None = None


@dataclass(frozen=True)
class Part:
    key: str
    title: str
    lines: tuple[str, ...]

    def render(self) -> str:
        return self.title + ":\n" + "\n".join("- " + line for line in self.lines)


@dataclass(frozen=True)
class Card:
    guid: int
    parts: tuple[Part, ...]
    missing: tuple[str, ...] = ()

    def render(self, budget_tokens: int = 600) -> str:
        """Keep a priority prefix; drop entire lowest-priority parts until it fits."""
        if type(budget_tokens) is not int or budget_tokens < 0:
            raise ValueError("awareness_budget_invalid")
        texts = [part.render() for part in self.parts]
        while texts and estimate_tokens("\n\n".join(texts)) > budget_tokens:
            texts.pop()
        return "\n\n".join(texts)


def _text(value, cap=500):
    if not isinstance(value, str) or len(value) > cap:
        return ""
    clean = "".join(c for c in strip_wow_markup(value) if c.isprintable() or c.isspace())
    return " ".join(clean.split())


def _json(value, default=None):
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _ids(value):
    return tuple(sorted({v for v in value if type(v) is int and 0 < v <= 0xFFFFFFFF})) \
        if isinstance(value, (tuple, list, set, frozenset)) else ()


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _uint(value):
    return value if type(value) is int and 0 <= value <= 0xFFFFFFFF else "unknown"


def _old_schema(exc):
    return str(exc).startswith(("no such table:", "no such column:", "no such module:"))


class _Reader:
    def __init__(self, conn):
        self.conn = conn
        self.tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.missing = set()

    def rows(self, tables, sql, params=()):
        absent = set(tables) - self.tables
        if absent:
            self.missing.update(absent)
            return []
        try:
            cur = self.conn.execute(sql, params)
            names = [column[0] for column in cur.description]
            return [dict(zip(names, row)) for row in cur.fetchall()]
        except sqlite3.OperationalError as exc:
            if _old_schema(exc):
                self.missing.update(tables)
                return []
            raise


def _sources(value):
    if isinstance(value, CardSources):
        return value
    if isinstance(value, sqlite3.Connection):
        return CardSources(value)
    store = getattr(value, "store", value)
    conn = store.conn
    cached = getattr(getattr(value, "control", None), "_bank_cache", None)
    bank = cached[1] if isinstance(cached, tuple) and len(cached) == 2 else None
    return CardSources(conn, world=getattr(value, "world", None), bank_snapshot=bank,
                       clock=getattr(value, "clock", None))


def _visible(channel, frm, to, guid, officer, context, readers=None):
    if not whisper_ok(channel, frm, to, speaker=guid, turn_channel=context.channel,
                      to_guid=context.to_guid):
        return False
    if readers is not None and guid not in readers:
        return False
    if is_private(channel):
        return guid in (frm, to) and whisper_ok(channel, frm, to, speaker=guid,
            turn_channel=context.channel, to_guid=context.to_guid) and {frm, to} == {guid, context.to_guid}
    if channel == "officer":
        return officer and context.channel == "officer"
    if channel == "guild":
        return True
    # Historical party, local and Discord messages need their captured audience.
    return readers is not None and guid in readers and channel == context.channel


def _context(guid, officer, ctx):
    lines = []
    if ctx.channel and _visible(ctx.channel, guid, ctx.to_guid, guid, officer, ctx, ctx.readers):
        lines.append("Channel: " + _text(ctx.channel, 100))
        if purpose := _text(ctx.purpose):
            lines.append("Purpose: " + purpose)
    for label, items in (("Reply to", (ctx.reply_to,) if ctx.reply_to else ()),
                         ("Thread", ctx.thread[-8:]), ("Pin", ctx.pins[:4]),
                         ("Directed at you", ctx.discord_events[-4:])):
        for item in items:
            if _visible(item.channel, item.from_guid, item.to_guid, guid, officer, ctx, item.readers):
                if text := _text(item.text):
                    if ctx.names is not None and label in {"Reply to","Thread","Pin"}:
                        text = _person(ctx,item.from_guid) + ": " + text
                    lines.append(f"{label}: {text}")
    return lines


def _holds(reader, source, guid, meta):
    if source.holds is not None:
        return sorted({h.owner for h in source.holds
                       if h.owner in HOLD_CODES and (h.guids is ALL or guid in _ids(h.guids))})
    codes = set()
    for row in reader.rows(("realm_action",),
            "SELECT owner,reserved_manifest FROM realm_action WHERE role='root' "
            "AND state NOT IN ('completed','not_sent') AND transport_state<>'not_entered'"):
        if guid in _ids(_json(row["reserved_manifest"])):
            codes.add(row["owner"] if row["owner"] in HOLD_CODES else "realm")
    for key, code, field in (("tg:lab:owned", "training", "guids"),
                             ("tg:benchmark:active", "benchmark", "members")):
        value = _json(meta.get(key))
        if isinstance(value, dict) and guid in _ids(value.get(field)):
            if key != "tg:lab:owned" or value.get("phase") != "complete":
                codes.add(code)
    return sorted(codes)


def _person(ctx,guid):
    return ctx.names.get(guid,"someone") if ctx.names is not None else f"#{guid}"


def _when(ctx,ts,now):
    if ctx.names is None:
        return f"at {ts:g}"
    age = abs(now-ts)
    if age < 60:
        return "just now" if ts<=now else "in under a minute"
    scale,label = (86400,"day") if age>=86400 else (3600,"hour") if age>=3600 else (60,"min")
    count = int(age//scale)
    text = f"{count} {label}" + ("s" if label!="min" and count!=1 else "")
    return text+" ago" if ts<=now else "in "+text


def _game(reader, source, guid, meta, now, ctx):
    roster = reader.rows(("cast_member",), "SELECT * FROM cast_member WHERE guid=?", (guid,))
    observed = dict(roster[0]) if roster else {}
    world = source.world
    member = getattr(world, "members", {}).get(guid)
    if member is not None:
        observed.update(online=guid in world.online, map=member.map, zone=member.zone)
    current = member is not None
    raw = reader.rows(("game_event",),
        "SELECT id,ts,type,payload FROM game_event WHERE ts BETWEEN ? AND ? AND json_valid(payload) "
        "AND (guid=? OR type IN ('group','hello') OR "
        "(type='text_emote' AND json_extract(payload,'$.target_guid')=?) OR "
        "(type='boss' AND EXISTS (SELECT 1 FROM json_each(payload,'$.members') WHERE value=?))) "
        "ORDER BY ts DESC,id DESC LIMIT 80", (now-RECENT_S, now, guid, guid, guid))
    events = [(r, _json(r["payload"], {})) for r in raw]
    events = [(r, p) for r, p in events if isinstance(p, dict)]
    if not current:
        event = next(((r, p) for r, p in events if r["type"] == "zone" and p.get("guid") == guid), None)
        if event and event[0]["ts"] >= observed.get("updated", 0):
            observed["zone"] = event[1].get("zone_id", observed.get("zone", "unknown"))
        presence = next(((r, p) for r, p in events if r["type"] in ("login", "logout")
                         and p.get("guid") == guid), None)
        if presence and presence[0]["ts"] >= observed.get("updated", 0):
            observed["online"] = presence[0]["type"] == "login"
    group = getattr(world, "group", None)
    if world is not None:
        members = _ids((*getattr(group, "members", ()), getattr(group, "leader_guid", 0)))
        raid = bool(getattr(group, "raid", False))
    else:
        latest = next((p for r, p in events if r["type"] in ("group", "hello")), {})
        members = _ids((*latest.get("members", ()), latest.get("leader_guid", 0))) \
            if isinstance(latest.get("members", ()), (list, tuple)) else ()
        raid = latest.get("raid") is True
    human = int(meta.get("human_guid", "0")) if str(meta.get("human_guid", "0")).isdecimal() else 0
    if type(getattr(world, "human_guid", None)) is int and world.human_guid > 0:
        human = world.human_guid
    extra = (source.game or {}).get(guid, {})
    extra = extra if isinstance(extra, dict) else {}
    area = extra.get("area") if type(extra.get("area")) is int and extra["area"] >= 0 else "unknown"
    instance = extra.get("instance_id") if type(extra.get("instance_id")) is int and extra["instance_id"] >= 0 else "unknown"
    instance_observed = False
    if instance == "unknown":
        encounter = next((p for r, p in events if r["type"] == "boss"
                          and guid in _ids(p.get("members")) and p.get("map") == observed.get("map")), None)
        if encounter and type(encounter.get("instance_id")) is int and encounter["instance_id"] > 0:
            instance, instance_observed = encounter["instance_id"], True
    online = observed.get("online")
    for key in ("map", "zone"):
        if type(observed.get(key)) is not int or observed[key] < 0:
            observed[key] = "unknown"
    state = "online" if online in (1, True) else "offline" if online in (0, False) else "unknown"
    lines = [f"Member #{guid}: {state}" + (" (current observer)" if current else " (last stored observation)"),
             f"Map: {observed.get('map', 'unknown')}; zone: {observed.get('zone', 'unknown')}; "
             f"area: {area}; instance: {instance}" + (" (last encounter)" if instance_observed else "")]
    if ctx.names is not None:
        from leeroy_mind.wow import RAID_INSTANCES,DUNGEONS
        maps = {0:"Eastern Kingdoms",1:"Kalimdor",530:"Outland",571:"Northrend",**RAID_INSTANCES,**DUNGEONS}
        zones = reader.rows(("game_event",),"SELECT payload FROM game_event WHERE type='zone' AND guid=? "
            "AND ts BETWEEN ? AND ? ORDER BY ts DESC,id DESC LIMIT 16",(guid,now-7*DAY_S,now))
        place = next((_text(p.get("zone_name")) for r in zones if isinstance(p:=_json(r["payload"]),dict)
                      and p.get("zone_id")==observed.get("zone") and _text(p.get("zone_name"))),"unknown zone")
        lines = [f"{_person(ctx,guid)}: {state}", f"Location: {place}, {maps.get(observed.get('map'),'unknown map')}"]
    combat = extra.get("combat")
    lines.append("Combat: " + ("in combat" if combat is True else "rest" if combat is False else "unknown"))
    if guid in members and human in members:
        lines.append(("His raid" if raid else "His group") + ": " + ", ".join(_person(ctx,g) for g in members))
    else:
        lines.append("His group: " + ("not in it" if world is not None or members else "unknown"))
    codes = _holds(reader, source, guid, meta)
    lines.append("Holds: " + (", ".join(codes) if codes else "none recorded"))
    recent = []
    for row, payload in events:
        kind = row["type"]
        if kind == "text_emote" and payload.get("target_guid") == guid:
            code = payload.get("emote", "")
            private_pair = ctx.channel=="whisper" and ctx.to_guid==payload.get("guid")
            if (isinstance(code,str) and _CODE.fullmatch(code) and
                    (private_pair or ctx.channel=="say" and code not in ROMANTIC_EMOTES)):
                recent.append(f"Emote {code} from {_person(ctx,_uint(payload.get('guid')))} {_when(ctx,row['ts'],now)}")
        elif kind == "loot_won" and payload.get("guid") == guid:
            item = _text(payload.get("item_name")) or "an item" if ctx.names is not None else f"item #{_uint(payload.get('item_id'))}"
            recent.append(f"Received {item} {_when(ctx,row['ts'],now)}")
        elif kind == "death" and payload.get("guid") == guid:
            recent.append(f"Died {_when(ctx,row['ts'],now)}")
        elif kind == "boss" and guid in _ids(payload.get("members")) and payload.get("state") == "killed":
            boss = (_text(payload.get("boss_name")) or "a boss") if ctx.names is not None else f"map #{_uint(payload.get('map'))}, boss #{_uint(payload.get('boss_id'))}"
            recent.append(f"Boss kill: {boss} {_when(ctx,row['ts'],now)}")
        if len(recent) == 5:
            break
    return lines, recent, human


def _claims(reader, guid, officer, ctx, now):
    if "member_claim" not in reader.tables:
        reader.missing.add("member_claim")
        return []
    candidates = None
    if ctx.embedding is not None and "member_claim_vec" in reader.tables:
        try:
            candidates = [claim for claim, _ in nearest(reader.conn, guid, ctx.embedding, k=8, now=now)]
        except sqlite3.OperationalError as exc:
            if not _old_schema(exc):
                raise
            reader.missing.add("member_claim_vec")
    if candidates is None:
        candidates = [SimpleNamespace(**r) for r in reader.rows(("member_claim",),
            "SELECT id,kind,subject,text,source,source_line FROM member_claim WHERE guid=? "
            "AND first_seen<=? AND superseded_by IS NULL AND (expires IS NULL OR expires>?) "
            "ORDER BY last_seen DESC,id DESC LIMIT 8", (guid, now, now))]
    ids = [c.source_line for c in candidates if c.source_line is not None]
    provenance = {r["id"]: r for r in reader.rows(("chat_line",),
        "SELECT id,channel,from_guid,to_guid,audience FROM chat_line "
        "WHERE id IN (SELECT value FROM json_each(?)) AND ts<=?",
        (json.dumps(ids), now))} if ids else {}
    out = []
    for claim in candidates:
        if getattr(claim, "first_seen", now) > now:
            continue
        line = provenance.get(claim.source_line)
        # Owner corrections inherit their cited line's visibility. Unscoped corrections
        # have no proof that this conversation may disclose them.
        if (line is None or claim.source == "chat" and line["from_guid"] != guid or not _visible(
                line["channel"], line["from_guid"], line["to_guid"], guid, officer, ctx,
                tuple(_ids(_json(line["audience"]))) if line["audience"] is not None else None)):
            continue
        if text := _text(claim.text):
            out.append(f"{claim.kind}: {text}")
    return out


def _drama(reader, guid, officer, ctx, now):
    out = []
    rows = reader.rows(("feud",),
            "SELECT id,a,b,started,ended,evidence FROM feud WHERE (a=? OR b=?) "
            "AND (ended IS NULL OR ended>=?) ORDER BY started DESC,id DESC LIMIT 4",
            (guid, guid, now-7*DAY_S))
    evidence = {row["id"]: _json(row["evidence"], []) for row in rows}
    valid = {key: refs for key, refs in evidence.items() if isinstance(refs, list) and 0 < len(refs) <= 16
             and all(isinstance(ref, str) and re.fullmatch(r"(?:chat|encounter):[1-9][0-9]*", ref)
                     for ref in refs)}
    ids = {kind: sorted({int(ref.split(":")[1]) for refs in valid.values() for ref in refs
                        if ref.startswith(kind + ":")}) for kind in ("chat", "encounter")}
    lines = {r["id"]: r for r in reader.rows(("chat_line",),
            "SELECT id,channel,from_guid,to_guid FROM chat_line WHERE id IN (SELECT value FROM json_each(?))",
            (json.dumps(ids["chat"]),))} if ids["chat"] else {}
    scenes = {r["id"]: r for r in reader.rows(("encounter",),
        "SELECT id,a,b,private FROM encounter WHERE id IN (SELECT value FROM json_each(?))",
        (json.dumps(ids["encounter"]),))} if ids["encounter"] else {}
    # Match the knowledge ledger's secret-fact rule without invoking its Store
    # transaction wrappers or walking unbounded provenance graphs.
    roots = [f"feud:{r['id']}" for r in rows] + [f"encounter:{i}" for i in ids["encounter"]]
    secrets = {r["root"] for r in reader.rows(("fact",),
        "SELECT DISTINCT root FROM fact WHERE secret=1 AND root IN (SELECT value FROM json_each(?))",
        (json.dumps(roots),))} if roots and "fact" in reader.tables else set()
    if "fact" in reader.missing:
        return []
    def visible(ref):
        kind, number = ref.split(":")
        row = (lines if kind == "chat" else scenes).get(int(number))
        if row is None:
            return False
        if kind == "chat":
            return _visible(row["channel"], row["from_guid"], row["to_guid"], guid, officer, ctx)
        if is_discord(ctx.channel):
            return False  # encounters have no Discord provenance in D1
        if guid not in (row["a"], row["b"]) or row["private"] not in (0, 1):
            return False
        partner = row["b"] if row["a"] == guid else row["a"]
        return scope_ok(SimpleNamespace(owner=guid, scope="private" if row["private"] or ref in secrets else "open",
                                       scope_with=partner), turn_channel=ctx.channel, to_guid=ctx.to_guid)
    for row in rows:
        if row["id"] not in valid or not all(visible(ref) for ref in valid[row["id"]]):
            continue
        other = row["b"] if row["a"] == guid else row["a"]
        if f"feud:{row['id']}" in secrets and not scope_ok(
                SimpleNamespace(owner=guid, scope="secret", scope_with=other),
                turn_channel=ctx.channel, to_guid=ctx.to_guid):
            continue
        label = "Disagreement" if ctx.names is not None else f"Feud #{row['id']}"
        out.append(f"{label} with {_person(ctx,other)}: " + ("active" if row["ended"] is None else "ended"))
    return out


def card(app_or_stores, guid: int, *, context: CardContext) -> Card:
    """Read one member's card. The caller owns the connection and its snapshot/transaction."""
    if type(guid) is not int or not 0 < guid <= 0xFFFFFFFF:
        raise ValueError("awareness_guid_invalid")
    source = _sources(app_or_stores)
    now = context.now if context.now is not None else source.clock.now() if source.clock else time.time()
    if not _number(now):
        raise ValueError("awareness_time_invalid")
    reader = _Reader(source.conn)
    tier = ("COALESCE((SELECT tier FROM persona_tier WHERE guid=p.guid),'cast')"
            if "persona_tier" in reader.tables else "'cast'")
    roles = reader.rows(("persona_core",), "SELECT CASE WHEN json_valid(json) THEN "
        "json_extract(json,'$.play.guild_role') END AS role," + tier + " AS tier "
        "FROM persona_core p WHERE guid=?", (guid,))
    if roles and roles[0]["tier"] == "world":
        return Card(guid,(),("world_excluded",))
    meta = {r["key"]: r["value"] for r in reader.rows(("meta",),
        "SELECT key,value FROM meta WHERE key IN ('human_guid','tg:lab:owned','tg:benchmark:active')")}
    officer = bool(roles and roles[0]["role"] == "officer")
    if is_discord(context.channel):
        parts = (("context","Conversation",_context(guid,officer,context)),
                 ("claims","Your claims",_claims(reader,guid,officer,context,now)),
                 ("drama","Your recent guild drama",_drama(reader,guid,officer,context,now)))
        return Card(guid,tuple(Part(k,title,tuple(lines)) for k,title,lines in parts if lines),tuple(sorted(reader.missing)))
    game, recent, human = _game(reader, source, guid, meta, now, context)
    if context.channel == "whisper" and context.to_guid == human and human:
        for row in reader.rows(("chat_line",), "SELECT text FROM chat_line WHERE channel='whisper' "
                "AND from_guid=? AND to_guid=? AND ts BETWEEN ? AND ? ORDER BY ts DESC,id DESC LIMIT 3",
                (human, guid, now-RECENT_S, now)):
            if text := _text(row["text"]):
                recent.append("His whisper: " + text)
    parts = {"game": Part("game", "In game", tuple(game))}
    def add(key, title, lines):
        if lines:
            parts[key] = Part(key, title, tuple(lines))
    add("context", "Conversation", _context(guid, officer, context))
    add("recent", "Recent events", recent)
    add("claims", "Your claims", _claims(reader, guid, officer, context, now))
    raids = reader.rows(("guild_raid",), "SELECT raid_id,raid_key,started,ended,kills,wipes FROM guild_raid "
                        "WHERE started<=? ORDER BY (ended IS NULL) DESC,started DESC,raid_id DESC LIMIT 1", (now,))
    add("raid", "Guild raid", [f"{'Recorded active' if r['ended'] is None else 'Last'} raid" +
        (f" #{r['raid_id']} " if context.names is None else " ") +
        f"({_text(r['raid_key'], 80)}): {r['kills']} kills, {r['wipes']} wipes" for r in raids])
    signup = ",coalesce((SELECT json_group_array(json_array(guid,status)) FROM " \
        f"(SELECT guid,status FROM realm_rsvp WHERE event=c.event_id ORDER BY guid LIMIT {SIGNUPS_PER_EVENT})), '[]') AS signups" \
        if "realm_rsvp" in reader.tables else ",'[]' AS signups"
    calendar = reader.rows(("realm_calendar",), "WITH upcoming AS (SELECT * FROM realm_calendar "
        "WHERE state='posted' AND at>=?), selected AS (SELECT id FROM "
        "(SELECT id FROM upcoming WHERE at<=? ORDER BY at,id LIMIT 4) UNION SELECT "
        "(SELECT id FROM upcoming WHERE kind='raid' ORDER BY at,id LIMIT 1)) "
        "SELECT c.id,c.kind,c.title,c.at" + signup +
        " FROM upcoming c WHERE c.id IN (SELECT id FROM selected) ORDER BY c.at,c.id",
        (now, now+14*DAY_S))
    next_raid = next((r['id'] for r in calendar if r['kind'] == 'raid'), None)
    add("calendar", "Upcoming calendar", [f"{'Next raid' if r['id']==next_raid else 'Event'}" +
        (f" #{r['id']} " if context.names is None else " ") +
        f"{_when(context,r['at'],now)}: {_text(r['title'])}; signups (up to {SIGNUPS_PER_EVENT}) " + ", ".join(
            f"{_person(context,g)} {_text(status, 40)}" for g, status in _json(r['signups'], [])) for r in calendar])
    standing = []
    dkp = reader.rows(("dkp_ledger",), "WITH b AS (SELECT guid,sum(delta) AS balance FROM dkp_ledger "
        "GROUP BY guid) SELECT balance,1+(SELECT count(*) FROM b other WHERE other.balance>b.balance) "
        "AS place FROM b WHERE guid=?", (guid,))
    if dkp:
        standing.append(f"DKP: {dkp[0]['balance']:g}; rank {dkp[0]['place']}")
    attendance = reader.rows(("guild_attendance",), "SELECT status,count(*) AS n FROM guild_attendance "
                             "WHERE guid=? GROUP BY status ORDER BY status", (guid,))
    standing.extend(f"Attendance: {r['n']} {r['status']}" for r in attendance)
    add("standing", "Your standing", standing)
    bank = []
    snapshot = source.bank_snapshot
    if isinstance(snapshot, dict) and type(snapshot.get("money")) is int and snapshot["money"] >= 0 \
            and _number(snapshot.get("read_at")) and 0 <= now-snapshot["read_at"] <= RECENT_S:
        bank.append(f"Balance: {snapshot['money']} copper, observed {_when(context,snapshot['read_at'],now)}")
    bank.extend(f"Recorded {r['dir']} movements in the last week: {r['n']}" for r in reader.rows(
        ("realm_bank",), "SELECT dir,count(*) AS n FROM realm_bank WHERE ts BETWEEN ? AND ? "
        "GROUP BY dir ORDER BY dir", (now-7*DAY_S, now)))
    if bank and not any(line.startswith("Balance:") for line in bank):
        bank.insert(0, "Current balance: unknown")
    add("bank", "Guild bank", bank)
    add("drama", "Your recent guild drama", _drama(reader, guid, officer, context, now))
    return Card(guid, tuple(parts[key] for key in PRIORITY if key in parts), tuple(sorted(reader.missing)))
