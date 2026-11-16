"""Phrase cooldown (owner's Turing-test push, 2026-11-15). The whole-line repeat guard missed
phrases: "you absolute menace" five times in 37 minutes, "I'm sitting here" in 8 of 15 DMs and
"be the death of me" from 8 members never came near its 0.70 line overlap.

A draft is flagged when one of its 2-5 word phrases (a word outside STOP, two for a two-word
phrase; no member's, his or a nickname's name; no capitalised proper noun) is one she already
used in this conversation (`phrase_conversation`), one she used in 2 or more of her lines in the
last 24 h (`phrase_day`), or one 3 or more other members used in the last 48 h (`phrase_cast`,
3+ words). A phrase he or another member used in the current conversation is its topic, not
her tic. The curated list in `stock_phrases.txt` is rationed on top: once per member in 72 h
(`stock_member`) and 3 times a day across the cast (`stock_cast`).

Reads are bounded and read-only; nothing here writes, logs or calls a model. Evidence is her own
lines in any channel and other members' lines, counted and never quoted: the rewrite hint names
only words of the draft itself (`Found.phrases`), never who else said them or where.
"""
from dataclasses import dataclass, field
from functools import lru_cache
from importlib import resources
import re

from leeroy_mind.text import strip_wow_markup

MIN_WORDS, MAX_WORDS = 2, 5
CONVERSATION_GAP = 30 * 60          # her lines this close together are one conversation
CONVERSATION_MAX = 3 * 3600
DAY_SECONDS, DAY_USES = 86400, 2    # the third use in a day is flagged
CAST_SECONDS, CAST_MEMBERS, CAST_WORDS = 48 * 3600, 3, 3
CAST_CONTENT = 2                    # "in a row" is everyone's; "cry for help" is a tell
STOCK_MEMBER_SECONDS = 72 * 3600
STOCK_CAST_SECONDS, STOCK_CAST_USES = 86400, 3
LINE_LIMIT, OWN_LIMIT = 4000, 400   # the cast's lines over the longest window, newest first
MAX_NAMED = 3
VOCATIVE_REACH = 3                  # "you" up to this many words before a closing epithet
CODES = ('phrase_conversation', 'phrase_day', 'stock_member', 'phrase_cast', 'stock_cast')

# Function words and the chat's commonest fillers, apostrophes dropped. A phrase needs a word
# outside this list ("i want to" has none); a two-word phrase needs two ("sitting here" has one).
STOP = frozenset('''
a about above absolutely actually after again against ago ah ahh all almost along already also
although always am among an and another any anybody anyone anything anyway anyways are arent
around as ask asked at aw away back bad basically be because been before being below best better
between both but by can cant come comes coming cool could couldnt day days definitely did didnt
do does doesnt doing done dont down during each eh eight either else end enough even ever every
everyone everything few find fine first five for found four from further gave get gets getting
give given gives go goes going gone good got gotta great guess guys had hadnt hah haha hahaha
has hasnt have havent having he hed hell her hes hey hi him himself his hm hmm honest honestly
hour hours how hows i id if ill im in into is isnt it itd itll its itself ive just keep kept
kind kinda know last later let lets like literally little lmao lol look looked looks lot lots
made make making many maybe me mean meant might mine minute minutes more morning most much must
my myself na nah need never new next nice night nine no nope nor not nothing now of off oh ok
okay omg on once one only or other our ours out over own people please pretty probably put quite
rather ready really rest right said same say says see seems seriously seven she shed shell shes
should shouldnt six so some somebody someone something soon sorry sort sorta sound sounds start
started still stop such sure take tell ten than thank thanks that thatd thatll thats the their
theirs them themselves then there theres these they theyd theyll theyre theyve thing things
think this those though three through thru thx time to today told tomorrow tonight too totally
tried try trying two uh um under until up upon us use used very via wanna want was wasnt way we
wed week weekend well went were werent weve what whatever whats when where wheres whether which
while who whod whole whom whos why will with within without wont work worse would wouldnt ya yah
yeah year years yep yes yesterday yet you youd youll your youre yours yourself youve yup
'''.split())

