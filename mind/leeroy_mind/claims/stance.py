"""Conservative English cues shared by merging and the model-free check."""
import re

from leeroy_mind.text import normalize_words

# A deictic subject ("that fight") needs semantic support. 0.65 is a deliberately
# permissive candidate gate, chosen on invented paraphrases, not a calibrated probability.
T_SUBJECT = 0.65
# Negation needs most of the same predicate, not merely a shared topic word.
# 0.60 admits a changed pronoun/auxiliary in the small synthetic regression set.
T_NEGATION = 0.60
_POS = {'love', 'loves', 'like', 'likes', 'enjoy', 'enjoys', 'prefer', 'prefers',
        'adore', 'adores', 'favourite', 'favorite'}
_NEG = {'hate', 'hates', 'dislike', 'dislikes', 'loathe', 'loathes', 'detest', 'detests',
        'dread', 'dreads'}
_STOP = set('i me my she her he his they their we our a an the that this it is am are '
            'was were be been do does did have has had at in on to of for and but '
            'really very still now anymore not never no longer'.split()) | _POS | _NEG
_VAGUE = {'fight', 'boss', 'raid', 'thing', 'job', 'place', 'work'}
_NEGATION = re.compile(r'\b(?:not|never|no longer)\b')
_SLOTS = (
    ('job', re.compile(r'\bwork(?:s|ing)?\s+(?:at|for)\s+(?:the\s+)?(.+)')),
    ('home', re.compile(r'\blive(?:s)?\s+in\s+(?:the\s+)?(.+)')),
)


def words(text: str) -> str:
    text = text.casefold().replace('\u2019', "'")
    text = re.sub(r"\b(?:don't|doesn't|didn't|isn't|aren't|wasn't|weren't|can't|won't|haven't|hasn't)\b",
                  ' not ', text)
    return normalize_words(text)


def polarity(text: str) -> int:
    tokens = words(text).split()
    signs = set()
    for i, word in enumerate(tokens):
        if word not in _POS | _NEG:
            continue
        sign = 1 if word in _POS else -1
        if _NEGATION.search(' '.join(tokens[max(0, i - 3):i])):
            sign *= -1
        signs.add(sign)
    return next(iter(signs)) if len(signs) == 1 else 0


def subject_matches(subject: str, old: str, new: str, score: float) -> bool:
    query = words(new)
    key = ' '.join(w for w in words(subject).split() if w not in _STOP)
    if key and re.search(rf'(?<!\w){re.escape(key)}(?!\w)', query):
        return True
    if any(slot in key.split() and pattern.search(words(old)) and pattern.search(query)
           for slot, pattern in _SLOTS):
        return True
    anchors = (set(words(subject).split()) | set(words(old).split())) - _STOP - _VAGUE
    if anchors & set(query.split()):
        return True
    # Without a named anchor, allow only an explicit deictic reference, supported
    # by the embedding. Unrelated "I hate coffee" never matches a Saurfang claim.
    return score >= T_SUBJECT and bool(re.search(r'\b(?:that|this|it)\b', query))


def _slot(text: str, pattern) -> str | None:
    match = pattern.search(words(text))
    if not match:
        return None
    value = re.split(r'\b(?:and|but|now|since|today|on|during|every|because)\b', match[1])[0].strip()
    return value or None


def reversal(kind: str, old: str, new: str, old_polarity: int) -> str | None:
    stance = polarity(new)
    if kind == 'opinion' and old_polarity and stance:
        return 'polarity' if stance == -old_polarity else None
    before, after = words(old), words(new)
    if kind == 'fact':
        for slot, pattern in _SLOTS:
            a, b = _slot(before, pattern), _slot(after, pattern)
            if (a and b and not (a.endswith(b) or b.endswith(a))
                    and not (_NEGATION.search(before) or _NEGATION.search(after))):
                return 'replaced_fact'
            if a and slot == 'job' and re.search(r'\b(?:quit|left|lost)\b.*\b(?:job|bakery|work)\b', after):
                return 'replaced_fact'
            if a and slot == 'home' and re.search(r'\bmoved (?:out|away)\b', after):
                return 'replaced_fact'
    if kind == 'plan' and re.search(r'\b(?:cancelled|canceled|abandoned)\b', after):
        return 'cancelled_plan'
    a = set(before.split()) - _STOP
    b = set(after.split()) - _STOP
    # Inflection is enough for "works" / "work", without importing an NLP stack.
    a, b = ({w.removesuffix('s') for w in v} for v in (a, b))
    overlap = len(a & b) / max(1, len(a | b))
    if (a & b and overlap >= T_NEGATION
            and bool(_NEGATION.search(before)) != bool(_NEGATION.search(after))):
        return 'negation'
    return None


def assertions(text: str) -> list[str]:
    """Ignore questions, quotations, hypotheticals and reports of someone else's stance.

    This deliberately misses sarcasm and complicated clauses. Returning a candidate
    here is not a semantic judgement; judge() is the optional confirmation step.
    """
    text = re.sub(r'"[^"\n]*"|\u201c[^\u201d\n]*\u201d', '', text)
    out = []
    for clause in re.split(r'[.!;\n]|\bbut\b', text):
        value = words(clause)
        if (not value or '?' in clause
                or re.search(r'\b(?:if|maybe|perhaps|might|would|used to|said|says|thinks|joking)\b', value)):
            continue
        if re.match(r'^(?:she|he|they|you|[a-z]+ (?:loves|hates|likes|works|lives))\b', value):
            if not value.startswith('i '):
                continue
        out.append(clause.strip())
    return out
