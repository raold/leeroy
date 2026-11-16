"""Bosses and battlegrounds a member names must exist (2026-11-15: members put battlegrounds in
zones that have none, "a battleground in Tanaris", and an invented boss name was picked
up from officer chat by two more officers).

Two deterministic checks, both failing open (no library, no flag):
- misplaced_battleground: a battleground said to be in, or named after, a zone of the library's
  places (ItemLinker.places) that is no battleground of its own (wow.BATTLEGROUNDS, Wintergrasp,
  the arenas);
- unknown_bosses: a word in a boss context ("parse X", "wipe on X", "kill X", "get X down",
  "X is basically free", "X heroic") that is no word the library knows (Library.known_words:
  every term of its text, or any word of a known boss or place with its punctuation taken
  out, as players type "lanathel"), no everyday or chat word, no common boss nickname
  (SHORTHAND), nothing with an everyday ending ("homework") and no member's name. A word another
  member already used in chat is no excuse: that is how one invention spread.
A battleground said to be in a capital city or in a zone with a battleground's entrance
(QUEUE_PLACES: people queue there) or "near" somewhere is fine.
The pipeline makes either a "game name" tell (Pipeline._generate)."""
import re
from collections.abc import Callable, Iterable

from leeroy_mind.talk.items import CHAT_WORDS
from leeroy_mind.talk.words import COMMON_WORDS
from leeroy_mind.wow import (BATTLEGROUNDS, BOSS_NAMES, DUNGEON_BOSS_NAMES, DUNGEONS,
                             ENCOUNTER_MEMBERS, RAID_INSTANCES)

_BG = r"(?:battlegrounds?|bgs?)"
_BG_IN = re.compile(rf"\b{_BG}\s+(?:in|at|out in|over in)\s+(?:the\s+)?([a-z][a-z' ]+)", re.I)
_BG_AFTER = re.compile(rf"\b([a-z][a-z']+(?:\s+[a-z][a-z']+){{0,2}})\s+{_BG}\b", re.I)
# the battlegrounds and arenas by name, and the zones a battle of their own is fought in
BG_PLACES = frozenset({*(n.casefold() for n in BATTLEGROUNDS.values()), "wintergrasp",
                       "lake wintergrasp", "nagrand arena", "blade's edge arena", "dalaran arena",
                       "dalaran sewers", "ruins of lordaeron", "ring of valor", "the ring of valor"})
# where people queue for one: the capital cities and the zones with a battleground's entrance
QUEUE_PLACES = frozenset({"orgrimmar", "stormwind", "stormwind city", "ironforge", "undercity",
                          "thunder bluff", "darnassus", "silvermoon", "silvermoon city", "exodar",
                          "the exodar", "shattrath", "shattrath city", "dalaran", "barrens",
                          "the barrens", "northern barrens", "ashenvale", "arathi highlands",
                          "alterac mountains", "hillsbrad foothills", "netherstorm"})
_VERB = (r"parse[sd]?|parsing|kill(?:ed|ing|s)?|down(?:ed|ing)?|pull(?:ed|ing|s)?|"
         r"farm(?:ed|ing)?|wipe[sd]? on|wiping on|progress(?:ion)? on|"
         r"prog(?:ging)? on|attempts? on|tries on")
_BOSS_CTX = re.compile(rf"\b(?:{_VERB})\s+(?:on\s+)?(?:the\s+)?([a-z][a-z'-]{{3,}})\b", re.I)
_BOSS_AFTER = re.compile(r"\b(?:get|got|getting)\s+([a-z][a-z'-]{3,})\s+down\b|"
                         r"\b([a-z][a-z'-]{3,})\s+(?:is (?:basically |practically )?free|"
                         r"hard ?mode|heroic|encounter|mechanics)\b", re.I)
# boss shorthand and raid words players use that the library's text does not spell
SHORTHAND = frozenset("""
sindy sindra putri prof valk valks valy vali thadd grobb razu jarax jaraxx mimi kolo sarth maly
yogg anub festy fester festers rotty roti maexx levi lana lanathel bql ldw sauf marrow gormok
icehowl sapph faerlina heigan noth gothik loatheb patch gluth ignis auri hodir freya thorim
vezax algalon koralon toravon emalon archavon halion ony champs twins beasts gunship trash adds
boss bosses hardmode twink twinks twinking everything anything something nothing them him her
it's that's this that those these whatever everyone someone
""".split())
# endings of everyday words ("homework", "boredom"); no boss or place name ends this way
_EVERYDAY_END = ("ness", "ment", "tion", "sion", "ity", "ful", "less", "ous", "ive", "able",
                 "ible", "work", "time", "ship", "hood", "dom", "ward", "wise", "ism", "ology")