# The cast's typing shortcuts (talk/style.py abbreviations), folded back before matching.
FOLD = {'u': ('you',), 'ur': ('your',), 'gonna': ('going', 'to'), 'wanna': ('want', 'to'),
        'gotta': ('got', 'to'), 'tht': ('that',), 'wt': ('what',), 'rly': ('really',),
        'rlly': ('really',), 'nt': ('not',), 'wy': ('why',), 'thx': ('thanks',), 'bc': ('because',),
        'cuz': ('because',), 'abt': ('about',), 'vr': ('very',), 'gd': ('good',), 'wk': ('week',),
        'rdy': ('ready',), 'sry': ('sorry',), 'tho': ('though',), 'rn': ('right', 'now'),
        'hw': ('how',), 'wh': ('who',), 'th': ('the',), 'pls': ('please',), 'plz': ('please',),
        'ppl': ('people',), 'idk': ('i', 'dont', 'know'), 'tbh': ('to', 'be', 'honest'),
        'ngl': ('not', 'going', 'to', 'lie'), 'imo': ('in', 'my', 'opinion'), 'ofc': ('of', 'course'),
        'prob': ('probably',), 'probs': ('probably',), 'cos': ('because',),
        # talk/style.py's spelling habits and finger slips (typing texture, 2026-11-15)
        'finaly': ('finally',), 'realy': ('really',), 'probly': ('probably',), 'literaly': ('literally',),
        'basicly': ('basically',), 'tonite': ('tonight',), 'alot': ('a', 'lot'), 'awhile': ('a', 'while'),
        'everytime': ('every', 'time'), 'noone': ('no', 'one'), 'wich': ('which',), 'loose': ('lose',),
        'teh': ('the',), 'adn': ('and',), 'jsut': ('just',), 'waht': ('what',), 'taht': ('that',),
        'wiht': ('with',), 'ahve': ('have',), 'konw': ('know',), 'baout': ('about',),
        'thnik': ('think',)}
_I = frozenset({'i', 'im', 'id', 'ill', 'ive'})
_LEAD = frozenset('a an the and but or so then just also oh um uh well lol haha'.split())
_TAIL = frozenset('a an the and but or so to of for with at in on from by like lol haha'.split())
_HIDE = re.compile(r'\{\{item:\d+\}\}|\[[^\[\]\n]{0,80}\]|https?://\S+|<[@#:][^<>\n]{0,80}>')
_SENTENCE = re.compile(r'[.!?]+(?=[\s"\')]|$)|\n')
_CLAUSE = re.compile(r'[,;:()"“”—–…]|\s-+\s|\.\.+')
_WORD = re.compile(r"[^\W_]+(?:['’][^\W_]+)*")


@dataclass(frozen=True)
class Token:
    i: int
    norm: str
    start: int
    end: int
    sentence: int
    clause: int
    proper: bool


@dataclass(frozen=True)
class Found:
    codes: tuple[str, ...] = ()
    phrases: tuple[str, ...] = field(default=(), repr=False)


def _fold(raw):
    word = raw.casefold().replace('’', "'")
    if word.startswith("u'"):
        word = 'you' + word[1:]
    word = word.replace("'", '')
    return FOLD.get(word, (word,))


def tokens(text: str) -> list[Token]:
    """The words of `text` (already free of WoW markup), folded, each with its sentence and
    comma clause. Item placeholders, bracketed names, URLs and mentions break a clause."""
    hidden = _HIDE.sub(lambda m: ',' + ' ' * (len(m.group(0)) - 1), text or '')
    breaks = sorted([(m.start(), 2) for m in _SENTENCE.finditer(hidden)] +
                    [(m.start(), 1) for m in _CLAUSE.finditer(hidden)])
    out, sentence, clause, last, b, first = [], 0, 0, 0, 0, True
    for m in _WORD.finditer(hidden):
        kind = 0
        while b < len(breaks) and breaks[b][0] < m.start():
            if breaks[b][0] >= last:
                kind = max(kind, breaks[b][1])
            b += 1
        if out and kind:
            clause += 1
            if kind == 2:
                sentence, first = sentence + 1, True
        raw = m.group(0)
        folded = _fold(raw)
        proper = (raw[:1].isupper() and folded[0] not in _I
                  and (not first or raw.isupper() and len(raw) > 1))
        for norm in folded:
            out.append(Token(len(out), norm, m.start(), m.end(), sentence, clause, proper))
        last, first = m.end(), False
    return out


