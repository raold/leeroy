"""Bounded public evidence. Private chats and ungrounded trait claims never enter topics."""
from dataclasses import dataclass
import json

from leeroy_mind.discord.channels import same_medium
from leeroy_mind.nightly import lexicon
from leeroy_mind.protocol import Boss, Death, LootDrop, LootWon
from leeroy_mind.text import truncate_words

FRESH = 900


@dataclass(frozen=True)
class Topic:
    key: str
    kind: str
    at: float
    evidence: str
    target: int = 0
    tease_owner: bool = False
    source_channel: str = ""  # empty: a world event, observable in either medium


def event_topic(app, ev):
    """Only events observed in this session, relevant to the owner or his group."""
    if app.is_backlog(ev):
        return None
    owner, now = app.human_guid, app.clock.now()
    group = app.world.group
    members = set(group.members) | {group.leader_guid} if group else {owner}
    kind, target, tease, evidence = "", 0, False, ""
    if isinstance(ev, Death) and ev.guid == owner:
        kind, target, tease = "death", owner, True
        evidence = "The owner died in game just now. The record gives no cause or blame."
    elif isinstance(ev, Boss) and ev.state == "wiped" and owner in members and set(ev.members) & members:
        kind = "wipe"
        evidence = "The group just wiped on encounter " + str(ev.boss_id) + ". No blame was recorded."
    elif isinstance(ev, LootWon) and ev.guid in members:
        kind, target, tease = "loot", ev.guid, ev.guid == owner
        evidence = "Member " + str(ev.guid) + " received item " + str(ev.item_id) + ". No luck or habit is established."
    elif isinstance(ev, LootDrop) and group and owner in members and any(
            m.guid in members and m.map == ev.map for m in app.world.members.values()):
        kind = "loot"
        evidence = "Items dropped in the group's instance: " + ", ".join(str(i.item_id) for i in ev.items[:8]) + "."
    if not kind:
        return None
    row = app.store.conn.execute("SELECT id FROM game_event WHERE type=? ORDER BY id DESC LIMIT 1",
        ({Death:"death",Boss:"boss",LootWon:"loot_won",LootDrop:"loot_drop"}[type(ev)],)).fetchone()
    if row is None:
        return None
    return Topic(f"event:{row[0]}",kind,now,evidence,target,tease)


def calendar_topics(app, now):
    for row in app.store.conn.execute("SELECT id,at,ref FROM realm_calendar WHERE kind='birthday' "
            "AND state='posted' AND at BETWEEN ? AND ? ORDER BY at,id LIMIT 8", (now-FRESH,now)):
        parts = row["ref"].split(":")
        guid = int(parts[1]) if len(parts)==3 and parts[1].isdigit() else 0
        if guid and app.personas.get(guid) is None:
            continue
        evidence = f"The guild calendar records a birthday for member {guid}." if guid else (
            "The guild calendar records shared guild birthdays today.")
        yield Topic(f"calendar:{row['id']}","birthday",row["at"],evidence,guid)


def rival_topics(app, now):
    # Only confirmed public Trade output, never a rival's whisper or private night sketch.
    for row in app.store.conn.execute("SELECT l.id,l.ts,l.text FROM realm_line l "
            "JOIN realm_actor a ON a.guid=l.guid WHERE l.channel='trade' AND l.state='sent' AND l.dropped IS NULL "
            "AND l.ts BETWEEN ? AND ? ORDER BY l.id DESC LIMIT 8", (now-FRESH,now)):
        yield Topic(f"rival:{row['id']}","rival",row["ts"],
            "A rival said in Trade (a quote, not verified fact): " + json.dumps(truncate_words(row["text"],400)),
            source_channel="trade")


def jokes(app, channel, now):
    for row in app.store.conn.execute("SELECT id,term,origin_ref FROM lexicon WHERE kind='joke' "
            "AND status='active' ORDER BY last_used,id LIMIT 16"):
        ref = row["origin_ref"]
        if not ref.startswith("chat:") or not ref[5:].isdigit():
            continue
        line = lexicon.public_line(app.store,int(ref[5:]))
        if (line is None or line["channel"] not in {"guild","discord:general"}
                or not same_medium(line["channel"],channel)
                or lexicon.public_sources(app.store,(f"joke:{row['id']}",)) is None):
            continue
        # A running gag proves that a line was said, not that its claim is true.
        yield Topic(f"joke:{row['id']}","joke",now,
            "Recorded running joke: " + json.dumps(row["term"]) + ". Its actual origin line: " +
            json.dumps(truncate_words(line["text"],400)) + ". This establishes no personal trait.",
            source_channel=line["channel"])
