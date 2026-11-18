"""Phrase cooldown (owner's Turing-test push, 2026-11-15). The whole-line repeat guard missed
phrases: "you absolute menace" five times in 37 minutes, "I'm sitting here" in 8 of 15 DMs and
"be the death of me" from 8 members never came near its 0.70 line overlap.

A draft is flagged when one of its 2-5 word phrases (a word outside STOP, two for a two-word
phrase; no member's, his or a nickname's name; no capitalised proper noun) is one she already
used in 2 or more of her earlier lines in this conversation, or in one when it holds a stock
entry (`phrase_conversation`), one she used in 2 or more of her lines in the last 24 h
(`phrase_day`), or one 3 or more other members used in the last 48 h (`phrase_cast`, 3+
words). The conversation's subject is never her tic (owner's live DM test, 2026-11-16: the
place and the plans he was writing about, said back, were flagged as her tic): the content words
he and other members wrote in this conversation in the last 3 h (each speaker's newest 10
lines), folded so "norway"/"norwegian" and "fast"/"faster" are one word (`_subject`,
`_same_word`). A phrase whose
every content word is in the subject is its topic, for every rule here; a stock entry only when
its content words are his ("thinking about you" said back to his: other members saying it is the
cast's sameness the stock ration is for). A two-word phrase is no candidate when either word is
in the subject. The curated list in `stock_phrases.txt` is rationed on top: once per member in
72 h (`stock_member`) and 3 times a day across the cast (`stock_cast`). Its "!" lines are slop,
a rewrite on every turn; its other lines are common phrases, a rewrite only on a turn not owed
to him: on an owed answer (`Draft.real_answer_due`) one gives no code but `Found.pending`, the
list's own words, named on her next turn (talk/pipeline.py `_phrase_pending`).

A member with a voice sheet (Persona.speech) keeps her own words: a phrase that is one of her
`says` is never her tic for the conversation, day or per-member stock rules, and is rationed
instead (guards/voice.py: about one in three of her lines in a conversation; above that it is
named as `phrase_conversation`). The cast rules still count it. A slop entry ("!" lines) is
never anyone's own word: a say-word inside one exempts nothing, and such a sheet is refused at
import (`says_slop`).

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
CONVERSATION_USES = 2               # her earlier lines in it that make a phrase her tic there
TOPIC_SECONDS, TOPIC_LINES = 3 * 3600, 10  # the subject: his and others' lines in it, each's newest
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
# Clause openers that turn or condition what came before them: a cut across one reverses the
# line (review-326 P2-1). The strip never removes such a clause and never leaves one hanging.
TURNS = frozenset('but though although unless if except until because cause cuz bc so while whereas yet'.split())
_DANGLE = re.compile(r'(^|[.!?]\s+|\n)(?:' + '|'.join(sorted(TURNS | {'and', 'or'})) + r')\b[,\s]*', re.I)
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
    # common stock entries an answer owed to him leaned on: no rewrite now, a hint on her next
    # turn (the entries' own words from the list, never the draft's)
    pending: tuple[str, ...] = field(default=(), repr=False)


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
def _stock_table():
    """(name, alts, slop) for each line of stock_phrases.txt; a leading "!" marks slop."""
    raw = (resources.files('leeroy_mind.guards') / 'stock_phrases.txt').read_text(encoding='utf-8')
    entries = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        is_slop = line.startswith('!')
        alts = tuple((alt.strip().startswith('^'), _words(alt.strip().lstrip('^')))
                     for alt in line.lstrip('!').split('|') if _words(alt.strip().lstrip('^')))
        if alts:
            entries.append((' '.join(alts[0][1]), alts, is_slop))
    return tuple(entries)


@lru_cache(maxsize=1)
def stock() -> tuple[tuple[str, tuple[tuple[bool, tuple[str, ...]], ...]], ...]:
    """The curated list: (name, ((anchored, words), ...)) for each line of stock_phrases.txt."""
    return tuple((name, alts) for name, alts, _ in _stock_table())


@lru_cache(maxsize=1)
def slop() -> frozenset:
    """The names of the slop entries ("!" lines): the roleplay epithets and tics, a rewrite on
    every turn. The others are common phrases: on an answer owed to him only a later hint."""
    return frozenset(name for name, _, is_slop in _stock_table() if is_slop)


def _at(words, phrase, anchored):
    n = len(phrase)
    starts = (0,) if anchored else range(len(words) - n + 1)
    return [i for i in starts if tuple(words[i:i + n]) == phrase]


def _has(clauses, alts):
    return any(_at(c, words, anchored) for c in clauses for anchored, words in alts)


@lru_cache(maxsize=1)
def game_names() -> frozenset:
    """The bosses, raids, dungeons, battlegrounds and arenas the code knows (talk/wow.py), as
    folded word tuples with and without a leading "the": a name typed in lowercase ("the lich
    king") is as much a name as a capitalised one."""
    from leeroy_mind.wow import (ARENAS, BATTLEGROUNDS, BOSS_NAMES, DUNGEON_BOSS_NAMES, DUNGEONS,
                                 ENCOUNTER_MEMBERS, RAID_INSTANCES)
    raw = [*BOSS_NAMES.values(), *DUNGEON_BOSS_NAMES.values(), *RAID_INSTANCES.values(),
           *DUNGEONS.values(), *BATTLEGROUNDS.values(), *ARENAS, *ENCOUNTER_MEMBERS,
           *(n for names in ENCOUNTER_MEMBERS.values() for n in names)]
    found = set()
    for name in raw:
        words = _words(name) if isinstance(name, str) else ()
        if words:
            found.add(words)
            if words[0] == 'the' and len(words) > 1:
                found.add(words[1:])
    return frozenset(found)


def names(reader) -> frozenset:
    """Members', his, the nicknames' and the game's names as folded word tuples (one bounded
    read; an older schema without one of the tables reads none)."""
    rows = reader.rows('names_unavailable',
        "SELECT name FROM (SELECT json_extract(json,'$.character_name') AS name FROM persona_core "
        "WHERE json_valid(json) LIMIT 2000) UNION ALL SELECT value FROM meta WHERE key='human_name' "
        "UNION ALL SELECT name FROM (SELECT term AS name FROM lexicon WHERE kind='nickname' "
        "AND status='active' LIMIT 1000)")
    return game_names() | frozenset(w for row in rows if isinstance(row['name'], str)
                                    and (w := _words(row['name'])))


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
                out.extend((name, alts, (clause[i].i, clause[i].i + len(alt) - 1))
                           for i in _at(words, alt, anchored))
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


def _subject(rows, guid, key, now):
    """The conversation's subject: the content words (outside STOP) of the lines he and other
    members wrote in it within TOPIC_SECONDS, each speaker's newest TOPIC_LINES; and his alone.
    `rows` is newest first."""
    words, his, per = set(), set(), {}
    for row in rows:
        if (row['from_guid'] == guid or now - row['ts'] > TOPIC_SECONDS
                or _key(row['channel'], row['from_guid'], row['to_guid']) != key):
            continue
        n = per.get(row['from_guid'], 0)
        if n >= TOPIC_LINES:
            continue
        per[row['from_guid']] = n + 1
        found = {w for w in _words(strip_wow_markup(row['text'] or '')) if _content(w)}
        words |= found
        if row['origin'] == 'human':
            his |= found
    return frozenset(words), frozenset(his)


def _same_word(a, b):
    """Two folded words as one for the subject: equal, or (both 4+ letters) sharing a prefix of
    at least 4 letters and all but 2 letters of the shorter ("norway"/"norwegian",
    "fast"/"faster", "thinking"/"think")."""
    if a == b:
        return True
    if len(a) < 4 or len(b) < 4:
        return False
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n >= max(4, min(len(a), len(b)) - 2)


def _in_subject(word, subject):
    return word in subject or any(_same_word(word, t) for t in subject)


def _topic(words, subject):
    """A phrase is the conversation's topic when every content word in it is in its subject."""
    content = [w for w in words if _content(w)]
    return bool(content) and all(_in_subject(w, subject) for w in content)


@lru_cache(maxsize=4096)
def _stock_in(words):
    """The names of the stock entries the phrase holds (any spelling of each)."""
    return tuple(name for name, alts in stock()
                 if any(len(alt) <= len(words) and any(words[i:i + len(alt)] == alt
                                                       for i in range(len(words) - len(alt) + 1))
                        for _, alt in alts))


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


def check(reader, draft, now: float, sheet=None) -> Found:
    """The draft's overused phrases. `reader` is review.Reader on a read-only snapshot; replay
    passes `draft.line_id` so a stored line never counts against itself or later lines.
    `sheet`: her voice sheet (guards.voice.Sheet), whose own words are rationed, not cooled."""
    from . import voice
    text = strip_wow_markup(draft.text)
    toks, phrases = candidates(text, names(reader))
    stocked = _stock_spans(toks)
    says = sheet is not None and bool(sheet.says)
    said = says and voice.any_said(toks, sheet)  # one of her own words is in the draft
    if not phrases and not stocked and not said:
        return Found()
    cutoff = draft.line_id if draft.line_id is not None else 2**63 - 1
    rows = reader.rows('phrases_unavailable', 'SELECT from_guid,ts,channel,to_guid,origin,text FROM chat_line '
        "WHERE origin IN ('mind','human') AND ts BETWEEN ? AND ? AND id<? ORDER BY ts DESC,id DESC LIMIT ?",
        (now - max(STOCK_MEMBER_SECONDS, DAY_SECONDS, CAST_SECONDS, STOCK_CAST_SECONDS), now, cutoff,
         LINE_LIMIT))
    own = [r for r in rows if r['from_guid'] == draft.guid and r['origin'] == 'mind'][:OWN_LIMIT]
    cast = [r for r in rows if r['from_guid'] != draft.guid and r['origin'] == 'mind']
    hits = {code: [] for code in CODES}
    # an answer owed to him: a common stock entry is no rewrite, only a hint on her next turn
    # (controller ruling, 2026-11-16); slop stays a rewrite everywhere
    owed, sloppy, pending = bool(draft.real_answer_due), slop(), set()
    key = _key(raw_channel(draft), draft.guid, draft.to_guid)
    # The conversation's subject is everyone's (owner's live DM test, 2026-11-16: the place he
    # was writing about, said back, was flagged as her tic): a phrase whose every content word
    # he or another member used in this conversation is its topic.
    subject, his = _subject(rows, draft.guid, key, now)
    talk = _conversation(own, key, now)[0] if phrases or said else []
    if said:  # her own words, over their ration in this conversation (voice.over_ration)
        hits['phrase_conversation'] += voice.rationed(
            toks, [_words(strip_wow_markup(r['text'] or '')) for r in talk], sheet)
    if phrases:
        seen_talk = [_seen(r['text'])[1] for r in talk]
        day = [_seen(r['text'])[1] for r in own if now - r['ts'] <= DAY_SECONDS]
        wanted = {words for words in phrases if len(words) >= CAST_WORDS
                  and sum(map(_content, words)) >= CAST_CONTENT}
        others = {}
        for row in cast:
            if wanted and now - row['ts'] <= CAST_SECONDS:
                for words in _seen(row['text'])[1] & wanted:
                    others.setdefault(words, set()).add(row['from_guid'])
        for words, spans in phrases.items():
            if _topic(words, subject):
                continue
            if len(words) == 2 and any(_in_subject(w, subject) for w in words):
                continue  # a two-word phrase only of words nobody else here used
            held = _stock_in(words)
            uses = sum(words in grams for grams in seen_talk)
            # rationed above, never cooled; a slop entry is nobody's own word
            own_word = says and voice.hers(words, sheet) and not set(held) & sloppy
            flagged = [code for code, met in (
                ('phrase_conversation', not own_word and (uses >= CONVERSATION_USES
                                                          or bool(uses and held))),
                ('phrase_day', not own_word
                 and sum(words in grams for grams in day) >= DAY_USES),
                ('phrase_cast', len(others.get(words, ())) >= CAST_MEMBERS)) if met]
            if flagged and owed and held and not set(held) & sloppy:
                pending.update(held)  # a common phrase on his answer: a hint next turn
                continue
            for code in flagged:
                hits[code] += spans
    if stocked:
        mine = [_seen(r['text'])[0] for r in own if now - r['ts'] <= STOCK_MEMBER_SECONDS]
        recent = [_seen(r['text'])[0] for r in (*own, *cast) if now - r['ts'] <= STOCK_CAST_SECONDS]
        for name, alts, span in stocked:
            if _topic(tuple(t.norm for t in toks[span[0]:span[1] + 1]), his):
                continue  # his own words said back ("thinking about you" to his)
            own_word = says and name not in sloppy and voice.hers(
                tuple(t.norm for t in toks[span[0]:span[1] + 1]), sheet)
            flagged = [code for code, met in (
                ('stock_member', not own_word and any(_has(clauses, alts) for clauses in mine)),
                ('stock_cast', sum(_has(clauses, alts) for clauses in recent) >= STOCK_CAST_USES))
                if met]
            if flagged and owed and name not in sloppy:
                pending.add(name)  # a common phrase on his answer: a hint next turn
                continue
            for code in flagged:
                hits[code].append(span)
    codes = tuple(code for code in CODES if hits[code])
    later = tuple(sorted(pending))[:MAX_NAMED]
    if not codes:
        return Found(pending=later)
    named = _surface(text, toks, [span for code in codes for span in hits[code]])
    return Found(codes, tuple(named[:MAX_NAMED]), later)


def hint(phrases) -> str:
    """The retry's instruction naming her own words; empty when there are none."""
    quoted = [f"'{p}'" for p in dict.fromkeys(phrases) if p][:MAX_NAMED]
    if not quoted:
        return ''
    said = quoted[0] if len(quoted) == 1 else ', '.join(quoted[:-1]) + ' or ' + quoted[-1]
    return f"Don't say {said} again; say it your own way or not at all."


@lru_cache(maxsize=256)
def _anchored(words):
    """A named phrase that is only a clause-opener stock entry ("^at least") counts where a
    clause opens with it; "at least ten" mid-sentence is never the entry."""
    kinds = {anchored for _, alts in stock() for anchored, alt in alts if alt == words}
    return kinds == {True}


def _occurrences(toks, phrase):
    words = _words(phrase)
    return [(c[i].i, c[i].i + len(words) - 1) for c in _clauses(toks)
            for i in _at([t.norm for t in c], words, _anchored(words))] if words else []


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
    sentence keeps two or more words without it. None when the clause opens with a word of
    TURNS ("..., but i'd rather stay in" cut to "that sounds really fun" says the opposite) or
    the clause after it does (the rest would hang on its "but")."""
    a = toks[first]
    if any(t.clause != a.clause for t in toks[first:last + 1]):
        return None
    same = [t for t in toks if t.sentence == a.sentence]
    if sum(t.clause != a.clause for t in same) < 2:
        return None
    mine = [t for t in same if t.clause == a.clause]
    if mine[0].norm in TURNS:
        return None
    before = [t for t in same if t.clause < a.clause]
    if before:  # a later clause: from the end of the clause before it
        return before[-1].end, mine[-1].end
    # The opening clause: cut to the start of the next; a "but" the next clause opens with is
    # left for the dangle trim ("you're trouble, but yes, i'll come" -> "yes, i'll come").
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


def _asks(text, start, stop):
    return bool(re.search(r'\?["\')]*\s*$', text[start:stop]))


def strip(text: str, phrases, *, min_words: int = 3, epithet_only: bool = False) -> str | None:
    """The text without the named phrases: a closing epithet ("..., you absolute menace") is
    cut, else the phrase's comma clause (never one across a word of TURNS), else its whole
    sentence; a conjunction the cut leaves hanging at the front of a sentence goes with it. A
    cut that takes a sentence's question mark takes the question: the rest ends in a full stop,
    never a stray "?". With `epithet_only` (an answer owed to him) only the epithet cut is made,
    so the answer keeps its meaning or goes as written. None when fewer than `min_words` words
    would survive or a phrase is still there."""
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
            if epithet_only:
                return None
            a, b = _clause_span(toks, first, last) or _sentence_span(text, toks[first])
            _, stop = _sentence_span(text, toks[first])
            if b < stop and _asks(text, b, stop) and not tokens(text[b:stop]):
                b = stop  # the clause carried its sentence's question mark: take it too
            head = text[:a]
            if b == stop and head.strip() and head.rstrip()[-1] not in '.!?' and _asks(text, a, b):
                head = head.rstrip() + '.'  # the question went with its clause: a statement stays
            text = head + ' ' + text[b:]
        text = _DANGLE.sub(r'\1', _tidy(text))
    toks = tokens(text)
    if len(toks) < min_words or any(_occurrences(toks, p) for p in phrases):
        return None
    return text