def _clauses(toks):
    groups = {}
    for t in toks:
        groups.setdefault(t.clause, []).append(t)
    return list(groups.values())


@lru_cache(maxsize=4096)
def _words(text):
    return tuple(t.norm for t in tokens(text))


@lru_cache(maxsize=8192)
def _seen(text):
    """A stored line's clauses (folded words) and every 2-5 word run inside one clause."""
    clauses = tuple(tuple(t.norm for t in c) for c in _clauses(tokens(strip_wow_markup(text or ''))))
    grams = frozenset(c[i:i + n] for c in clauses for n in range(MIN_WORDS, MAX_WORDS + 1)
                      for i in range(len(c) - n + 1))
    return clauses, grams


def _content(word):
    return len(word) > 1 and word not in STOP and not word.isdigit()


@lru_cache(maxsize=1)
def stock() -> tuple[tuple[str, tuple[tuple[bool, tuple[str, ...]], ...]], ...]:
    """The curated list: (name, ((anchored, words), ...)) for each line of stock_phrases.txt."""
    raw = (resources.files('leeroy_mind.guards') / 'stock_phrases.txt').read_text(encoding='utf-8')
    entries = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        alts = tuple((alt.strip().startswith('^'), _words(alt.strip().lstrip('^')))
                     for alt in line.split('|') if _words(alt.strip().lstrip('^')))
        if alts:
            entries.append((' '.join(alts[0][1]), alts))
    return tuple(entries)


def _at(words, phrase, anchored):
    n = len(phrase)
    starts = (0,) if anchored else range(len(words) - n + 1)
    return [i for i in starts if tuple(words[i:i + n]) == phrase]


def _has(clauses, alts):
    return any(_at(c, words, anchored) for c in clauses for anchored, words in alts)


def names(reader) -> frozenset:
    """Members', his and the nicknames' names as folded word tuples (one bounded read; an
    older schema without one of the tables reads none)."""
    rows = reader.rows('names_unavailable',
        "SELECT name FROM (SELECT json_extract(json,'$.character_name') AS name FROM persona_core "
        "WHERE json_valid(json) LIMIT 2000) UNION ALL SELECT value FROM meta WHERE key='human_name' "
        "UNION ALL SELECT name FROM (SELECT term AS name FROM lexicon WHERE kind='nickname' "
        "AND status='active' LIMIT 1000)")
    return frozenset(w for row in rows if isinstance(row['name'], str) and (w := _words(row['name'])))


def candidates(text: str, known=frozenset()):
    """The draft's tokens and eligible phrases: {words: [(first, last) token index, ...]}."""
    toks = tokens(text)
    found = {}
    for clause in _clauses(toks):
        words = [t.norm for t in clause]
        blocked = set()
        for name in known:
            for i in _at(words, name, False):
                blocked.update(range(i, i + len(name)))
        for n in range(MIN_WORDS, MAX_WORDS + 1):
            for i in range(len(clause) - n + 1):
                window = clause[i:i + n]
                if blocked.intersection(range(i, i + n)) or any(t.proper for t in window):
                    continue
                if sum(_content(t.norm) for t in window) < (2 if n == 2 else 1):
                    continue
                found.setdefault(tuple(words[i:i + n]), []).append((window[0].i, window[-1].i))
    return toks, found


def _stock_spans(toks):
    out = []
    for name, alts in stock():
        for clause in _clauses(toks):
            words = [t.norm for t in clause]
            for anchored, alt in alts:
                out.extend((alts, (clause[i].i, clause[i].i + len(alt) - 1)) for i in _at(words, alt, anchored))
    return out


def _key(channel, from_guid, to_guid):
    """A whisper and a DM between the same two are one conversation, in either direction."""
    if channel == 'whisper' or channel.startswith('dm:'):
        return ('pair', frozenset((from_guid, to_guid)))
    return ('room', channel)


def raw_channel(draft) -> str:
    """The chat_line channel a draft is stored on."""
    prefix = {'discord': 'discord:', 'dm': 'dm:', 'group': 'gdm:', 'voice': 'voice:'}.get(draft.channel)
    if prefix:
        return prefix + draft.room
    return draft.room if draft.channel == 'game' else draft.channel


