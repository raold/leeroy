"""His line said back in her opening (evening DM audit, 2026-11-16: 10 of 38 answers to him).

The echo guard (review.echoes, `echo_parent`) holds a reply that copies the message it answers:
the whole of it, or a long run of its words. It never saw the shorter ways a line comes back:
his short line repeated as her first words ("raid friday? raid friday."), his words quoted back
("'lol sure' - that's all i get?"), or what he said restated to him ("you forgot my main??" to
his "i forgot"). `echo_opener` finds those in her opening, her first OPENING_WORDS words, against
the message she answers:

1. Repeat: one of her leading clauses, its lead-in ("oh", "yeah", "wait") and closing filler
   ("of course", "lol") aside, is a run of two or more words of one of his clauses holding every
   content word of it. One word said back ("fish??") is a reaction; a greeting or thanks said
   back ("sleep well", "morning sunshine"), one said as her own ("i love you", "lets do this
   then") or given back to him ("... too") answers him. His question said back as a statement
   opening her reply ("coming to the raid, yes") is its answer.
2. Quote: a quoted span in her opening that is his whole line, or two or more of his words in
   a row.
3. Restated: "you" and, within RESTATE_REACH words in its clause, a word he said within
   RESTATE_REACH words after "i" ("i logged in from my phone" -> "you logged in from your phone
   to chat?"), or "you" asking, saying or telling and RESTATE_SHARED content words of one of his
   sentences after it ("you're asking to farm herbs again?"). When he asked her something, only
   as her first words: after her answer it is a remark on it. In her first sentence otherwise
   (an ellipsis does not end it).

An echo opener is a rewrite (review.HINTS). `cut` takes the echoing clauses out of a second
draft that still has them (talk.pipeline._without_phrases) when what is left stands alone.
Model-free and read-free; nothing here logs.
"""
import re

from leeroy_mind.text import strip_wow_markup

from . import phrases

OPENING_WORDS = 40       # her opening: the first sentence, at most this many of its words
RESTATE_REACH = 4        # words after "i" (his) and "you" (hers) that say what was done
RESTATE_SHARED = 2       # content words of his sentence a "you're asking ..." clause repeats
MIN_LEFT = 3             # words a cut answer keeps, or it is not cut

# What an opener may say before or after his words without being an answer of its own.
LEAD = frozenset('''oh ooh ohh ah ahh aw aww awww wait so well um uh hmm hm mm mmm mhm yeah yes
yep yup ya yea ok okay k kk lol lmao haha hahaha hehe omg wow huh eh hey hi and but babe baby hun
love lover pfft ha hah ooo oooh'''.split())
TAIL = frozenset('''of course then huh eh lol lmao haha hahaha hehe again already right yeah yes
yep ok okay really seriously babe baby hun love lover mate aye innit is it now'''.split())
# His words that, said back, are a greeting, thanks or a declaration: an answer, not an echo.
SOCIAL = frozenset('''hi hello hey hiya yo morning evening night goodnight gn gm nite bye cya later
thanks thank ty thx cheers love miss happy birthday bday congrats grats gratz gz gg wp welcome sorry
merry christmas xmas new year easter halloween hug hugs kiss kisses xx xo sweet dreams sleep
well safe travels luck care fun enjoy'''.split())
_GREETING = frozenset('hi hello hey hiya heya yo morning evening night goodnight gn gm nite bye '
                      'cya'.split())
# A reply that gives his words back to him ("you drive me wild too") answers in kind.
RECIPROCAL = frozenset({'too', 'also', 'more', 'back'})
_I = frozenset({'i', 'im', 'ive'})  # "i'd" and "i'll" say what he would do, not what he did
# A clause of his said back with "i", "you" or "we" in it is said as her own ("i love you", "lets
# do this then"), never a bare repeat.
_PERSONS = frozenset('i im ive id ill you youre youve youd youll your yours we were weve well lets '
                     'us our'.split())
