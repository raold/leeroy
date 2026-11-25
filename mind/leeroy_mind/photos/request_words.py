"""Recognize an explicit request for the addressed member's own photo, without a model."""
import re

_PHOTO = r'(?:selfies?|pics?|pictures?|photos?|snaps?|pix)'
_YOU = r'(?:you|u|ya)'
_PET = (r'(?:babe|baby|bae|hun|hon|honey|love|lover|sweetie|sweetheart|darling|dear|sexy|beautiful|'
        r'gorgeous|cutie|girl|princess|angel|hottie|cutie\s*pie)')
_SOFT = (r'(?:(?:please|pls|plz|just|also|so|and|now|then|ok|okay|hey|yo|lol|haha|omg|oh|well|'
         r'come\s+on|c\'?mon|' + _PET + r')\s+)*')
_VERB = r'(?:send|show|post|take|drop|share|snap|shoot|dm|text|message|msg)'
_ING = (r'(?:sending|showing|posting|taking|dropping|sharing|snapping|shooting|dm[\'’]?ing|'
        r'texting|messaging)')
# "go ahead and send", "hurry up and send", "try to send"; "keep sending", "retry sending",
# "would you mind sending" (the owner's DMs, 2026-11-17..23).
_LEAD = r'(?:(?:go\s+ahead|hurry(?:\s+up)?)\s+(?:and\s+)?|try\s+(?:to\s+|and\s+)?)'
_ASK = (r'(?:' + _LEAD + r'?' + _VERB + r'|(?:keep|retry|try|start|mind)\s+' + _ING + r')'
        r'(?!\s+' + _YOU + r'\b)')
# Addressed to her: an imperative, "can/could/would/will you ...", his "can I see/get/have",
# "let me see", "gimme", or "I want/need/'d love ..." a photo or her to send one. Never his own
# "I'll send", "let me take", "send you" or "want to see a pic?" (his offer of his own).
_WANT = (r'i(?:\s+(?:really\s+|just\s+|so\s+)?(?:want|wanna|need|would\s+love|would\s+like)|'
         r'[\'’]d\s+(?:really\s+)?(?:love|like))\s+'
         r'(?:(?:you|u)\s+to\s+' + _ASK + r'|(?:to\s+)?see\b|(?=(?:a|an|another|some|more|one|ur|your|new)\b))')
_FRAME = re.compile(_SOFT + r'(?:(?:(?:can|could|would|will)\s+' + _YOU + r'\s+(?:please\s+|pls\s+|just\s+)?)?'
                    + _ASK + r'|(?:can|could|may)\s+i\s+(?:please\s+)?(?:see|get|have)|let\s+me\s+see|'
                    r'lemme\s+see|gimme|give\s+me|' + _WANT + r')\b', re.I)
_GOT = re.compile(_SOFT + r'(?:(?:have\s+)?' + _YOU + r'\s+)?got\s+(?=(?:a|an|any|another|some)\b)', re.I)
# Up to six words before the photo noun, never a conjunction or someone else's: "DM me a sensual
# sexy NSFW selfie", but not "send me my pic" or "show me that and a pic".
_FILL = (r'(?:(?!(?:and|then|but|or|if|when|because|my|his|their|it|that|this|those|these)\b)'
         r'[\w\'’-]+\s+){0,6}?')
_OBJECT = re.compile(r'\s*' + _FILL + r'(' + _PHOTO + r')\b(?:\s+(of|from)\s+(\S.*))?', re.I)
_CLAUSE = re.compile(r'([^.!?;:,\n]+)([.!?;:,\n]*)')
_JOIN = re.compile(r'\s+(?:and|then|n)\s+', re.I)
# A fresh ask after a pet name mid-clause: "show me everything baby send me a selfie".
_RESTART = re.compile(r'\b' + _PET + r'\s+', re.I)
_BARE = re.compile(r'^(?:please\s+)?(?:selfie|pic)\s*\?(?:\s*please)?[!?\s]*$', re.I)
# "what are you wearing?" asks to see her: a photo of how she is.
_WEARING = re.compile(r"\bwhat\s+(?:are|r)\s+" + _YOU + r"\s+wearing\b|"
                      r"\b(?:show|let)\s+me\s+(?:see\s+)?what\s+" + _YOU + r"(?:['’]?re|\s+are|\s+r)?\s+wearing\b",
                      re.I)
_PAST = re.compile(r'\b(?:that|this|the\s+(?:last|earlier|previous|same))\s+' + _PHOTO + r'\b|'
                   r'\b(?:you\s+(?:sent|posted|shared|took)|already\s+(?:sent|posted|shared)|'
                   r'(?<!\bcan\s)(?<!\bcould\s)(?<!\bmay\s)i\s+(?:took|sent|posted|shared|have)|'
                   r'my\s+(?:own\s+)?' + _PHOTO + r')\b', re.I)