def squashed(names: Iterable[str]) -> frozenset[str]:
    """Every word of the names, lower-case with its punctuation taken out ("Lana'thel":
    lanathel, "Blood-Queen": bloodqueen), as players type them."""
    out = set()
    for name in names:
        for word in (name or "").casefold().split():
            if w := re.sub(r"[^a-z]", "", word):
                out.add(w)
    return frozenset(out)


# the bosses, raids and dungeons this code knows by name
BOSS_WORDS = squashed([*BOSS_NAMES.values(), *DUNGEON_BOSS_NAMES.values(), *RAID_INSTANCES.values(),
                       *DUNGEONS.values(), *ENCOUNTER_MEMBERS,
                       *(n for names in ENCOUNTER_MEMBERS.values() for n in names)])
_PART = re.compile(r"[a-z]+")
_CONTRACTION = re.compile(r"'(?:ll|re|ve|d|t|m)$")


def misplaced_battleground(text: str, places: Iterable[str]) -> bool:
    """The text puts a battleground in, or names one after, a library place that is none."""
    fine = BG_PLACES | QUEUE_PLACES
    zones = {p for p in places if p not in fine}
    if not zones:
        return False
    low = " ".join((text or "").casefold().replace("’", "'").split())

    def placed(phrases: list[str]) -> bool:  # the longest phrase that is a place decides
        for phrase in phrases:
            if phrase in fine:
                return False
            if phrase in zones:
                return True
        return False
    for m in _BG_IN.finditer(low):
        words = m.group(1).split()
        if placed([" ".join(words[:n]) for n in (3, 2, 1) if n <= len(words)]):
            return True
    for m in _BG_AFTER.finditer(low):
        words = m.group(1).split()
        if placed([" ".join(words[-n:]) for n in (3, 2, 1) if n <= len(words)]):
            return True
    return False


def boss_words(text: str) -> list[str]:
    """The words in a boss context, lower-case, once each."""
    low = (text or "").casefold().replace("’", "'")
    out = []
    for m in (*_BOSS_CTX.finditer(low), *_BOSS_AFTER.finditer(low)):
        w = next(g for g in m.groups() if g).strip("'-")
        w = w[:-2] if w.endswith("'s") else w
        if _CONTRACTION.search(w):  # "down i'll", "get it'd": no name
            continue
        if w and w not in out:
            out.append(w)
    return out


def unknown_bosses(text: str, known: Callable[[Iterable[str]], set[str]],
                   people: Iterable[str] = (), names: Iterable[str] = ()) -> list[str]:
    """The boss-context words nobody could know: not everyday, chat, shorthand, a known boss or
    place (BOSS_WORDS, `names`: squashed words) or a person's name, and not a term of the
    library (`known`: of the words given, those it has)."""
    people = {p.casefold() for p in people if p}
    names = BOSS_WORDS | set(names)
    asked: dict[str, list[str]] = {}
    for w in boss_words(text):
        if (w in COMMON_WORDS or w in CHAT_WORDS or w in SHORTHAND or w in people
                or re.sub(r"[^a-z]", "", w) in names or w.endswith(_EVERYDAY_END)):
            continue
        parts = _PART.findall(w)
        if parts:
            asked[w] = parts
    if not asked:
        return []
    have = known({v for parts in asked.values() for p in parts for v in _forms(p)})
    return [w for w, parts in asked.items()
            if not all(any(v in have for v in _forms(p)) for p in parts)]


def _forms(word: str) -> set[str]:
    """The word and its plain stems ("lapsed": lapse, "twinking": twink), so an inflected
    everyday word the library spells only one way still counts as known."""
    out = {word}
    for end in ("s", "es", "ed", "d", "ing", "ly"):
        if word.endswith(end) and len(word) - len(end) >= 3:
            stem = word[:-len(end)]
            out |= {stem, stem + "e"}
    return out
