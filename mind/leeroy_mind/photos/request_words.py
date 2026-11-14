"""Recognize an explicit request for the addressed member's own photo, without a model."""
import re

_PHOTO = r'(?:selfies?|pics?|pictures?|photos?|snaps?)'
_ASK = re.compile(r'\b(?:send|show|post|take|drop|share|snap|got|can\s+i\s+(?:see|get)|let\s+me\s+see)\b', re.I)
_NOUN = re.compile(r'\b' + _PHOTO + r'\b', re.I)
_BARE = re.compile(r'^(?:please\s+)?(?:selfie|pic)\s*\?(?:\s*please)?[!?\s]*$', re.I)
_PAST = re.compile(r'\b(?:that|this|the\s+(?:last|earlier|previous|same))\s+' + _PHOTO + r'\b|'
                   r'\b(?:you\s+(?:sent|posted|shared|took)|already\s+(?:sent|posted|shared)|'
                   r'i\s+(?:took|sent|posted|shared|have)|my\s+(?:own\s+)?' + _PHOTO + r')\b', re.I)
_OTHER = re.compile(r'\b(?:of|from)\s+(?:me|myself|him|her|them|us|the\b|your\s+(?:pet|dog|cat|food|loot|gear|room))\b', re.I)
_NEGATIVE = re.compile(r"\b(?:don['’]t|do\s+not|never|stop)\s+(?:send|show|post|take|drop|share|snap)\b", re.I)


def asks_for_selfie(text: str) -> bool:
    """His words are only intent/context; this result never supplies image-prompt text."""
    if not isinstance(text, str):
        return False
    text = re.sub(r'(?<!\w)@\w+', '', text).strip(' ,:;')
    if _NEGATIVE.search(text) or _PAST.search(text) or _OTHER.search(text):
        return False
    if _BARE.fullmatch(text):
        return True
    ask = _ASK.search(text)
    noun = _NOUN.search(text, ask.end()) if ask else None
    return bool(ask and noun and noun.start() - ask.end() <= 100)


def request_target(app, message: int) -> int | None:
    """A member DM's peer, or the first explicitly addressed channel member."""
    from leeroy_mind.discord import layout
    from leeroy_mind.discord.channels import CHANNEL_PREFIX, is_group
    row = app.store.conn.execute('SELECT * FROM discord_message WHERE id=?', (message,)).fetchone()
    if (row is None or row['deleted'] is not None or row['origin'] != 'human'
            or row['author'] != app.human_guid or is_group(row['channel'])
            or not asks_for_selfie(row['text'])):
        return None
    peer = layout.dm_peer(row['channel'])
    if peer is not None:
        return peer if (peer != app.human_guid and peer in app.world.discord_members()
                        and app.personas.get(peer) is not None) else None
    if layout.channel(row['channel']) is None:
        return None
    replied = None
    if row['reply_to'] is not None:
        prior = app.store.conn.execute('SELECT author,origin,deleted,channel FROM discord_message WHERE id=?',
                                      (row['reply_to'],)).fetchone()
        if prior and prior['origin'] == 'mind' and prior['deleted'] is None and prior['channel'] == row['channel']:
            replied = prior['author']
    audience = app.world.audience(CHANNEL_PREFIX + row['channel'], from_guid=app.human_guid)
    named = app.discord_talk._named(row['text'], replied, audience)
    return named[0] if named else None