_NEGATIVE = re.compile(r"\b(?:don['’]?t|do\s+not|never|stop|quit)\s+(?:" + _VERB + '|' + _ING + r")\b", re.I)
# "of your X" is hers unless X is someone or something else of hers (her cat, her setup).
_OTHER = re.compile(
    r'(?:cats?|kitty|kittens?|dogs?|doggo|puppy|pups?|pets?|birds?|fish|horses?|setup|build|desk|'
    r'pc|computer|screen|monitor|keyboard|phone|car|bike|house|home|apartment|flat|garden|yard|'
    r'view|food|lunch|dinner|breakfast|meal|snacks?|coffee|drinks?|cake|cooking|plate|kids?|son|'
    r'daughter|baby|husband|boyfriend|bf|wife|girlfriend|gf|partner|friends?|sister|brother|'
    r'mom|mum|mother|dad|father|family|parents|toon|character|alt|gear|mount|loot|raid|guild|'
    r'team|transmog|plants?|flowers?|art|drawing|painting|work|office|class|homework|notes|'
    r'room|kitchen|place|town|city|trip|vacation|holiday)\b', re.I)
# What makes an ask more than "a photo of you": where, what she wears or does, or how he wants
# it ("sexy", "nasty"). Those take a fresh photo; a plain ask may take a ready-made one.
_SCENE = re.compile(
    r'\b(?:wear|wears|wearing|dress|dressed|dresses|outfit|outfits|clothes|uniform|scrubs|bikini|'
    r'swimsuit|lingerie|pajamas|pyjamas|pjs|put\s+on|change|changed|changing|in\s+(?:your|ur|a|an|'
    r'the|that|those|bed)|at\s+(?:work|the|your|ur|home)|from\s+(?:work|the|your|ur|home|bed)|'
    r'on\s+(?:the|your|ur|a)|with\s+(?:your|ur|a|an|the)|outside|inside|while|during|mirror|'
    r'bathroom|bedroom|kitchen|gym|beach|pool|shower|bath|office|couch|sofa|desk|car|bed|'
    r'sexy|sexier|sexiest|hot|hotter|nasty|naughty|sensual|slutty|sluttiest|spicy|steamy|nsfw|'
    r'cute|cutest|pretty|flirty|seductive|provocative|risque|risqu[eé]|lewd|dirty|filthy|'
    r'of\s+(?:your|ur)\s+(?!face\b))', re.I)


def _her(names):
    own = '|'.join(re.escape(n) for n in names if n)
    return own, re.compile(r'(?:' + _YOU + r'|yourself|urself|(?:your|ur)\s+face'
                           + ('|' + own if own else '') + r')\b', re.I)


def _theirs(subject: str) -> bool:
    """"your <words>": hers unless one of the next three words is another subject."""
    found = re.match(r'(?:your|ur)\s+((?:[\w\'’-]+\s*){1,3})', subject, re.I)
    return bool(found) and not _OTHER.search(found.group(1))


def _starts(clause: str):
    yield 0
    for found in _RESTART.finditer(clause):
        yield found.end()


def _asked(text: str, names=()) -> bool:
    if not isinstance(text, str):
        return False
    text = re.sub(r'\b(of|from)\s+@(?=\w)', r'\1 ', text, flags=re.I)
    text = re.sub(r'(?<!\w)@\w+', '', text).strip(' ,:;')
    text = re.sub(r'\s+[-–—]+\s+', ', ', text)  # "NSFW - send me a selfie"
    if _NEGATIVE.search(text) or _PAST.search(text):
        return False
    if _BARE.fullmatch(text) or _WEARING.search(text):
        return True
    own, her = _her(names)
    clauses = [(part, end) for whole, end in _CLAUSE.findall(text)
               for part in _JOIN.split(whole)]  # "... when you're free and send me a selfie"
    for clause, end in clauses:
        clause = clause.strip()
        if own:  # her name before the ask: "Ana send me a pic"
            clause = re.sub(r'^(?:' + own + r')\b\s*', '', clause, flags=re.I)
        for start in _starts(clause):
            frame = _FRAME.match(clause, start) or (end.startswith('?') and _GOT.match(clause, start))
            found = _OBJECT.match(clause, frame.end()) if frame else None
            if found is None:
                continue
            noun, link, subject = found.groups()
            if (link and not her.match(subject) and not _theirs(subject)
                    and (link.lower() == 'of' or not noun.lower().startswith('selfie'))):
                continue
            return True
    return False


def asks_for_selfie(text: str, names=()) -> bool:
    """His words are only intent/context; this result never supplies image-prompt text.

    A photo "of" anything but her (you, yourself, your face, her `names`, or "your <words>"
    that name no other subject such as her cat) is another subject; so is one "from" anything
    but her, unless it is a selfie.
    """
    return _asked(text, names)


def specific_ask(text: str) -> bool:
    """Whether his ask describes the photo (a place, what she wears or does, or how he wants
    it), so a ready-made photo would not answer it. A plain "send me a pic", "selfie?" or
    "what are you wearing?" is not. Only a yes/no reaches the caller, never his words."""
    if not isinstance(text, str):
        return True
    plain = _WEARING.sub(' ', text)
    return bool(_SCENE.search(plain))


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