def _conversation(own, key, now):
    """Her lines in this conversation, each within CONVERSATION_GAP of the next and at most
    CONVERSATION_MAX back, and where it began. `own` is newest first."""
    lines, edge = [], now
    for row in own:
        if edge - row['ts'] > CONVERSATION_GAP or now - row['ts'] > CONVERSATION_MAX:
            break
        if _key(row['channel'], row['from_guid'], row['to_guid']) == key:
            lines.append(row)
            edge = row['ts']
    return lines, min(edge, now - CONVERSATION_GAP)


def _surface(text, toks, spans):
    """Overlapping spans merged, as the draft wrote them. A leading article or conjunction and
    a trailing preposition are trimmed ("the death of me" -> "death of me") while two words and
    one outside STOP stay."""
    merged = []
    for first, last in sorted(spans):
        if merged and first <= merged[-1][1] + 1 and toks[first].clause == toks[merged[-1][1]].clause:
            merged[-1][1] = max(merged[-1][1], last)
        else:
            merged.append([first, last])
    def whole(a, b):
        return b - a >= 1 and any(_content(t.norm) for t in toks[a:b + 1])
    out = []
    for first, last in merged:
        while toks[first].norm in _LEAD and whole(first + 1, last):
            first += 1
        while toks[last].norm in _TAIL and whole(first, last - 1):
            last -= 1
        out.append(text[toks[first].start:toks[last].end])
    return list(dict.fromkeys(out))


def check(reader, draft, now: float) -> Found:
    """The draft's overused phrases. `reader` is review.Reader on a read-only snapshot; replay
    passes `draft.line_id` so a stored line never counts against itself or later lines."""
    text = strip_wow_markup(draft.text)
    toks, phrases = candidates(text, names(reader))
    stocked = _stock_spans(toks)
    if not phrases and not stocked:
        return Found()
    cutoff = draft.line_id if draft.line_id is not None else 2**63 - 1
    rows = reader.rows('phrases_unavailable', 'SELECT from_guid,ts,channel,to_guid,origin,text FROM chat_line '
        "WHERE origin IN ('mind','human') AND ts BETWEEN ? AND ? AND id<? ORDER BY ts DESC,id DESC LIMIT ?",
        (now - max(STOCK_MEMBER_SECONDS, DAY_SECONDS, CAST_SECONDS, STOCK_CAST_SECONDS), now, cutoff,
         LINE_LIMIT))
    own = [r for r in rows if r['from_guid'] == draft.guid and r['origin'] == 'mind'][:OWN_LIMIT]
    cast = [r for r in rows if r['from_guid'] != draft.guid and r['origin'] == 'mind']
    hits = {code: [] for code in CODES}
    if phrases:
        key = _key(raw_channel(draft), draft.guid, draft.to_guid)
        talk, began = _conversation(own, key, now)
        said = set().union(*(_seen(r['text'])[1] for r in talk))
        # The conversation's topic is everyone's: words he or another member here used too are
        # never her tic ("grilled cheese" in a talk about grilled cheese).
        topic = set().union(*(_seen(r['text'])[1] for r in rows if r['from_guid'] != draft.guid
                              and r['ts'] >= began and _key(r['channel'], r['from_guid'], r['to_guid']) == key))
        day = [_seen(r['text'])[1] for r in own if now - r['ts'] <= DAY_SECONDS]
        wanted = {words for words in phrases if len(words) >= CAST_WORDS
                  and sum(map(_content, words)) >= CAST_CONTENT}
        others = {}
        for row in cast:
            if wanted and now - row['ts'] <= CAST_SECONDS:
                for words in _seen(row['text'])[1] & wanted:
                    others.setdefault(words, set()).add(row['from_guid'])
        for words, spans in phrases.items():
            if words in said and words not in topic:
                hits['phrase_conversation'] += spans
            if words not in topic and sum(words in grams for grams in day) >= DAY_USES:
                hits['phrase_day'] += spans
            if len(others.get(words, ())) >= CAST_MEMBERS:
                hits['phrase_cast'] += spans
    if stocked:
        mine = [_seen(r['text'])[0] for r in own if now - r['ts'] <= STOCK_MEMBER_SECONDS]
        recent = [_seen(r['text'])[0] for r in (*own, *cast) if now - r['ts'] <= STOCK_CAST_SECONDS]
        for alts, span in stocked:
            if any(_has(clauses, alts) for clauses in mine):
                hits['stock_member'].append(span)
            if sum(_has(clauses, alts) for clauses in recent) >= STOCK_CAST_USES:
                hits['stock_cast'].append(span)
    codes = tuple(code for code in CODES if hits[code])
    if not codes:
        return Found()
    named = _surface(text, toks, [span for code in codes for span in hits[code]])
    return Found(codes, tuple(named[:MAX_NAMED]))


