"""Current, bounded evidence for intentions. Reading a row never widens its audience."""
from datetime import datetime
import json
import re

from leeroy_mind.discord import layout
from leeroy_mind.culture.origins import _item_name, _safe_name
from leeroy_mind.discord.channels import is_discord, is_group, is_private, is_restricted
from leeroy_mind.discord.presence import local_time, OWNER_ZONE
from leeroy_mind.groups.audience import visible
from leeroy_mind.memory.privacy import GROUP
from leeroy_mind.nightly.human_world import _provenance
from leeroy_mind.personas.io import load_persona
from leeroy_mind.personas.tiers import is_cast
from leeroy_mind.storage.store import clean
from leeroy_mind.talk.romance import forward
from leeroy_mind.text import strip_wow_markup
from leeroy_mind.wow import boss_name

DAY = 86400.0
MIN_AGE = 25
BIRTHDAY_ABOUT = "Today is your birthday; mention it to him naturally."
MISS_YOU_ABOUT = "You've missed chatting with him and want to catch up, warmly and without pressure."
_NUMBER = re.compile(r"[1-9][0-9]*")
_PERSISTED = re.compile(r"(?:memory|chat|event|session|story|chronicle|encounter|beat|life|away|feud|mentor|award|joke|rule:fact):[1-9][0-9]*")


def about(text):
    return clean(" ".join(strip_wow_markup(text).split()))[:200]


def member_local(local, persona):
    return local_time(local if local.tzinfo else local.replace(tzinfo=OWNER_ZONE),
                      persona.identity.region)


def _id(ref, kind):
    prefix = kind + ":"
    tail = ref[len(prefix):] if isinstance(ref, str) and ref.startswith(prefix) else ""
    return int(tail) if _NUMBER.fullmatch(tail) and len(tail) <= 18 else None


def _readers(raw):
    if raw is None:
        return None
    values = json.loads(raw)
    if not isinstance(values, list) or any(type(g) is not int or g <= 0 for g in values):
        raise ValueError("source_audience")
    return frozenset(values)


def _chat_pair(store, line, guid, human):
    channel = line["channel"]
    readers = _readers(line["audience"])
    if is_discord(channel):
        # A deleted Discord post's chat mirror is retained, but is no longer evidence.
        receipt = store.conn.execute("SELECT d.deleted,d.text FROM meta m JOIN discord_message d "
            "ON m.key='discord:line:'||d.id WHERE m.value=? AND d.author=? AND d.channel=?",
            (str(line["id"]), line["from_guid"], channel.removeprefix("discord:"))).fetchone()
        if receipt is None or receipt[0] is not None or receipt[1] != line["text"]:
            return False
    pair = {guid, human}
    if is_private(channel):
        return ({line["from_guid"], line["to_guid"]} == pair
                and (layout.dm_peer(channel) in (None, guid))
                and (readers is None or guid in readers or line["from_guid"] == guid))
    if is_group(channel):
        return visible(readers, f"dm:{guid}", speaker=guid, to_guid=human)
    if is_restricted(channel):
        return False
    if channel == "guild" or is_discord(channel):
        return line["from_guid"] == guid or readers is not None and guid in readers
    # A local game conversation is evidence for this pair only when both were there.
    return channel in {"party", "raid", "raid_warning", "say", "yell", "emote"} and (
        readers is not None and pair <= readers | {line["from_guid"]})


def pair_provenance(store, guid, refs, *, human):
    """Full recall/retelling ancestry, at most 200 nodes, with every boundary checked.

    _provenance checks existence, cycles, sent-output recall and retained joke origins.
    Its generic rule roots are deliberately excluded here: an unbacked label cannot
    authorize sharing. Group evidence needs the original captured readers at every leaf.
    """
    try:
        nodes = _provenance(store, refs)
        if not nodes or any(not _PERSISTED.fullmatch(ref) for ref in nodes):
            return False
        group_scope, group_line = False, False
        pair = {guid, human}
        for ref, (_, _, holder, scope, parties) in nodes.items():
            if ref.startswith("chat:"):
                line = store.conn.execute("SELECT * FROM chat_line WHERE id=?", (int(ref[5:]),)).fetchone()
                if line is None or not _chat_pair(store, line, guid, human):
                    return False
                group_line |= is_group(line["channel"])
                continue
            if scope[0] == "open":
                continue
            if holder is not None:
                if scope == ("private", GROUP):
                    group_scope = True
                elif scope[1] <= 0 or {holder, scope[1]} != pair:
                    return False
            elif not pair <= parties:
                return False
        return not group_scope or group_line
    except (ValueError, TypeError, KeyError, RecursionError):
        return False


