"""Each US member's home state (owner, 2026-11-15: "location (state if in US) UK if in UK"), for
the console's member card and Personas line. A UK member has none and reads "UK".

- choose: where her own words put her: the first US place her real_life texts name (household
  first, then job, rhythm, life thread, money, sleep), then her mock interview. A place named as
  where a relative is ("son is 23 and in Columbus", "kids in Ohio and Texas") counts only when
  she names no place of her own. A place is a state's full name, "City, ST" or a city of CITIES
  after "in", "outside", "near", "from", "at", "of" or a comma (never "off Peoria": a road).
  Otherwise a draw seeded by her guid and weighted by the states' 2020 populations, so the same
  member always gets the same state.
- set_home: one transaction. Her stored persona read again, saved with the home as a new persona
  version, source "owner", with a persona ledger row of kind "home" and a private receipt (meta
  RECEIPT: the home before and after, and the source before), as personas.looks_edit does.
- restore: nightly.growth.undo's "home" branch (`personas undo <id>`): the home as it was, with
  the source before, while it is still what the edit left.

`personas homes` plans, and with --go saves, only US members of the cast without a home; nothing
in talk reads it, and presence keeps US Eastern time for every US member."""
import json
import random
import re
from dataclasses import dataclass, replace

from .io import load_persona, save_persona
from .states import ABBREVIATIONS, US_STATES

KIND = "home"
RECEIPT = "personas:home-undo:{ledger}"
SOURCES = ("seed", "generated", "owner", "variety")  # persona_core's sources
# real_life's free text in the order a home is looked for, then the mock interview
FIELDS = ("household", "job", "rhythm", "life_thread", "money", "sleep")
# Cities the stored cast actually names (2026-11-15), by the state each is in.
CITIES = {
    "Akron": "Ohio", "Alexandria": "Virginia", "Anchorage": "Alaska", "Ashtabula": "Ohio",
    "Atlanta": "Georgia", "Austin": "Texas", "Berry Hill": "Tennessee", "Boise": "Idaho",
    "Boulder": "Colorado", "Chandler": "Arizona", "Cleveland": "Ohio", "Columbus": "Ohio",
    "Dayton": "Ohio", "Eugene": "Oregon", "Everett": "Washington", "Flagstaff": "Arizona",
    "Hutchinson": "Kansas", "Irving": "Texas", "Juneau": "Alaska", "Kansas City": "Missouri",
    "Lake Forest Park": "Washington", "Midland": "Texas", "Nashville": "Tennessee",
    "NC State": "North Carolina", "Omaha": "Nebraska", "Phoenix": "Arizona",
    "Roanoke": "Virginia", "Short North": "Ohio", "St. Paul": "Minnesota", "Taos": "New Mexico",
    "Tucson": "Arizona", "Tulsa": "Oklahoma", "Waco": "Texas", "Yakima": "Washington",
}
_COMMON = frozenset({"Boulder", "Chandler", "Phoenix"})  # a common noun unless capitalised
_CITY_NAMES = {c.casefold(): c for c in CITIES}
_STATE_NAMES = {s.casefold(): s for s in US_STATES}


def _alt(names) -> str:
    return "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True))


# words that are no place: a cat breed, the capital district
_NOT_PLACE = re.compile(r"\bMaine\s+Coons?\b|\bWashington,?\s+D\.?\s?C\b\.?", re.IGNORECASE)
# "Flagstaff, AZ", "Cleveland TN": capitalised words and a postal code. A code that is also an
# English word (OK, OR, IN...) needs the comma; "7-10pm CT" is a time zone, not Connecticut.
_WORDY = frozenset({"AL", "CO", "DE", "HI", "ID", "IN", "LA", "MA", "ME", "OH", "OK", "OR", "PA"})
_CODED = re.compile(r"\b((?:[A-Z][\w.'-]*\s+){0,2}[A-Z][\w.'-]*)(,\s*|\s+)("
                    + _alt(ABBREVIATIONS) + r")\b(?![\w'-])")
_CITY_CODED = re.compile(r"\b(" + _alt(CITIES) + r"),\s*(" + _alt(ABBREVIATIONS) + r")\b",
                         re.IGNORECASE)  # "tucson, az" as she typed it
_CITY = re.compile(r"(?:\b(?:in|outside|near|from|at|of)\s+(?:(?:a|an|the)\s+)?|,\s*|^)("
                   + _alt(CITIES) + r")\b", re.IGNORECASE)
_STATE = re.compile(r"\b(" + _alt(US_STATES) + r")\b", re.IGNORECASE)
# someone else's place: a relative named earlier in the same clause
_RELATIVE = re.compile(r"\b(?:kids?|sons?|daughters?|child(?:ren)?|grandkids?|mom|mum|mother|"
                       r"dad|father|parents?|sisters?|brothers?)\b", re.IGNORECASE)


@dataclass(frozen=True)
class Mention:
    state: str
    phrase: str  # the words that name it
    own: bool    # where she is, not where a relative of hers is


@dataclass(frozen=True)
class HomePlan:
    guid: int
    name: str
    before: str | None
    home: str | None
    how: str                  # "text", "draw" or "clear"
    field: str | None = None  # where her words named it
    phrase: str | None = None


