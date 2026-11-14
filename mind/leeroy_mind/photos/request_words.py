"""Recognize an explicit request for the addressed member's own photo, without a model."""
import re

_PHOTO = r'(?:selfies?|pics?|pictures?|photos?|snaps?)'
_YOU = r'(?:you|u|ya)'
_SOFT = (r'(?:(?:please|pls|plz|just|also|so|and|now|then|ok|okay|hey|yo|lol|haha|omg|oh|well|'
         r'babe|baby|bae|hun|hon|honey|love|lover|sweetie|sweetheart|darling|dear|sexy|beautiful|'
         r'gorgeous|cutie|girl)\s+)*')
# Addressed to her at a clause's start: an imperative, "can/could/would/will you ...", or his
# "can I see/get" and "let me see". Never his own "I'll send", "let me take" or "send you".
_FRAME = re.compile(r'^' + _SOFT + r'(?:(?:(?:can|could|would|will)\s+' + _YOU + r'\s+(?:please\s+|pls\s+|just\s+)?)?'
                    r'(?:send|show|post|take|drop|share|snap)(?!\s+' + _YOU + r'\b)'
                    r'|(?:can|could|may)\s+i\s+(?:see|get)|let\s+me\s+see)\b', re.I)
_GOT = re.compile(r'^' + _SOFT + r'(?:(?:have\s+)?' + _YOU + r'\s+)?got\s+(?=(?:a|an|any|another|some)\b)', re.I)
_OBJECT = re.compile(r'\s*(?:[\w\'’-]+\s+){0,4}?(' + _PHOTO + r')\b(?:\s+(of|from)\s+(\S.*))?', re.I)
_CLAUSE = re.compile(r'([^.!?;,\n]+)([.!?;,\n]*)')
_JOIN = re.compile(r'\s+(?:and|then|n)\s+', re.I)
_BARE = re.compile(r'^(?:please\s+)?(?:selfie|pic)\s*\?(?:\s*please)?[!?\s]*$', re.I)
_PAST = re.compile(r'\b(?:that|this|the\s+(?:last|earlier|previous|same))\s+' + _PHOTO + r'\b|'
                   r'\b(?:you\s+(?:sent|posted|shared|took)|already\s+(?:sent|posted|shared)|'
                   r'i\s+(?:took|sent|posted|shared|have)|my\s+(?:own\s+)?' + _PHOTO + r')\b', re.I)
_NEGATIVE = re.compile(r"\b(?:don['’]t|do\s+not|never|stop)\s+(?:send|show|post|take|drop|share|snap)\b", re.I)


def asks_for_selfie(text: str, names=()) -> bool:
    """His words are only intent/context; this result never supplies image-prompt text.

    A photo "of" anything but her (you, yourself, your face, her `names`) is another subject;
    so is one "from" anything but her, unless it is a selfie.
    """
    if not isinstance(text, str):
        return False
    text = re.sub(r'\b(of|from)\s+@(?=\w)', r'\1 ', text, flags=re.I)
    text = re.sub(r'(?<!\w)@\w+', '', text).strip(' ,:;')
    if _NEGATIVE.search(text) or _PAST.search(text):
        return False
    if _BARE.fullmatch(text):
        return True
    own = '|'.join(re.escape(n) for n in names if n)
    her = re.compile(r'(?:' + _YOU + r'|yourself|urself|(?:your|ur)\s+face' + ('|' + own if own else '') + r')\b', re.I)
    clauses = [(part, end) for whole, end in _CLAUSE.findall(text)
               for part in _JOIN.split(whole)]  # "... when you're free and send me a selfie"
    for clause, end in clauses:
        clause = clause.strip()
        if own:  # her name before the ask: "Ana send me a pic"
            clause = re.sub(r'^(?:' + own + r')\b\s*', '', clause, flags=re.I)
        frame = _FRAME.match(clause) or (end.startswith('?') and _GOT.match(clause))
        found = _OBJECT.match(clause, frame.end()) if frame else None
        if found is None:
            continue
        noun, link, subject = found.groups()
        if link and (link.lower() == 'of' or not noun.lower().startswith('selfie')) and not her.match(subject):
            continue
        return True
    return False


def request_target(app, message: int) -> int | None:
    """A member DM's peer, or the first explicitly addressed channel member."""
    from leeroy_mind.discord import layout
    from leeroy_mind.discord.channels import CHANNEL_PREFIX, is_group
    row = app.store.conn.execute('SELECT * FROM discord_message WHERE id=?', (message,)).fetchone()
    if (row is None or row['deleted'] is not None or row['origin'] != 'human'
            or row['author'] != app.human_guid or is_group(row['channel'])):
        return None
    peer = layout.dm_peer(row['channel'])
    if peer is not None:
        guid = peer if (peer != app.human_guid and peer in app.world.discord_members()
                        and app.personas.get(peer) is not None) else None
    elif layout.channel(row['channel']) is None:
        return None
    else:
        replied = None
        if row['reply_to'] is not None:
            prior = app.store.conn.execute('SELECT author,origin,deleted,channel FROM discord_message WHERE id=?',
                                          (row['reply_to'],)).fetchone()
            if prior and prior['origin'] == 'mind' and prior['deleted'] is None and prior['channel'] == row['channel']:
                replied = prior['author']
        audience = app.world.audience(CHANNEL_PREFIX + row['channel'], from_guid=app.human_guid)
        named = app.discord_talk._named(row['text'], replied, audience)
        guid = named[0] if named else None
    if guid is None:
        return None
    member, p = app.world.discord_members().get(guid), app.personas.get(guid)
    real = p.identity.real_name if p is not None else ''
    names = (getattr(member, 'name', ''), real, *real.split()[:1])
    return guid if asks_for_selfie(row['text'], names=names) else None