def beat_memory(store, guid, bid):
    row = store.conn.execute("SELECT id FROM memory WHERE owner=? AND source='offscreen' AND status='active' "
        "AND superseded_by IS NULL AND EXISTS (SELECT 1 FROM json_each(memory.evidence) WHERE value=?) "
        "ORDER BY id LIMIT 1", (guid, f"beat:{bid}")).fetchone()
    return store.get_memories([row[0]])[0] if row else None


def follow_memory(store, guid, ref):
    row = store.conn.execute("SELECT id FROM memory WHERE owner=? AND key=? AND status='active' "
                             "AND superseded_by IS NULL ORDER BY id DESC LIMIT 1", (guid, ref)).fetchone()
    return store.get_memories([row[0]])[0] if row else None


def his_day(store, eid, guid, human, *, now=None, library=None, ident=None):
    row = store.conn.execute("SELECT * FROM game_event WHERE id=?", (eid,)).fetchone()
    if row is None or now is not None and row["ts"] > now:
        return None
    try:
        data = json.loads(row["payload"])
        members = data.get("members")
        if row["type"] == "loot_won" and "members" not in data and row["session_id"] is not None:
            # Native LootWon has no audience. Its prior captured group in this
            # session supplies witnesses; a later/current roster never does.
            group = store.conn.execute("SELECT payload FROM game_event WHERE type='group' AND session_id=? "
                "AND (ts<? OR (ts=? AND id<?)) ORDER BY ts DESC,id DESC LIMIT 1",
                (row["session_id"], row["ts"], row["ts"], row["id"])).fetchone()
            members = json.loads(group[0]).get("members") if group else None
        if (not isinstance(members, list) or any(type(g) is not int or g <= 0 for g in members)
                or guid not in members or human not in members):
            return None
        if (row["type"] == "boss" and data.get("state") == "killed"
                and type(data.get("map")) is int and type(data.get("boss_id")) is int):
            # Only the repository boss catalogue supplies a name, never receipt prose.
            boss = boss_name(data['map'], data['boss_id'])
            boss = _safe_name(boss, ident=ident) if boss != "the boss" else None
            return about(f"You were there when he killed {boss or 'a boss'}.")
        if row["type"] == "loot_won" and row["guid"] == human and type(data.get("item_id")) is int:
            name = _item_name(store.conn, data,
                              library=library if hasattr(library, "item_info") else None, ident=ident)
            return f"You were there when he won [{name}]."
    except (ValueError, TypeError, AttributeError):
        pass
    return None


def quiet_pair(store, guid, human, *, now, delivered_line=None):
    # A receipt runs after its own delivered line is inserted. Exclude exactly that
    # line, not the timestamp: another medium can have real intervening contact.
    game = store.conn.execute("SELECT 1 FROM chat_line WHERE channel='whisper' AND ts>? AND ts<=? "
        "AND id!=? AND ((from_guid=? AND to_guid=?) OR (from_guid=? AND to_guid=?)) LIMIT 1",
        (now - 2 * DAY, now, delivered_line or 0, guid, human, human, guid)).fetchone()
    dm = store.conn.execute("SELECT 1 FROM discord_message d WHERE channel=? AND author IN (?,?) "
        "AND deleted IS NULL AND ts>? AND ts<=? AND NOT EXISTS "
        "(SELECT 1 FROM meta WHERE key='discord:line:'||d.id AND value=?) LIMIT 1",
        (f"dm:{guid}", guid, human, now - 2 * DAY, now, str(delivered_line or 0))).fetchone()
    return game is None and dm is None


