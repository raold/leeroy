"""A member's wrong pronoun for another member (evening DM audit, 2026-11-16: asked about a
guildmate who is a woman, a member called her "he" in all three runs).

`misgendered` reads a draft that names guild members of one gender by character name (in the
draft; else in his line it answers) and calls them only by the other sex's pronouns: a subject
or reflexive pronoun ("he", "she's", "himself") after the name, and none of theirs. The
roster's gender is the player's (personas.facts.pronoun_of: owner ruling 2026-11-15). It stays
quiet when the pronoun could be someone else's: a name she speaks to ("belra stop. he'll answer
later", "careful belra, he'll..."), someone she called "he" or "she" before the name, members
of both genders, a guild member without a persona, his name, a game name (a boss), or a person
or pet word ("my husband", "your cat"). Quoted words are someone else's. The rewrite hint names
the member and the pronouns only. Model-free; the caller reads the roster; nothing here logs.
"""
import re

from leeroy_mind.text import strip_wow_markup

from . import phrases

FORMS = {'female': ('she', 'her'), 'male': ('he', 'him')}
# What she calls someone: a subject or reflexive pronoun ("him" or "her" may be anyone's object).
_CALLS = {
    'male': re.compile(r"\b(?:he['’](?:s|d|ll)|himself|hes|he)\b", re.I),
    'female': re.compile(r"\b(?:she['’](?:s|d|ll)|herself|shes|she)\b", re.I),
}
# Any of their pronouns: one of these and the draft may be right after all.
_ANY = {
    'male': re.compile(r"\b(?:he|him|his|himself|he['’](?:s|d|ll)|hes)\b", re.I),
    'female': re.compile(r"\b(?:she|her|hers|herself|she['’](?:s|d|ll)|shes)\b", re.I),
}
_EITHER = (r"boss|partner|cat|kitty|kitten|dog|doggo|puppy|pup|pet|horse|pony|bird|parrot|"
           r"rabbit|bunny|hamster|fish|ferret|baby|kid|toddler|teacher|doctor|landlord|"
           r"neighbou?r|coworker|colleague|manager|flatmate|roommate|mate|friend|cousin")
# People (and pets) who could be the "he" or "she" instead.
_PEOPLE = {
    'male': re.compile(r"\b(?:husband|boyfriend|bf|dad|daddy|father|brother|bro|son|uncle|"
                       r"nephew|grandad|grandpa|granddad|guy|man|men|bloke|dude|lad|fella|"
                       rf"gentleman|king|lord|{_EITHER})s?\b", re.I),
    'female': re.compile(r"\b(?:wife|girlfriend|gf|mum|mom|mommy|mummy|mother|sister|sis|"
                         r"daughter|aunt|auntie|niece|nan|gran|grandma|granny|girl|woman|women|"
                         rf"lady|gal|lass|queen|{_EITHER})s?\b", re.I),
}
_QUOTED = re.compile(r"\"[^\"\n]*\"|“[^”\n]*”")
_NAME_WORD = re.compile(r"@?[^\W\d_][\w'’]*")
# Words around a name that make it the one she speaks to ("careful belra", "belra stop", "belra
# he's taken"), and words before a name ending its sentence that make it an object.
_ADDRESS_BEFORE = frozenset('''careful hey hi hiya yo oh thanks thank ty sorry morning night gz
grats congrats love babe dear ok okay'''.split())
_SUBJECT_AFTER = frozenset('''he she i you u we they it hes he's shes she's im i'm youre you're
theyre they're'''.split())
_START_CUES = frozenset('''chill careful if stop dont don't please pls plz lol lmao haha omg why
what how are did do can could would will wanna look listen wait hey hi yo go come get ur your
you u'''.split())
_OBJECT_BEFORE = frozenset('''with for to at about from on of ask asked tell told message ping dm
whisper check invite inv kick bench flag remind give gave and or'''.split())


def calls_someone(text: str) -> bool:
    """Whether `text` calls anyone "he" or "she" at all: the cheap test before the roster."""
    plain = strip_wow_markup(text or '')
    return bool(_CALLS['male'].search(plain) or _CALLS['female'].search(plain))


