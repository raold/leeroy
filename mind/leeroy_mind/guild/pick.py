"""Bounded parsing of the owner's raid choices and short guild commands.

This recognizes requests, not arbitrary mentions of raids or officer names.
Unknown surrounding prose, contradictory raid choices and negated requests are
left for ordinary conversation instead of causing a guild action.
"""

from dataclasses import dataclass
import re

from .raids import CATALOG


@dataclass(frozen=True, slots=True)
class Lead:
    human: bool
    guid: int | None = None
    rotate: bool = False


@dataclass(frozen=True, slots=True)
class Pick:
    raid_key: str
    size: int | None
    heroic: bool
    lead: Lead | None


@dataclass(frozen=True, slots=True)
class Decline:
    pass


@dataclass(frozen=True, slots=True)
class Hangout:
    pass


@dataclass(frozen=True, slots=True)
class LeadOnly:
    lead: Lead


_ALIASES = {alias: raid.key for raid in CATALOG.values() for alias in raid.aliases}
_RAID = re.compile(r"\b(?:" + "|".join(re.escape(a) for a in sorted(_ALIASES, key=lambda a: (-len(a), a))) + r")\b")
_SIZE = re.compile(r"\b(10|25)(?:\s*(man|hc|m|n|h))?\b")
_HEROIC = re.compile(r"\b(?:heroic|hc)\b")
_INTENT = re.compile(r"\b(?:let'?s|wanna|want to|can we|could we|how about|raid|run|do|go|up for|feel like)\b")
_NEGATED = re.compile(r"\b(?:not|no|never|don'?t|doesn'?t|didn'?t|can'?t|won'?t|wouldn'?t|shouldn'?t|stop|avoid|skip|pass|nah|nope)\b")
_HUMAN = re.compile(r"\b(?:i'?ll lead|i lead|i'?m leading|my lead|i'?ll call it)\b")
_ROTATE = re.compile(r"\b(?:someone else (?:can )?leads?|pick (?:a|someone to) lead|you pick a lead|rotate(?: the)? lead|new lead tonight)\b")
_MEMBER = tuple(re.compile(pattern) for pattern in (
    r"\blet ([^\W\d_][\w'-]*) lead\b",
    r"\byou lead,? ([^\W\d_][\w'-]*)\b",
    r"\b([^\W\d_][\w'-]*) leads?\b",
))
_DECLINE = re.compile(r"\b(?:not tonight|no raid|skip(?: it| tonight)?|pass|nah|nope|not feeling it|maybe tomorrow|no thanks|not today)\b")
_HANGOUT = re.compile(r"\b(?:no raid,? (?:let'?s )?(?:just )?(?:hang(?: ?out)?|chill|fish)|hang ?out(?: night)?|chill night|off night|fishing night|just (?:hang(?: ?out)?|chill))\b")
_AFTER = re.compile(r"\b(?:then )?hang ?out after(?:wards)?\b")
_FILLER = re.compile(r"\b(?:i|we|you|should|please|pls|tonight|today|now|guys|all|maybe|again|instead|first|next|a|the|night)\b")
_CLAIM = re.compile(r"(?:mine|need|me pls|gimme|i'?ll take (?:it|that)|can i (?:have|get) (?:it|that))")
_END = re.compile(r"(?:call(?:ing)? it|that'?s it for tonight|done for tonight|i'?m out|gn(?: all)?)")


def _text(text: str) -> str:
    if not isinstance(text, str) or len(text) > 1024:
        return ""
    return " ".join(text.lower().replace("’", "'").replace("‘", "'").split())


def _empty(text: str) -> bool:
    return not _FILLER.sub(" ", text).strip(" \t\r\n,.!?;:")


def _resolve(name: str, officers: dict[str, int]) -> int | None:
    names = [(key.lower(), guid) for key, guid in officers.items()
             if isinstance(key, str) and type(guid) is int and guid > 0]
    exact = {guid for key, guid in names if key == name}
    if exact:
        return next(iter(exact)) if len(exact) == 1 else None
    if len(name) < 3:
        return None
    matches = {guid for key, guid in names if key.startswith(name)}
    return next(iter(matches)) if len(matches) == 1 else None


def _lead(text: str, officers: dict[str, int]) -> tuple[Lead | None, str]:
    rotate = bool(_ROTATE.search(text))
    human = bool(_HUMAN.search(text))
    remainder = _HUMAN.sub(" ", _ROTATE.sub(" ", text))
    guids = set()
    for pattern in _MEMBER:
        for match in pattern.finditer(remainder):
            guid = _resolve(match[1], officers)
            if guid is not None:
                guids.add(guid)
        remainder = pattern.sub(" ", remainder)
    if rotate:
        return Lead(False, None, True), remainder
    if human:
        return Lead(True), remainder
    return (Lead(False, next(iter(guids))) if len(guids) == 1 else None), remainder


def parse(text: str, officers: dict[str, int], *, require_intent: bool) -> Pick | Decline | Hangout | LeadOnly | None:
    text = _text(text)
    if not text:
        return None
    # Separate compact raid-size-mode forms without matching an alias inside a word.
    text = re.sub(r"(?<=[a-z])(?=\d)|(?<=\d)(?=[a-z])", " ", text)
    lead, remainder = _lead(text, officers)
    # An officer named Ruby can lead Icecrown without proposing two raids.
    aliases = tuple(_RAID.finditer(remainder))
    if not aliases:
        if _HANGOUT.search(text) and _empty(_INTENT.sub(" ", _DECLINE.sub(" ", _HANGOUT.sub(" ", text)))):
            return Hangout()
        if _DECLINE.search(text) and _empty(_DECLINE.sub(" ", text)):
            return Decline()
        if _NEGATED.search(text):
            return None
        return LeadOnly(lead) if lead is not None and _empty(remainder) else None

    keys = {_ALIASES[match[0]] for match in aliases}
    if len(keys) != 1 or _NEGATED.search(text):
        return None
    if require_intent and not _INTENT.search(text):
        return None
    sizes = tuple(_SIZE.finditer(remainder))
    choices = {int(match[1]) for match in sizes}
    if len(choices) > 1:
        return None
    heroic = any(match[0] == "togc" for match in aliases) or bool(_HEROIC.search(remainder)) or any(match[2] in ("h", "hc") for match in sizes)
    if heroic and any(match[2] == "n" for match in sizes):
        return None
    for pattern in (_RAID, _SIZE, _HEROIC, _INTENT, _AFTER):
        remainder = pattern.sub(" ", remainder)
    if not _empty(remainder):
        return None
    return Pick(next(iter(keys)), next(iter(choices)) if choices else None, heroic, lead)


def is_claim(text: str) -> bool:
    """Only an entire short claim counts, allowing conversational punctuation."""
    return _CLAIM.fullmatch(_text(text).strip(" .!?")) is not None


def is_end(text: str) -> bool:
    """A question or a quoted/narrated end phrase never closes a raid."""
    text = _text(text)
    return "?" not in text and _END.fullmatch(text.strip(" .!")) is not None
