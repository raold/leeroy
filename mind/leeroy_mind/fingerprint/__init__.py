"""Bounded, model-free typing measurements. Reading a profile never migrates or writes.

Rates are shares of lines, except the known-typo lower bound (share of words).
Private lines contribute numbers only: opener/closer text comes solely from public
guild/say/yell lines. No learned phrase text is ever rendered in a style hint.
"""
import json
import math
import re
import sqlite3
from collections import Counter
from dataclasses import asdict, dataclass, fields, replace
from itertools import combinations

from leeroy_mind.text import strip_wow_markup

VERSION = 1
MAX_LINES = 300
MIN_LINES = 50
PUBLIC = frozenset({'guild', 'say', 'yell'})
WORDS = re.compile(r"[A-Za-z]+(?:'[A-Za-z]+)?")
EMOTICONS = re.compile(r"(?<!\w)(?:[:;=8][-^]?[)(DPp/]|[xX][dD]|<3|o7)(?!\w)")
# A base pictograph plus its selectors/modifiers, including joined emoji and flags.
_BASE = r'[\U0001F000-\U0001FAFF\u2300-\u23FF\u2600-\u27BF\u2B00-\u2BFF]'
_MOD = r'[\uFE0F\U0001F3FB-\U0001F3FF]*'
EMOJI = re.compile(r'[\U0001F1E6-\U0001F1FF]{2}|' + _BASE + _MOD + r'(?:\u200D' + _BASE + _MOD + r')*')
LAUGHTER = frozenset({'lol', 'haha', 'lmao'})
SLANG = frozenset({'aggro', 'aoe', 'afk', 'brb', 'buff', 'debuff', 'dps', 'gg', 'heals',
                   'oom', 'pull', 'pug', 'rez', 'res', 'wipe', 'tank', 'nerf', 'proc', 'loot'})
# Deliberately narrow: no unknown-word dictionary which would call names/region/slang typos.
TYPOS = frozenset({'teh', 'hte', 'adn', 'nad', 'thier', 'recieve', 'becuase', 'definately',
                   'wierd', 'freind', 'agian', 'jsut', 'woudl', 'shoudl', 'coudl'})


@dataclass(frozen=True)
class Profile:
    version: int = VERSION
    available: int = 0
    seeded: int = 1
    samples: int = 0
    p25: float = 5
    p50: float = 10
    p90: float = 18
    lowercase: float = .4
    no_punctuation: float = .5
    emoji: float = .02
    emoticon: float = .08
    laughter: float = .1
    typo: float = 0
    question: float = .15
    exclamation: float = .1
    emphasis: float = .05
    slang: float = .15
    emoji_ids: tuple[int, ...] = ()
    emoticon_ids: tuple[int, ...] = ()
    laughter_ids: tuple[int, ...] = ()
    opener_ids: tuple[int, ...] = ()
    closer_ids: tuple[int, ...] = ()


RATES = ('lowercase', 'no_punctuation', 'emoji', 'emoticon', 'laughter', 'typo',
         'question', 'exclamation', 'emphasis', 'slang')
IDS = ('emoji_ids', 'emoticon_ids', 'laughter_ids', 'opener_ids', 'closer_ids')


def _number(value, default, lo, hi):
    return max(lo, min(hi, value)) if type(value) in (float, int) and math.isfinite(value) else default


def seed(persona=None, *, talkative=5, humour_dial=5, samples=0):
    """A transparent prior, not a claim about an age or region's real population.

    Median = 6 + talkative + age-decade offset + education offset. UK adds .02
    dry laughter; US adds .02 exclamations. Humour changes laughter/caps by fixed
    small amounts. Optional education uses only exact enums, never infers from a job.
    """
    persona = persona if isinstance(persona, dict) else {}
    identity = persona.get('identity', {})
    identity = identity if isinstance(identity, dict) else {}
    age = _number(identity.get('age'), 35, 25, 99)
    talk = _number(talkative, 5, 0, 10)
    humour = _number(humour_dial, 5, 0, 10) / 5
    education = persona.get('education')
    schooling = 2 if education in ('university', 'postgraduate') else 1 if education == 'college' else 0
    median = 6 + talk + (age - 35) / 10 + schooling
    kind = persona.get('humour')
    kind = kind if isinstance(kind, str) else ''
    laugh = {'dry': .06, 'dark': .06, 'self-deprecating': .1, 'teasing': .14,
             'puns': .12, 'absurd': .18}.get(kind, .1)
    older = (age - 25) / 74
    return Profile(available=int(bool(persona)), samples=samples, p25=round(median*.55, 2),
        p50=round(median, 2), p90=round(median*1.8, 2),
        lowercase=round(.6-.3*older-.03*schooling, 4),
        no_punctuation=round(.65-.3*older-.05*schooling, 4),
        emoji=round(.04-.03*older, 4), emoticon=round(.05+.01*talk, 4),
        laughter=round(min(1, (laugh + (.02 if identity.get('region') == 'UK' else 0))*humour), 4),
        question=round(.1+.01*talk, 4),
        exclamation=round(.05+.01*talk+(.02 if identity.get('region') == 'US' else 0), 4),
        emphasis=round(.02+.01*talk, 4), slang=round(.08+.01*talk, 4))


