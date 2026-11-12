"""Bounded public evidence. Private chats and ungrounded trait claims never enter topics."""
from dataclasses import dataclass
import json

from leeroy_mind.discord.channels import is_discord
from leeroy_mind.nightly import lexicon
from leeroy_mind.protocol import Boss, Death, LootDrop, LootWon
from leeroy_mind.text import truncate_words
from leeroy_mind.wow import boss_name

FRESH = 900
JOKE_COOLDOWN = 3*86400


def name(app,guid):
    if guid == app.human_guid:
        return app.engine.human.called
    persona=app.personas.get(guid)
    return persona.character_name if persona else 'a guild member'


def item_name(app,ident,given=''):
    library=getattr(app,'library',None)
    if library is not None and (row := library.item_info((ident,)).get(ident)):
        return row['name']
    return given.strip() or 'an item'


def joke_uses(store,key):
    try:
        value=json.loads(store.get_meta('banter:joke-use:'+key) or '{}')
        at,count=float(value.get('at',0)),int(value.get('count',0))
        return at,max(0,count)
    except (TypeError,ValueError,AttributeError):
        return float('inf'),4  # damaged usage never resets its allowance


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
        evidence = name(app,owner) + " died in game just now. The record gives no cause or blame."
    elif isinstance(ev, Boss) and ev.state == "wiped" and owner in members and set(ev.members) & members:
        kind = "wipe"
        evidence = "The group just wiped on " + boss_name(ev.map,ev.boss_id,ev.boss_name) + ". No blame was recorded."
    elif isinstance(ev, LootWon) and ev.guid in members:
        kind, target, tease = "loot", ev.guid, ev.guid == owner
        evidence = name(app,ev.guid) + " received " + item_name(app,ev.item_id,ev.name) + ". No luck or habit is established."
    elif isinstance(ev, LootDrop) and group and owner in members and any(
            m.guid in members and m.map == ev.map for m in app.world.members.values()):
        kind = "loot"
        evidence = "Items dropped in the group's instance: " + ", ".join(item_name(app,i.item_id,i.name) for i in ev.items[:8]) + "."
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
        evidence = f"The guild calendar records a birthday for {name(app,guid)}." if guid else (
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
    # Filter medium and exact existing origin before paging. A rejected provenance
    # candidate does not occupy one of the sixteen eligible slots.
    medium='discord:general' if is_discord(channel) else 'guild'
    cursors=getattr(app,'_banter_joke_cursors',None)
    if cursors is None:
        cursors={}; app._banter_joke_cursors=cursors
    start=cursors.get(medium,0)
    found=0
    for offset in range(start,start+512,16):
        rows=app.store.conn.execute("SELECT l.id,l.term,l.origin_ref FROM lexicon l "
            "JOIN chat_line c ON c.id=CAST(substr(l.origin_ref,6) AS INTEGER) WHERE l.kind='joke' "
            "AND l.origin_ref='chat:'||c.id AND l.status='active' AND c.channel=? "
            "ORDER BY l.last_used,l.id LIMIT 16 OFFSET ?",
            (medium,offset)).fetchall()
        for row in rows:
            topic=joke_topic(app,row,now)
            if topic is not None:
                yield topic
                found+=1
                if found==16:
                    cursors[medium]=offset+16
                    return
        if len(rows)<16:
            cursors[medium]=0
            return
    cursors[medium]=start+512


def joke_topic(app,row,now):
    line=lexicon.public_line(app.store,int(row['origin_ref'][5:]))
    if line is None or lexicon.public_sources(app.store,(f"joke:{row['id']}",)) is None:
        return None
    return Topic(f"joke:{row['id']}",'joke',now,
        'Recorded running joke: '+json.dumps(row['term'])+'. Its actual origin line: '+
        json.dumps(truncate_words(line['text'],400))+'. This establishes no personal trait.',
        source_channel=line['channel'])


def current_joke(app,topic,now):
    row=app.store.conn.execute("SELECT id,term,origin_ref FROM lexicon WHERE id=? AND kind='joke' AND status='active'",
                              (int(topic.key[5:]),)).fetchone()
    if not row or not row['origin_ref'].startswith('chat:') or not row['origin_ref'][5:].isdigit():
        return False
    current=joke_topic(app,row,now)
    return bool(current and current.evidence==topic.evidence and current.source_channel==topic.source_channel)
