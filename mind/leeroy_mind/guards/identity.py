"""A member speaking with the other sex's body or role in the first person (identity audit,
2026-11-15: in explicit pair talk a woman answering his first-person lines took his anatomy and
his role, "i'm not some nice gentleman").

The lists are narrow on purpose. They read only what she says about herself ("my", "i'm", "as a
guy, i"); her words about him ("your ...", "you're such a gentleman") never count, and neither
does correcting a misgendering ("i'm not a guy lol"). A man is held to the mirror list.
`without_slips` keeps the sentences that do not slip, for a short owed answer."""
import re

_I_AM = r"(?:\bi'm|\bi am|\bim)"
_QUALITY = (r"(?:proper|real|good|regular|nice|old|lucky|single|true|perfect|decent|total|"
            r"complete)")


def _role(nouns: str) -> str:
    """"i'm a/some/your [quality] NOUN", and with "not" only where it still says she is one
    ("i'm not some nice gentleman"), never the bare correction "i'm not a guy"."""
    return (_I_AM + r" (?:just |really |such |still |only |more of )?(?:a|an|some|your|ur) "
            rf"(?:{_QUALITY} )?(?:{nouns})s?\b"
            r"|" + _I_AM + rf" not (?:(?:a|an|your|ur) {_QUALITY}|some(?: {_QUALITY})?) "
            rf"(?:{nouns})s?\b")


# A woman speaking as a man.
MALE_SELF = re.compile("|".join((
    r"\bmy (?:(?:big|hard|thick|throbbing|aching|swollen|own|fat) )?(?:cock|dick|penis|balls|"
    r"ballsack|nutsack|testicles|foreskin|boner|erection|hard-?on|bulge|dong|knob)\b",
    _I_AM + r" (?:so |rock |getting |already )?hard (?:for (?:you|u)|right now|just thinking|"
    r"thinking about)\b",
    _role(r"gentleman|gentlemen|guy|dude|bloke|man|lad|boyfriend|husband|dad|father|gent|fella|"
          r"chap|daddy"),
    r"\bas an? (?:guy|man|dude|bloke|lad|gentleman|boy)(?:,| i\b| myself\b)",
    r"\bbury(?:ing)? myself (?:\w+ ){0,2}(?:in|inside) (?:you|ya|u)\b",
    r"\b(?:cum|cumming|finish|finishing|unload|unloading) (?:deep )?(?:inside|in) (?:you|ya|u)\b",
    r"\b(?:knock you up|get you pregnant|breed you)\b",
)))
# A man speaking as a woman.
FEMALE_SELF = re.compile("|".join((
    r"\bmy (?:(?:little|wet|tight|own|soaking|dripping) )?(?:pussy|vagina|clit|clitoris|cunt|tits|"
    r"titties|boobs|breasts)\b",
    _role(r"lady|woman|women|wife|girlfriend|mum|mom|mother|mommy|mummy|lass|gal"),
    r"\bas an? (?:woman|lady|girl|mum|mom|mother)(?:,| i\b| myself\b)",
    _I_AM + r" pregnant\b",
    r"\bwhen i was pregnant\b",
)))
LISTS = {"female": MALE_SELF, "male": FEMALE_SELF}
# The guard's codes, by the speaker's gender: a woman who wrote as a man, a man as a woman.
CODES = {"female": "self_male_slip", "male": "self_female_slip"}
_SENTENCE = re.compile(r"(?<=[.!?])\s+|\n+")


def _fold(text: str) -> str:
    return " ".join(str(text or "").casefold().replace("’", "'").replace("ʼ", "'").split())


def slips(text: str, gender: str) -> bool:
    """True when `text`, said by a member of player gender `gender`, speaks with the other
    sex's body or role in the first person."""
    rx = LISTS.get(gender)
    return rx is not None and rx.search(_fold(text)) is not None


def code_for(gender: str) -> str | None:
    return CODES.get(gender)


def without_slips(text: str, gender: str) -> str:
    """`text` with each sentence that slips left out, the rest joined by a space; "" when every
    sentence slips."""
    kept = [s.strip() for s in _SENTENCE.split(text or "") if s.strip() and not slips(s, gender)]
    return " ".join(kept)


__all__ = ["CODES", "FEMALE_SELF", "LISTS", "MALE_SELF", "code_for", "slips", "without_slips"]