def quantile(values, q):
    """Linear interpolation at (n-1)*q, including the one-line case."""
    ordered = sorted(values)
    at = (len(ordered)-1)*q
    lo, hi = math.floor(at), math.ceil(at)
    return ordered[lo] + (ordered[hi]-ordered[lo])*(at-lo)


def measure(lines):
    """Pure numerical profile; caller supplies at most the latest 300 stored lines."""
    texts = [strip_wow_markup(text[:4096]).strip() for text in lines[:MAX_LINES]]
    words = [WORDS.findall(text) for text in texts]
    if not texts:
        return Profile()
    n, total = len(texts), sum(map(len, words))
    rate = lambda predicate: sum(predicate(t, w) for t, w in zip(texts, words))/n
    return Profile(available=1, seeded=0, samples=n,
        p25=quantile(list(map(len, words)), .25), p50=quantile(list(map(len, words)), .5),
        p90=quantile(list(map(len, words)), .9),
        lowercase=rate(lambda t,w: bool(w and w[0][0].islower())),
        no_punctuation=rate(lambda t,w: bool(t and t[-1] not in '.!?;:,…')),
        emoji=rate(lambda t,w: bool(EMOJI.search(t))),
        emoticon=rate(lambda t,w: bool(EMOTICONS.search(t))),
        laughter=rate(lambda t,w: bool(LAUGHTER.intersection(x.lower() for x in w))),
        typo=sum(x.lower() in TYPOS for row in words for x in row)/max(1, total),
        question=rate(lambda t,w: '?' in t), exclamation=rate(lambda t,w: '!' in t),
        emphasis=rate(lambda t,w: any(len(x)>1 and x.isupper() for x in w)),
        slang=rate(lambda t,w: bool(SLANG.intersection(x.lower() for x in w))))


def _persona(conn, guid):
    try:
        row = conn.execute('SELECT json FROM persona_core WHERE guid=?', (guid,)).fetchone()
        obj = json.loads(row[0]) if row else {}
        return obj if isinstance(obj, dict) else {}
    except (sqlite3.OperationalError, ValueError, TypeError, RecursionError):
        return {}


def _decode(raw):
    obj = json.loads(raw)
    if not isinstance(obj, dict) or set(obj) != {f.name for f in fields(Profile)}:
        raise ValueError('fingerprint_shape')
    for key in IDS:
        value = obj[key]
        if not isinstance(value, list) or len(value)>5 or any(type(x) is not int or x<1 for x in value):
            raise ValueError('fingerprint_ids')
        obj[key] = tuple(value)
    if (obj['version'] != VERSION or type(obj['version']) is not int
            or any(type(obj[k]) is not int or obj[k] not in (0,1) for k in ('available','seeded'))
            or type(obj['samples']) is not int or not 0 <= obj['samples'] <= MAX_LINES):
        raise ValueError('fingerprint_header')
    for key in (*RATES, 'p25', 'p50', 'p90'):
        value = obj[key]
        if type(value) not in (float,int) or not math.isfinite(value) or not 0<=value<=(4096 if key.startswith('p') and key[1:].isdigit() else 1):
            raise ValueError('fingerprint_number')
    if not obj['p25'] <= obj['p50'] <= obj['p90']:
        raise ValueError('fingerprint_quantiles')
    return Profile(**obj)


def fingerprint(conn, guid) -> Profile:
    """Stored current-version profile, or a deterministic persona prior; read-only on old DBs."""
    if type(guid) is not int or not 0 < guid <= 0xFFFFFFFF:
        raise ValueError('fingerprint_guid')
    from leeroy_mind.personas.tiers import is_cast
    if not is_cast(conn,guid):
        return seed()
    try:
        row = conn.execute('SELECT profile,version FROM voice_fingerprint WHERE guid=?', (guid,)).fetchone()
        if row and row[1] == VERSION:
            return _decode(row[0])
    except (sqlite3.OperationalError, ValueError, TypeError, RecursionError):
        pass
    return seed(_persona(conn, guid))