# What he wants, means or is able to do is no act of his to say back ("i want to cook for you":
# "you cook for me already" answers it).
_MODAL = frozenset('''want wanna would could will gonna going need needs have hope plan can might
should may must wish gotta'''.split())
_YOU = frozenset({'you', 'youre', 'youve', 'youd', 'youll'})
_SUBJECTS = frozenset('''i im ive id ill you youre youve youd youll he hes she shes it its we were
weve they theyre theyve'''.split())
# Helpers and adverbs between "you" and what it says he did ("you're actually painting").
_BY = frozenset('''actually really so just literally still totally honestly seriously all even
always already also are were was is am have has had do did been be being not dont didnt arent
aint kinda legit basically probably definitely finally like keep keeps kept'''.split())
# Words after which a bare "you" is who does it ("and you logged in", "did you forget"), not
# whom it is done to ("i want u to ...").
_BEFORE_SUBJECT = frozenset('''and but so then now cause cuz because bc when if that oh lol lmao
haha wait yeah ok okay wow omg well while since though tho where why how what who did do does are
arent were werent have havent didnt dont cant can could would will wont actually just literally
honestly really seriously still also even said say says saying think thought guess bet reckon
swear'''.split())
_REPORT = frozenset('''ask asks asked asking say says said saying tell tells told telling want
wants wanting wanted wanna mean means meant meaning'''.split())
# Past forms a plain stem does not reach ("forgotten" is "forget").
_IRREGULAR = {
    'forgot': 'forget', 'forgotten': 'forget', 'made': 'make', 'went': 'go', 'gone': 'go',
    'said': 'say', 'told': 'tell', 'got': 'get', 'gotten': 'get', 'took': 'take',
    'taken': 'take', 'bought': 'buy', 'brought': 'bring', 'thought': 'think', 'ate': 'eat',
    'eaten': 'eat', 'came': 'come', 'saw': 'see', 'seen': 'see', 'gave': 'give',
    'given': 'give', 'left': 'leave', 'felt': 'feel', 'lost': 'lose', 'won': 'win',
    'sent': 'send', 'spent': 'spend', 'slept': 'sleep', 'woke': 'wake', 'woken': 'wake',
    'wrote': 'write', 'written': 'write', 'drove': 'drive', 'driven': 'drive', 'fell': 'fall',
    'fallen': 'fall', 'ran': 'run', 'sat': 'sit', 'met': 'meet', 'broke': 'break',
    'broken': 'break', 'kept': 'keep', 'found': 'find', 'held': 'hold', 'paid': 'pay',
    'stood': 'stand', 'heard': 'hear', 'flew': 'fly', 'flown': 'fly', 'drank': 'drink',
    'drunk': 'drink', 'sang': 'sing', 'sung': 'sing', 'swam': 'swim', 'wore': 'wear',
    'worn': 'wear', 'chose': 'choose', 'chosen': 'choose', 'began': 'begin', 'begun': 'begin',
    'fought': 'fight', 'caught': 'catch', 'taught': 'teach', 'sold': 'sell', 'built': 'build',
    'hidden': 'hide', 'rode': 'ride', 'ridden': 'ride', 'stole': 'steal', 'stolen': 'steal',
    'threw': 'throw', 'thrown': 'throw', 'grew': 'grow', 'grown': 'grow', 'knew': 'know',
    'known': 'know'}
_QUOTES = re.compile(r'"([^"\n]{1,160})"|“([^”\n]{1,160})”'
                     r'|(?<![\w\'’])[\'‘]([^\'‘’\n]{1,120})[\'’](?![\w\'’])')
# He asked for the words, so saying them is the answer (as review._ASKED).
_ASKED = re.compile(r"(?:^|[.!?,:;]\s*|\b(?:now|just|please|pls|plz|go on|go ahead|can you|"
                    r"could you|will you|wanna)\s+)(?:repeat|say)\b(?:\s+(?:after me|it|that|"
                    r"this|back|the words)\b|\s*[:\"“])", re.IGNORECASE)