def hint(phrases) -> str:
    """The retry's instruction naming her own words; empty when there are none."""
    quoted = [f"'{p}'" for p in dict.fromkeys(phrases) if p][:MAX_NAMED]
    if not quoted:
        return ''
    said = quoted[0] if len(quoted) == 1 else ', '.join(quoted[:-1]) + ' or ' + quoted[-1]
    return f"Don't say {said} again; say it your own way or not at all."


def _occurrences(toks, phrase):
    words = _words(phrase)
    return [(c[i].i, c[i].i + len(words) - 1) for c in _clauses(toks)
            for i in _at([t.norm for t in c], words, False)] if words else []


def _cut(text, start, end):
    return text[:start] + ' ' + text[end:]


def _tidy(text):
    """Close the gap a cut leaves, keeping paragraph breaks; emoticons such as ";)" and ":)"
    are left as they are."""
    text = re.sub(r'[ \t]+([,.!?])(?=\s|$)', r'\1', text)
    text = re.sub(r',(?:[ \t]*,)+', ',', text)
    text = re.sub(r',+[ \t]*([.!?]|$)', r'\1', text, flags=re.M)
    text = re.sub(r'^[ \t]*[,.!?]+[ \t]*', '', text, flags=re.M)
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' ?\n[ \n]*', lambda m: '\n\n' if m.group(0).count('\n') > 1 else '\n', text)
    return text.strip()


def _sentence_span(text, tok):
    breaks = list(_SENTENCE.finditer(text))
    start = max((m.end() for m in breaks if m.end() <= tok.start), default=0)
    stop = next((m.end() for m in breaks if m.start() >= tok.end), len(text))
    return start, stop


def _clause_span(toks, first, last):
    """The comma clause holding tokens first..last, with the comma that joins it, when its
    sentence keeps two or more words without it."""
    a = toks[first]
    if any(t.clause != a.clause for t in toks[first:last + 1]):
        return None
    same = [t for t in toks if t.sentence == a.sentence]
    if sum(t.clause != a.clause for t in same) < 2:
        return None
    mine = [t for t in same if t.clause == a.clause]
    before = [t for t in same if t.clause < a.clause]
    if before:  # a later clause: from the end of the clause before it
        return before[-1].end, mine[-1].end
    return mine[0].start, next(t for t in same if t.clause > a.clause).start


def _vocative(toks, first, last):
    """'you' and an epithet closing its sentence ("send it now you absolute menace"): the start
    of "you ...", when every word after it is outside STOP (so "show you exactly what that
    means" is no epithet) and two or more words stay before it."""
    a = toks[first]
    sentence = [t for t in toks if t.sentence == a.sentence]
    if last != sentence[-1].i:
        return None
    for t in reversed(toks[max(0, first - VOCATIVE_REACH):first]):
        if t.sentence != a.sentence or t.clause != a.clause:
            return None
        if t.norm == 'you':
            epithet = toks[t.i + 1:last + 1]
            return (t.start if all(_content(s.norm) for s in epithet)
                    and sum(s.i < t.i for s in sentence) >= 2 else None)
    return None


def strip(text: str, phrases, *, min_words: int = 3) -> str | None:
    """The text without the named phrases: a closing epithet ("..., you absolute menace") or the
    comma clause holding a phrase is cut, else its whole sentence. None when fewer than
    `min_words` words would survive or a phrase is still there."""
    text = strip_wow_markup(text or '')
    for phrase in phrases:
        for _ in range(8):
            toks = tokens(text)
            found = _occurrences(toks, phrase)
            if not found:
                break
            first, last = found[0]
            start = _vocative(toks, first, last)
            if start is not None:
                text = _cut(text, start, toks[last].end)
                continue
            span = _clause_span(toks, first, last) or _sentence_span(text, toks[first])
            text = _cut(text, *span)
        text = _tidy(text)
    toks = tokens(text)
    if len(toks) < min_words or any(_occurrences(toks, p) for p in phrases):
        return None
    return text