def render_style_hint(profile) -> str:
    """Fixed safe words only, no lexical lookup, <=160 characters (40 estimated tokens)."""
    p = profile
    length = 'brief lines' if p.p50<9 else 'medium lines' if p.p50<17 else 'longer lines'
    case = 'lowercase starts' if p.lowercase>=.5 else 'mixed case'
    punct = 'light punctuation' if p.no_punctuation>=.5 else 'regular punctuation'
    mood = 'some laughter' if p.laughter>=.15 else 'little laughter'
    marks = 'few questions' if p.question<.25 else 'some questions'
    return f'Typing: {length}; {case}; {punct}; {mood}; {marks}.'


def distinctiveness(profiles):
    """Mean pairwise L1 distance, each numeric feature normalized to [0,1].

    Lengths saturate at 60 words. Phrase ids are deliberately excluded: an id's
    numeric distance has no lexical meaning. None means fewer than two profiles.
    """
    rows = [p for p in profiles if p.available]
    vectors = [[min(1, getattr(p,k)/60) for k in ('p25','p50','p90')]
               + [getattr(p,k) for k in RATES] for p in rows]
    total, pairs = 0., 0
    for x,y in combinations(vectors, 2):
        total += sum(abs(a-b) for a,b in zip(x,y))/len(x)
        pairs += 1
    return total/pairs if pairs else None


def _phrases(rows, blocked):
    counts = {kind: Counter() for kind in ('opener','closer','emoji','emoticon','laughter')}
    for text, channel, recipient in rows:
        text = strip_wow_markup(text[:4096])
        # Only fixed non-content marks/laughter can come from a restricted line.
        counts['emoji'].update(s for s in EMOJI.findall(text) if len(s)<=80)
        counts['emoticon'].update(EMOTICONS.findall(text))
        words = [x.lower() for x in WORDS.findall(text)]
        counts['laughter'].update(x for x in words if x in LAUGHTER)
        if channel not in PUBLIC or recipient:
            continue
        for size in (1,2,3):
            if len(words)<size:
                continue
            for kind, gram in (('opener', words[:size]), ('closer', words[-size:])):
                phrase = ' '.join(gram)
                if len(phrase)<=80 and not blocked.intersection(gram):
                    counts[kind][phrase] += 1
    return {kind: [s for s,n in sorted(c.items(), key=lambda x: (-x[1], x[0]))[:5]]
            for kind,c in counts.items()}


def update(conn, guid, *, now, persona=None, talkative=5, humour_dial=5, blocked_names=()):
    """Write inside the caller's unit transaction; never commits or logs chat text."""
    from leeroy_mind.personas.tiers import is_cast
    if not is_cast(conn,guid):
        return seed()
    rows = conn.execute("SELECT text,channel,to_guid FROM chat_line WHERE from_guid=? AND origin='mind' "
                        'ORDER BY ts DESC,id DESC LIMIT ?', (guid,MAX_LINES)).fetchall()
    persona = _persona(conn,guid) if persona is None else persona
    if len(rows)<MIN_LINES:
        profile = seed(persona, talkative=talkative, humour_dial=humour_dial, samples=len(rows))
        phrases = {}
    else:
        profile = measure([r[0] for r in rows])
        blocked = {x.lower() for name in blocked_names for x in WORDS.findall(name)}
        for (name,) in conn.execute('SELECT name FROM cast_member'):
            blocked.update(x.lower() for x in WORDS.findall(name))
        # Protect authored real names too, without loading them into logs or the profile.
        for (raw,) in conn.execute('SELECT json FROM persona_core'):
            try:
                name = json.loads(raw).get('identity',{}).get('real_name','')
                blocked.update(x.lower() for x in WORDS.findall(name))
            except (ValueError, TypeError, AttributeError, RecursionError):
                continue
        phrases = _phrases(rows, blocked)
    ids = {}
    keep = []
    for kind, values in phrases.items():
        selected = []
        for text in values:
            conn.execute('INSERT OR IGNORE INTO phrase(guid,kind,text) VALUES(?,?,?)', (guid,kind,text))
            ident = conn.execute('SELECT id FROM phrase WHERE guid=? AND kind=? AND text=?', (guid,kind,text)).fetchone()[0]
            selected.append(ident)
        ids[kind+'_ids'] = tuple(selected)
        keep.extend(selected)
    profile = replace(profile, **ids)
    conn.execute('INSERT INTO voice_fingerprint(guid,profile,version,ts) VALUES(?,?,?,?) '
                 'ON CONFLICT(guid) DO UPDATE SET profile=excluded.profile,version=excluded.version,ts=excluded.ts',
                 (guid,json.dumps(asdict(profile),separators=(',',':'),allow_nan=False),VERSION,now))
    # Bound retained lexical data to at most 25 phrases/member; no stale private history.
    marks = ','.join('?' for _ in keep) or 'NULL'
    conn.execute(f'DELETE FROM phrase WHERE guid=? AND id NOT IN ({marks})' if keep else
                 'DELETE FROM phrase WHERE guid=?', (guid,*keep))
    return profile