_LEADING_MARKS = re.compile(r'^[\s\-–—,.;:!?"“”\'‘’)]+')
_TRAILING_OFF = re.compile(r'\s*(?:\.{2,}|…)\s*')
# A quote mark a cut left on its own.
_LONE_MARK = re.compile(r'(?:(?<=\s)|^)[?!.]*["“”‘’](?=\s|$)')


def _stem(word: str) -> str:
    if word in _IRREGULAR:
        return _IRREGULAR[word]
    for end in ('ing', 'ed', 'es', 's'):
        if word.endswith(end) and len(word) - len(end) >= 3:
            word = word[:-len(end)]
            if len(word) > 3 and word[-1] == word[-2] and word[-1] not in 'lsz':
                word = word[:-1]
            break
    return word[:-1] if word.endswith('e') and len(word) > 3 else word


_SOCIAL_STEMS = frozenset(map(_stem, SOCIAL))  # "missed" is "miss"


def _meant(word: str) -> bool:
    """A word that carries what was said: a content word (outside phrases.STOP) or a number."""
    return word.isdigit() or phrases._content(word)


def _clauses(toks):
    out = {}
    for t in toks:
        out.setdefault(t.clause, []).append(t)
    return list(out.values())


def _trim(words):
    words = list(words)
    while words and words[0] in LEAD:
        words.pop(0)
    tail = []
    while words and words[-1] in TAIL:
        tail.insert(0, words.pop())
    return words, tail


def _run(words, clause) -> int:
    """The length of the longest prefix of `words` found as a run in `clause`."""
    best = 0
    for start in range(len(clause)):
        n = 0
        while n < len(words) and start + n < len(clause) and words[n] == clause[start + n]:
            n += 1
        best = max(best, n)
    return best


def _asks(plain, toks, c) -> bool:
    """Whether clause `c` (tokens of `toks` over `plain`) ends on a question mark."""
    following = next((t.start for t in toks if t.i > c[-1].i), len(plain))
    return '?' in plain[c[-1].end:following]


_ASKING = frozenset('''can could would will do does did are is am was were have has had should
what how when where why who which whats hows wanna'''.split())


