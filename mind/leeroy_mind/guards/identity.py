"""A member speaking with the other sex's body or role in the first person (identity audit,
2026-11-15: in explicit pair talk a woman answering his first-person lines took his anatomy and
his role, "i'm not some nice gentleman").

The lists are narrow on purpose. They read only what she says about herself ("my", "i'm", "as a
guy, i"); her words about him ("your ...", "you're such a gentleman") never count, and neither
does correcting a misgendering ("i'm not a guy lol"). A man is held to the mirror list.
`without_slips` keeps the sentences that do not slip, for a short owed answer."""
import re

_I_AM = r"(?:\bi'm|\bi am|\bim)"
_QUALITY = r"(?:proper|real|good|regular|nice|old|lucky|single|true|perfect|decent)"
# A noun's everyday company, where it says nothing about who she is: "a dad joke", "a mom friend".
_COMPANY = (r"(?!\s+(?:jokes?|friends?|energy|mode|vibes?|bod|humou?r|moves?|brain|life|"
            r"stuff|thing|group|chat))")
# Who speaks first-person: "i", "i'll", "i'd", "i'm", "me", a few words before the verb.
_ME = r"\b(?:i|me)(?:'ll|'d|'m)?(?:\s+\w+){0,3}\s+"


def _role(nouns: str, yours: str) -> str:
    """"i'm a/an/some [quality] NOUN" (never "i'm such a dad", self-mockery), "i'm your NOUN" only
    for `yours` ("i'm your man for the job" and "your guy" are idioms), and with "not" only
    with a quality word, where it still says she is one ("i'm not some nice gentleman"), never
    "i'm not a guy" or "i'm not some guy you met in trade"."""
    return "|".join((
        _I_AM + rf" (?:just |really |still |only )?(?:a|an|some) (?:{_QUALITY} )?(?:{nouns})s?\b"
        + _COMPANY,
        _I_AM + rf" not (?:a|an|some) {_QUALITY} (?:{nouns})s?\b" + _COMPANY,
        _I_AM + rf" (?:just |really |still |only )?(?:your|ur) (?:{_QUALITY} )?(?:{yours})\b"
        + _COMPANY))


# A woman speaking as a man.
MALE_SELF = re.compile("|".join((
    r"\bmy (?:(?:big|hard|thick|throbbing|aching|swollen|own|fat) )?(?:cock|dick|penis|balls|"
    r"ballsack|nutsack|testicles|foreskin|boner|erection|hard-?on|bulge|dong|knob)\b",
    _I_AM + r" (?:so |rock |getting |already )?hard (?:for (?:you|u)|right now|just thinking|"
    r"thinking about)\b",
    _role(r"gentleman|gentlemen|guy|dude|bloke|man|lad|boyfriend|husband|dad|father|gent|fella|"
          r"chap|daddy", r"boyfriend|husband|daddy|gentleman"),
    r"\bas an? (?:guy|man|dude|bloke|lad|gentleman|boy)(?:,| i\b| myself\b)",
    r"\bbury(?:ing)? myself (?:\w+ ){0,2}(?:in|inside) (?:you|ya|u)\b",
    _ME + r"(?:cum|cumming|finish|finishing|unload|unloading) (?:deep )?(?:inside|in) "
    r"(?:you|ya|u)\b(?! know)",
    _ME + r"(?:knock you up|get you pregnant|breed you)\b",
)))
# A man speaking as a woman.
FEMALE_SELF = re.compile("|".join((
    r"\bmy (?:(?:little|wet|tight|own|soaking|dripping) )?(?:pussy(?!\s*cats?)|vagina|clit|"
    r"clitoris|cunt|tits|titties|boobs|breasts)\b",
    _role(r"lady|woman|women|wife|girlfriend|mum|mom|mother|mommy|mummy|lass|gal",
          r"wife|girlfriend|mommy|mummy|lady"),
    r"\bas an? (?:woman|lady|girl|mum|mom|mother)(?:,| i\b| myself\b)",
    _I_AM + r" pregnant\b",
    r"\bwhen i was pregnant\b",
)))
LISTS = {"female": MALE_SELF, "male": FEMALE_SELF}
# The guard's codes, by the speaker's gender: a woman who wrote as a man, a man as a woman.
CODES = {"female": "self_male_slip", "male": "self_female_slip"}
_SENTENCE = re.compile(r"(?<=[.!?])\s+|\n+")
# Someone else's words: a quotation, and a sentence that reports what someone said.
_QUOTED = re.compile(r"\"[^\"\n]*\"|“[^”\n]*”")
_REPORTED = re.compile(r"\b(?:he|she|they|you|u|him|her)\s+(?:said|says|goes|went|wrote|texted|"
                       r"told me|tells me|was like|is like|asked)\b")


def _fold(text: str) -> str:
    return " ".join(str(text or "").casefold().replace("’", "'").replace("ʼ", "'").split())


def slips(text: str, gender: str) -> bool:
    """True when `text`, said by a member of player gender `gender`, speaks with the other
    sex's body or role in the first person. Quoted words and a sentence reporting what someone
    said are someone else's, not hers."""
    rx = LISTS.get(gender)
    if rx is None:
        return False
    for sentence in _SENTENCE.split(_QUOTED.sub(" ", str(text or ""))):
        own = _fold(sentence)
        if own and not _REPORTED.search(own) and rx.search(own):
            return True
    return False


def code_for(gender: str) -> str | None:
    return CODES.get(gender)


def without_slips(text: str, gender: str) -> str:
    """`text` with each sentence that slips left out, the rest joined by a space; "" when every
    sentence slips."""
    kept = [s.strip() for s in _SENTENCE.split(text or "") if s.strip() and not slips(s, gender)]
    return " ".join(kept)


__all__ = ["CODES", "FEMALE_SELF", "LISTS", "MALE_SELF", "code_for", "slips", "without_slips"]