def source_alive(store, item, *, now, human, local=None, persona=None, delivered_line=None,
                 ident=None, library=None):
    """Source eligibility, not a receipt or expiry mutation. Unknown source kinds fail closed."""
    guid = item.guid
    if (guid == human or human <= 0 or not is_cast(store.conn, guid)
            or store.conn.execute("SELECT 1 FROM cast_member WHERE guid=?", (guid,)).fetchone() is None):
        return False
    try:
        p = persona if persona is not None else load_persona(store, guid)
        if p is None or p.guid != guid:
            return False
        expected = "guild" if item.kind in ("his_day", "birthday") else "pair"
        if item.audience != expected:
            return False
        if item.kind == "life":
            bid = _id(item.ref, "life")
            row = store.conn.execute("SELECT b.text,t.guid FROM life_beat b JOIN life_thread t "
                                     "ON t.id=b.thread_id WHERE b.id=?", (bid,)).fetchone()
            memory = beat_memory(store, guid, bid) if bid else None
            return bool(row and row[1] == guid and memory and about(row[0]) == item.about == about(memory.text)
                        and pair_provenance(store, guid, [f"memory:{memory.id}"], human=human))
        if item.kind == "plan":
            cid = _id(item.ref, "plan")
            row = store.conn.execute("SELECT c.*,l.from_guid FROM member_claim c JOIN chat_line l "
                "ON l.id=c.source_line WHERE c.id=?", (cid,)).fetchone()
            return bool(row and row["guid"] == row["from_guid"] == guid and row["kind"] == "plan"
                        and row["superseded_by"] is None and row["last_seen"] <= now
                        and (row["expires"] is None or row["expires"] > now)
                        and about(row["text"]) == item.about
                        and pair_provenance(store, guid, [f"chat:{row['source_line']}"], human=human))
        if item.kind == "followup":
            root = item.ref.removeprefix("followup:")
            rid = _id(root, "chat")
            memory = follow_memory(store, guid, item.ref) if item.ref.startswith("followup:") and rid else None
            line = store.conn.execute("SELECT from_guid,origin FROM chat_line WHERE id=?", (rid,)).fetchone()
            nodes = _provenance(store, [f"memory:{memory.id}"]) if memory else None
            return bool(memory and memory.t_created <= now and about(memory.text) == item.about
                        and line and tuple(line) == (human, "human") and nodes and root in nodes
                        and pair_provenance(store, guid, [f"memory:{memory.id}"], human=human))
        if item.kind == "his_day":
            return his_day(store, _id(item.ref, "his_day"), guid, human, now=now,
                           library=library, ident=ident) == item.about
        if item.kind == "birthday":
            at = member_local(local or store.clock.local(), p)
            return (item.ref == f"birthday:{at.date()}" and p.real_life.birthday == at.strftime("%m-%d")
                    and item.about == BIRTHDAY_ABOUT)
        if item.kind == "miss_you":
            datetime.strptime(item.ref.removeprefix("miss_you:"), "%Y-%m-%d")
            return (item.ref.startswith("miss_you:") and item.about == MISS_YOU_ABOUT
                    and p.identity.age >= MIN_AGE and p.identity.player_gender == "female"
                    and p.romance.stance in ("drawn", "lust") and forward(p) > 0
                    and quiet_pair(store, guid, human, now=now, delivered_line=delivered_line))
        if item.kind == "dm_nudge":
            mid = _id(item.ref, "dm_nudge")
            row = store.conn.execute("SELECT id,author,origin,ts,deleted FROM discord_message WHERE channel=? "
                "ORDER BY id DESC LIMIT 1", (f"dm:{guid}",)).fetchone()
            return bool(row and row[0] == mid and row[1] == guid and row[2] == "mind"
                        and now - 3 * DAY < row[3] <= now and row[4] is None)
    except (ValueError, TypeError, KeyError, RecursionError):
        return False
    return False


def source_embedding(store, item):
    """Reuse a source vector after source_alive; never compute or copy a new embedding."""
    if item.kind == "life":
        bid = _id(item.ref, "life")
        memory = beat_memory(store, item.guid, bid) if bid else None
    elif item.kind == "followup":
        memory = follow_memory(store, item.guid, item.ref)
    elif item.kind == "plan":
        row = store.conn.execute("SELECT text,embedding FROM member_claim WHERE id=? AND guid=? "
            "AND superseded_by IS NULL", (_id(item.ref, "plan"), item.guid)).fetchone()
        return row[1] if row and about(row[0]) == item.about else None
    else:
        return None
    return memory.embedding if memory and about(memory.text) == item.about else None