class _His:
    """The message she answers, read once: its words, its clauses (and which ask), the content
    words of each sentence, whether it asks her anything and what he said he did after "i"."""

    def __init__(self, parent: str):
        plain = strip_wow_markup(parent or '')
        toks = phrases.tokens(plain)
        self.words = [t.norm for t in toks]
        # a greeting and what he calls her ("morning sunshine") said back is a greeting
        self.clauses = [(_trim([t.norm for t in c])[0], _asks(plain, toks, c))
                        for c in _clauses(toks) if c[0].norm not in _GREETING]
        sentences = {}
        for t in toks:
            sentences.setdefault(t.sentence, []).append(t.norm)
        self.sentences = [{_stem(w) for w in s if _meant(w)} for s in sentences.values()]
        self.asks = (any(asks for _, asks in self.clauses)
                     or any(s[0] in _ASKING for s in sentences.values()))
        # what he did and to what: the first two words that say it within RESTATE_REACH of his
        # "i" ("i logged in from my phone": logged, phone; "i sent fresh flowers": sent, fresh)
        self.acts = set()
        for c in _clauses(toks):
            for i, t in enumerate(c):
                if t.norm not in _I or _MODAL & {u.norm for u in c[i + 1:i + 3]}:
                    continue
                said = []
                for u in c[i + 1:i + 1 + RESTATE_REACH]:
                    if u.norm in _SUBJECTS:
                        break  # "i see youre busy": what she is, not what he did
                    if _meant(u.norm) and u.norm not in _REPORT:
                        said.append(_stem(u.norm))
                self.acts.update(s for s in said[:2] if s not in _SOCIAL_STEMS)

    def repeated(self, clause_words, asks: bool, *, again: bool = False) -> bool:
        """Rule 1 on one of her clauses (`asks`: it ends on a question mark). His question
        said back as a statement opening her reply ("coming to the raid, yes") is its answer;
        once she has said it back (`again`), saying it once more is not ("raid friday? raid
        friday.")."""
        words, tail = _trim(clause_words)
        if len(words) < 2 or RECIPROCAL & {*tail, words[-1]} or _PERSONS & set(words):
            return False  # "fish??" is a reaction; "i love you", "lets do this" said back answer
        for core, his_asks in self.clauses:
            need = {w for w in core if _meant(w)}
            if (need and not need <= SOCIAL and (asks or again or not his_asks)
                    and _run(words, core) == len(words) and need <= set(words)):
                return True
        return False

    def quoted(self, text: str) -> bool:
        """Rule 2: a quoted span of `text` is his whole line, or 2+ of his words in a row."""
        for m in _QUOTES.finditer(text):
            said = [t.norm for t in phrases.tokens(next(g for g in m.groups() if g is not None))]
            if said and (said == self.words or len(said) >= 2 and any(
                    _run(said, c) == len(said) for c in (self.words, *(w for w, _ in self.clauses)))):
                return True
        return False

    def restated(self, clause_words, *, first: bool, opening: bool) -> bool:
        """Rule 3 on one of her clauses (`first`: her first clause; `opening`: in her first
        sentence). What he said he did, said back to him, counts in her first sentence when he
        asked her nothing; when he did, only as the first words of her reply (after an answer it
        is a remark on it: "resto shaman lol you forgot my whole life"). Telling him what
        he asks or says counts anywhere in her opening."""
        words, tail = _trim(clause_words)
        if not words or RECIPROCAL & {*tail, words[-1]}:
            return False
        start = len(clause_words) - len(words) - len(tail)  # her lead-in words before it
        at = (range(start, start + 1) if self.asks and first
              else range(len(clause_words)) if opening and not self.asks else range(0))
        if words[0].endswith('ing') and _stem(words[0]) in self.acts and start in at:
            return True  # "logging in from a beach to chat": his act, its "you" left out
        for i, w in enumerate(clause_words):
            if w not in _YOU:
                continue
            # the word "you" does, past a helper or an adverb ("you're actually painting"): "you
            # had me at pizza" says nothing he did, nor does "i want u to" (you as an object)
            does = next((x for x in clause_words[i + 1:i + 1 + RESTATE_REACH] if x not in _BY), '')
            if (i in at and _stem(does) in self.acts
                    and (w != 'you' or i == start or clause_words[i - 1] in _BEFORE_SUBJECT)):
                return True
            if any(x in _REPORT for x in clause_words[i + 1:i + 3]):
                rest = {_stem(x) for x in clause_words[i + 1:i + 11]
                        if _meant(x) and x not in _REPORT}
                if any(len(rest & said) >= RESTATE_SHARED for said in self.sentences):
                    return True
        return False


def _found(plain: str, parent: str):
    """(her tokens, the first part of her opening that says `parent` back or None): ("lead",
    first, last) for her leading clauses that repeat or quote him, ("clause", first, last) for
    a later clause that does or restates him, ("quote", start, end) for a quote of his words
    inside a longer clause (character offsets)."""
    toks = phrases.tokens(plain)
    if not toks or not (parent or '').strip() or _ASKED.search(parent):
        return toks, None
    his = _His(parent)
    if not his.words:
        return toks, None
    opening = toks[:OPENING_WORDS]
    clauses = _clauses(opening)
    # her first sentence: an ellipsis ("arent you... logging in from a beach") does not end it
    sentence = opening[0].sentence
    for a, b in zip(opening, opening[1:]):
        if b.sentence != sentence and not _TRAILING_OFF.fullmatch(plain[a.end:b.start]):
            break
        sentence = b.sentence
    first_sentence = {t.i for t in opening if t.sentence <= sentence}

    def window(c):  # a clause with the marks around it, so a quote's own marks are in
        return plain[max(0, c[0].start - 1):c[-1].end + 1]
    lead = []
    for c in clauses:
        if (his.repeated([t.norm for t in c], _asks(plain, toks, c), again=bool(lead))
                or his.quoted(window(c))):
            lead.append(c)
        else:
            break
    if lead:
        return toks, ('lead', lead[0][0].i, lead[-1][-1].i)
    for n, c in enumerate(clauses):
        if his.quoted(window(c)) or his.restated([t.norm for t in c], first=n == 0,
                                                 opening=c[0].i in first_sentence):
            return toks, ('clause', c[0].i, c[-1].i)
    end = opening[-1].end + 1
    for m in _QUOTES.finditer(plain[:end]):
        if his.quoted(m.group(0)):
            return toks, ('quote', m.start(), m.end())
    return toks, None


