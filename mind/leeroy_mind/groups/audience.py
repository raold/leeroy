"""Historical group readers are immutable; every current reader must be among them."""
from dataclasses import dataclass
import json

from leeroy_mind.discord.channels import is_group, is_private


def parse_readers(raw):
    try:
        values = json.loads(raw) if isinstance(raw,str) else raw
        if (not isinstance(values,(list,tuple,set,frozenset)) or not 2 <= len(values) <= 5
                or any(type(g) is not int or g <= 0 for g in values)):
            return frozenset()
        return frozenset(values)
    except (ValueError,TypeError):
        return frozenset()


@dataclass(frozen=True, slots=True)
class GroupSource:
    channel: str
    readers: frozenset[int]


def turn_readers(conn, channel, speaker, to_guid):
    if is_private(channel):
        return frozenset({speaker,to_guid}) if speaker and to_guid and speaker != to_guid else frozenset()
    if is_group(channel):
        from .store import readers
        current = readers(conn,channel)
        return current if speaker in current else frozenset()
    return frozenset()


def message_readers(conn, message):
    """A post's captured audience, never today's membership substituted for it."""
    row = conn.execute("SELECT l.audience FROM meta m JOIN chat_line l "
        "ON l.id=CAST(m.value AS INTEGER) JOIN discord_message d ON d.id=? "
        "WHERE m.key=? AND l.channel=d.channel AND l.from_guid=d.author",
        (message,f'discord:line:{message}')).fetchone()
    return parse_readers(row[0]) if row else frozenset()


def visible(source_readers, target, *, speaker, to_guid, readers=()):
    prior = parse_readers(source_readers)
    if is_private(target):
        from leeroy_mind.discord.layout import dm_peer
        peer = dm_peer(target)
        if peer is not None and peer not in {speaker,to_guid}:
            return False
        current = frozenset({speaker,to_guid}) if speaker and to_guid and speaker != to_guid else frozenset()
    elif is_group(target):
        current = parse_readers(readers)
    else:
        return False
    return bool(prior and current and speaker in current and current <= prior)