def _spoken_to(text: str, m) -> bool:
    """The name at `m` is the one she speaks to: "@name"; after "careful", "thanks"...; before
    a comma, "!" or "?"; ending her sentence or line, unless an object ("check with name.");
    before a pronoun; or opening her line before "chill", "if", "stop"..."""
    if m.group(0).startswith('@'):
        return True
    before = re.findall(r"[\w'’]+", text[:m.start()].casefold())
    if before and before[-1] in _ADDRESS_BEFORE:
        return True
    rest = text[m.end():]
    if re.match(r"\s*[,!?]", rest):
        return True
    if re.match(r"\s*(?:\.(?!\w)|$)", rest):
        return not before or before[-1] not in _OBJECT_BEFORE
    following = re.match(r"\s*([\w'’]+)", rest)
    word = following.group(1).casefold().replace('’', "'") if following else ''
    return word in _SUBJECT_AFTER or (not before and word in _START_CUES)


def _named(text: str, roster, others=frozenset()) -> tuple[list, bool]:
    """([(position, entry)] for each roster member `text` names in the third person, first
    mention; whether it names anyone else of the guild): a whole name, a possessive allowed
    ("belra's"); a name that is also an everyday word only capitalised."""
    out, else_ = {}, False
    for m in _NAME_WORD.finditer(text):
        raw = re.sub(r"['’]s$", '', m.group(0).lstrip('@'))
        key = raw.casefold()
        entry = roster.get(key)
        if entry is None:
            else_ = else_ or key in others
            continue
        if (entry[2] and not raw[:1].isupper()) or _spoken_to(text, m):
            continue
        out.setdefault(entry, m.start())
    return [(at, entry) for entry, at in out.items()], else_


def misgendered(text: str, his_line: str, roster: dict, *, his_names=(),
                others=frozenset()) -> tuple:
    """((name, pronoun as written, "she/her" or "he/him"), ...) for `text`, a member's draft
    answering `his_line` (his line it answers, "" for anyone else's). `roster`: folded character
    name -> (name, gender "female" or "male", whether the name is an everyday word), the speaker
    left out; `others`: folded names of guild members without one. Empty when nothing is wrong
    or it cannot tell."""
    plain = _QUOTED.sub(' ', strip_wow_markup(text or ''))
    if not calls_someone(plain):
        return ()
    named, else_ = _named(plain, roster, others)
    after, at, in_draft = plain, 0, bool(named)
    if named:
        at = min(at for at, _ in named)
        after = plain[at:]  # her pronouns for a name come after it
        if _ANY['male'].search(plain[:at]) or _ANY['female'].search(plain[:at]):
            return ()  # someone she already called "he": "he's baking for belra's party"
    elif not else_:
        named, else_ = _named(strip_wow_markup(his_line or ''), roster, others)
    genders = {entry[1] for _, entry in named}
    if else_ or len(genders) != 1 or not genders <= set(FORMS):
        return ()
    gender = genders.pop()
    other = 'male' if gender == 'female' else 'female'
    wrong = _CALLS[other].search(after)
    if wrong is None or _ANY[gender].search(after) or _PEOPLE[other].search(plain):
        return ()
    folded = plain.casefold()
    if any(n and re.search(r'(?<!\w)' + re.escape(n.casefold()) + r'(?!\w)', folded)
           for n in his_names):
        return ()
    words = [t.norm for t in phrases.tokens(plain)]
    if any(words[i:i + len(name)] == list(name) for name in phrases.game_names()
           for i in range(len(words) - len(name) + 1) if words[i] == name[0]):
        return ()  # "he" may be the boss she names
    # whom the pronoun means: the member she named last before it (the first his line names)
    before = [(where, entry) for where, entry in named if where <= at + wrong.start()]
    _, entry = max(before, key=lambda x: x[0]) if in_draft and before else named[0]
    return ((entry[0], wrong.group(0), '/'.join(FORMS[gender])),)


def hint(found) -> str:
    """The rewrite's words on it: the member and the pronouns, nothing else about them."""
    parts = []
    for name, wrong, right in dict.fromkeys(found):
        said, them = right.split('/')
        parts.append(f'you called {name} "{wrong}": use "{said}" and "{them}" for {name}')
    text = '; '.join(parts)
    return text[:1].upper() + text[1:] + '.' if text else ''