def echo_opener(text: str, parent: str) -> bool:
    """True when her opening says `parent`, the message she answers, back (the rules above)."""
    return _found(strip_wow_markup(text or ''), parent)[1] is not None


QUESTION_ECHO_WORDS = 4  # his line this short, said back as a question, is cut from her opening
_ECHO_WORD = re.compile(r"[a-z0-9']+")


def without_question_echo(text: str, parent: str) -> str:
    """`text` without an opening that only says his short line back as a question ("brb?",
    "be nice to him? you know im no angel"): his whole line of at most QUESTION_ECHO_WORDS words,
    a lead-in aside ("oh", "wait"), then a question mark, when MIN_LEFT words or more follow.
    One word said back and nothing else ("fish??") is a reaction and stays, as does a line he
    asked her to repeat. The self-play audit, 2026-11-17: 5 of 8 echoes were this shape, under
    echo_opener's two-word floor."""
    plain = text or ''
    his = _ECHO_WORD.findall((parent or '').casefold().replace('’', "'"))
    if not his or len(his) > QUESTION_ECHO_WORDS or _ASKED.search(parent or ''):
        return text
    words = list(re.finditer(r"[A-Za-z0-9'’]+", plain))
    i = 0
    while i < len(words) and words[i].group(0).casefold() in LEAD and (
            words[i].group(0).casefold() != his[0]):
        i += 1
    said = [w.group(0).casefold().replace('’', "'") for w in words[i:i + len(his)]]
    if said != his:
        return text
    rest = plain[words[i + len(his) - 1].end():]
    mark = re.match(r"\s*\?[?!]*", rest)
    if mark is None:
        return text
    left = rest[mark.end():].lstrip(" \t,.-")
    if len(left.split()) < MIN_LEFT:
        return text
    return left


def cut(text: str, parent: str) -> str | None:
    """`text` without what its opening says back of `parent`: leading clauses cut to the clause
    after them, a later clause cut as the phrase cooldown cuts one (phrases._clause_span: never
    across a turn such as "but", else its sentence), a quote of his words inside a longer
    clause said as "that". None when fewer than MIN_LEFT words would be left or it still says
    him back."""
    out = strip_wow_markup(text or '')
    for _ in range(4):
        toks, found = _found(out, parent)
        if found is None:
            break
        kind, first, last = found
        if kind == 'quote':
            out = out[:first] + 'that' + out[last:]
        elif kind == 'lead':
            following = next((t for t in toks if t.clause > toks[last].clause), None)
            out = out[following.start:] if following is not None else ''
        else:
            a, b = (phrases._clause_span(toks, first, last)
                    or phrases._sentence_span(out, toks[first]))
            out = out[:a] + ' ' + out[b:]
        out = _LONE_MARK.sub('', out)
        out = _LEADING_MARKS.sub('', phrases._DANGLE.sub(r'\1', phrases._tidy(out)))
    else:
        return None
    if len(phrases.tokens(out)) < MIN_LEFT or echo_opener(out, parent):
        return None
    if text[:1].isupper() and out[:1].islower():
        out = out[:1].upper() + out[1:]
    return out