def mentions(text: str) -> list[Mention]:
    """The US places a text names, left to right."""
    found: list[tuple[int, int, str | None, str]] = []  # start, end, state (None: no place)

    def free(a, b):
        return all(b <= s or a >= e for s, e, _, _ in found)

    for m in _NOT_PLACE.finditer(text):
        found.append((m.start(), m.end(), None, m.group(0)))
    for m in _CODED.finditer(text):
        if (m.group(3) not in _WORDY or "," in m.group(2)) and free(m.start(), m.end()):
            found.append((m.start(), m.end(), ABBREVIATIONS[m.group(3)], m.group(0)))
    for m in _CITY_CODED.finditer(text):
        if free(m.start(), m.end()):
            found.append((m.start(), m.end(), ABBREVIATIONS[m.group(2).upper()], m.group(0)))
    for m in _CITY.finditer(text):
        city = _CITY_NAMES[m.group(1).casefold()]
        if free(m.start(1), m.end(1)) and (m.group(1)[0].isupper() or city not in _COMMON):
            found.append((m.start(1), m.end(1), CITIES[city], m.group(1)))
    for m in _STATE.finditer(text):
        if free(m.start(), m.end()):
            found.append((m.start(), m.end(), _STATE_NAMES[m.group(1).casefold()], m.group(1)))
    out = []
    for start, _, state, phrase in sorted(found):
        if state is not None:
            clause = re.split(r"[,;.]\s", text[:start])[-1]
            out.append(Mention(state, phrase, not _RELATIVE.search(clause)))
    return out


def from_texts(p) -> tuple[Mention, str] | None:
    """(the place, the field that named it) where her words put her, or None."""
    texts = [(f, getattr(p.real_life, f)) for f in FIELDS] + [("interview", p.mock_interview)]
    named = [(m, f) for f, t in texts for m in mentions(t or "")]
    return next(((m, f) for m, f in named if m.own), named[0] if named else None)


def draw(guid: int) -> str:
    """A state for `guid`, the same each time, weighted by population."""
    names = sorted(US_STATES)
    return random.Random(f"home:{guid}").choices(names, weights=[US_STATES[n] for n in names])[0]


def choose(p) -> HomePlan:
    found = from_texts(p)
    if found is None:
        return HomePlan(p.guid, p.character_name, p.identity.home, draw(p.guid), "draw")
    m, field = found
    return HomePlan(p.guid, p.character_name, p.identity.home, m.state, "text", field, m.phrase)


def plan_homes(personas, *, clear: bool = False) -> list[HomePlan]:
    """A home for each US member without one; with `clear`, every home taken off again."""
    if clear:
        return [HomePlan(p.guid, p.character_name, p.identity.home, None, "clear")
                for p in personas if p.identity.home is not None]
    return [choose(p) for p in personas
            if p.identity.region == "US" and p.identity.home is None]


def describe(before: str | None, after: str | None) -> str:
    return f"home {before or 'not set'} -> {after or 'not set'}"


def _race_class(store, guid: int) -> tuple[str, str]:
    from leeroy_mind.nightly.enrich import _race_class as race_class
    return race_class(store, guid)


def set_home(store, guid: int, home: str | None, *, now: float) -> int | None:
    """Her home as given, saved as an owner persona version with its ledger row and receipt;
    the ledger row's id, or None when it already is (nothing written). The schema refuses a
    home that is not a US state, or one for a UK member (PersonaError)."""
    from leeroy_mind.nightly.store import ledger_add
    with store.transaction():
        stored = load_persona(store, guid)
        if stored is None:
            raise LookupError(f"guid {guid} has no stored persona")
        before = stored.identity.home
        if before == home:
            return None
        source = store.conn.execute("SELECT source FROM persona_core WHERE guid=?",
                                    (guid,)).fetchone()[0]
        race, class_name = _race_class(store, guid)
        version = save_persona(store, replace(stored, identity=replace(stored.identity, home=home)),
                               source="owner", ts=now, race=race, class_name=class_name)
        ledger = ledger_add(store, guid=guid, kind=KIND, text=describe(before, home), evidence=[],
                            now=now)
        store.set_meta(RECEIPT.format(ledger=ledger), json.dumps(
            {"before": before, "after": home, "source": source, "version": version},
            sort_keys=True))
    return ledger


def restore(store, row, *, now: float) -> bool:
    """nightly.growth.undo of a "home" ledger row, inside its transaction: the home as it was
    before, with the persona's source before, while it is still what the edit left. False
    (nothing written) when the receipt is gone or a later change moved it."""
    raw = store.get_meta(RECEIPT.format(ledger=row["id"]))
    if raw is None:
        return False
    receipt = json.loads(raw)
    p = load_persona(store, row["guid"])
    if p is None or p.identity.home != receipt["after"]:
        return False  # a later change wins
    restored = replace(p, identity=replace(p.identity, home=receipt["before"]))
    source = receipt.get("source") if receipt.get("source") in SOURCES else "owner"
    race, class_name = _race_class(store, p.guid)
    save_persona(store, restored, source=source, ts=now, race=race, class_name=class_name)
    return True
