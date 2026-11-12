"""Observed Discord state. No game commands, model calls or inferred activity."""
from dataclasses import dataclass
from datetime import datetime
import json
import re

from leeroy_mind.discord import layout
from leeroy_mind.discord.channels import is_private
from leeroy_mind.discord.presence import Presence, RNG_SEED, STATUS_SHARE, local_time, presence_of, status_text

PLAY_KEY = 'server:playing'
PLAY_STALE_S = 180


@dataclass(frozen=True, slots=True)
class DiscordState:
    presence: Presence
    status: str | None
    reading: str | None
    voice_room: str | None
    last_said: tuple[str, float, int] | None
    unread_mentions: int
    with_her: tuple[int, ...]


def pulled_into_play(app) -> frozenset[int]:
    world = app.world
    members = set()
    if world.human_online and world.group is not None:
        members.update(world.group.members)
        members.add(world.group.leader_guid)
    if app.guild is not None:
        members.update(app.guild.members_in_play())
    return frozenset(members - {app.human_guid})


def refresh_play(app, now: float) -> None:
    with app.store.transaction():
        app.store.set_meta(PLAY_KEY, json.dumps({'guids': sorted(pulled_into_play(app)), 'at': now}))


def read_play(conn, now: float) -> frozenset[int]:
    row = conn.execute('SELECT value FROM meta WHERE key=?', (PLAY_KEY,)).fetchone()
    try:
        value = json.loads(row[0]) if row else {}
        if (not isinstance(value, dict) or type(value.get('at')) not in (int, float)
                or not 0 <= now - value['at'] <= PLAY_STALE_S
                or not isinstance(value.get('guids'), list)
                or any(type(g) is not int or g <= 0 for g in value['guids'])):
            return frozenset()
        return frozenset(value['guids'])
    except (TypeError, ValueError):
        return frozenset()


def state_of(app, guid: int, now: float) -> DiscordState:
    conn, world = app.store.conn, app.world
    cast = world.discord_members()
    p, member = app.personas.get(guid), cast.get(guid)
    owner_now = datetime.fromtimestamp(now, app.clock.local().tzinfo)
    local = local_time(owner_now, p.identity.region) if p is not None else owner_now
    presence = presence_of(p, local=local, body_online=guid in world.online,
        body_zone=getattr(member, 'zone', 0), rng_seed=RNG_SEED,
        in_his_group=guid in pulled_into_play(app)) if p is not None else Presence('offline', False, 0)
    row = conn.execute('SELECT channel,ts,reply_to FROM discord_message WHERE author=? '
        'AND origin=\'mind\' AND deleted IS NULL AND ts<=? ORDER BY ts DESC,id DESC LIMIT 1', (guid, now)).fetchone()
    last, reading = None, None
    if row:
        parent = conn.execute('SELECT author FROM discord_message WHERE id=?', (row[2],)).fetchone() if row[2] else None
        target = app.human_guid if is_private(row[0]) else parent[0] if parent else 0
        last = (row[0], row[1], target)
        if now-row[1] <= 1800:
            reading = row[0]
    officers = {g for g in cast if world.is_officer(g)}
    channels = [c.id for c in layout.CHANNELS
                if guid in layout.audience(c.id, cast=set(cast), officers=officers, human=app.human_guid)]
    channels.append(f'dm:{guid}')
    name = getattr(member, 'name', p.character_name if p else '')
    pattern = re.compile(r'(?<!\w)@' + re.escape(name) + r'\b', re.I) if name else None
    since = row[1] if row else 0
    mentions = sum(bool(pattern.search(r[0])) for r in conn.execute(
        'SELECT text FROM discord_message WHERE channel IN (SELECT value FROM json_each(?)) '
        'AND deleted IS NULL AND author<>? AND ts>? AND ts<=? AND instr(text,\'@\')>0',
        (json.dumps(channels), guid, since, now))) if pattern else 0
    company = ()
    room_definition = layout.channel(reading) if reading is not None else None
    if reading in channels and room_definition is not None and not room_definition.officers_only:
        company = tuple(r[0] for r in conn.execute('SELECT DISTINCT author FROM discord_message '
            'WHERE channel=? AND deleted IS NULL AND author<>? AND ts BETWEEN ? AND ? ORDER BY author',
            (reading, guid, now-900, now)) if r[0] in cast and r[0] != app.human_guid)
    comms = app.engine.comms
    room = comms.room_of(guid) if comms is not None else None
    return DiscordState(presence, status_text(p, local, presence, rng_seed=RNG_SEED) if p else None,
                        reading, room, last, mentions, company)


def tie_kind(conn, a: int, b: int) -> str:
    if conn.execute('SELECT 1 FROM feud WHERE ended IS NULL AND '
                    '((a=? AND b=?) OR (a=? AND b=?)) LIMIT 1', (a,b,b,a)).fetchone():
        return 'feud'
    row = conn.execute('SELECT affinity,romance_stage FROM relationship WHERE a=? AND b=?', (a,b)).fetchone()
    if row is None:
        return 'neutral'
    if row[1] not in ('none', 'ex'):
        return 'crush'
    return 'friend' if row[0] >= 60 else 'fond' if row[0] >= 30 else 'rival' if row[0] <= -30 else 'neutral'
