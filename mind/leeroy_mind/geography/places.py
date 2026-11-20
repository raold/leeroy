"""The place sheet (places.toml) and where each member lives on it.

- A US member lives in her home state (Identity.home, given by `personas homes`); one without a
  home has no place, and everything here leaves her as she was (the owner's clock, no line).
- A UK member lives in the UK region her own words name (uk_region: the first place her
  real_life texts name, household first, then job, rhythm, life thread, money, sleep, then her
  mock interview; a place named as where a relative is counts only when she names none of her
  own, as personas.homes reads a US home), else in the UK as a whole.

Nothing here reads a switch: geography.config says whether any of it is in force."""
import math
import re
import tomllib
from dataclasses import dataclass
from functools import lru_cache
from importlib.resources import files
from zoneinfo import ZoneInfo

from leeroy_mind.personas.states import ABBREVIATIONS

SEASONS = {12: "winter", 1: "winter", 2: "winter", 3: "spring", 4: "spring", 5: "spring",
           6: "summer", 7: "summer", 8: "summer", 9: "autumn", 10: "autumn", 11: "autumn"}
UK_WHOLE = "uk"  # the UK entry for a member whose words name no part of it
# Fields her home is looked for in, in order (personas.homes.FIELDS), then the mock interview
FIELDS = ("household", "job", "rhythm", "life_thread", "money", "sleep")
# UK words that are also ordinary words or other places: only after "in", "from", "near"...
_CONTEXT_ONLY = frozenset({"Reading", "Stoke", "Perth", "Newport", "Hull", "Derby", "Lincoln",
                           "Durham", "Chester", "Bangor", "Lancaster", "Worcester", "Halifax",
                           "Gloucester", "Midlands", "Highlands"})
# Words for where she is from rather than where she lives: used only when no place is named
_ADJECTIVES = frozenset({"Welsh", "Scottish", "Geordie", "Scouse", "Mancunian", "Glaswegian",
                         "Brummie", "Cornish"})
_RELATIVE = re.compile(r"\b(?:kids?|sons?|daughters?|child(?:ren)?|grandkids?|mom|mum|mother|"
                       r"dad|father|parents?|sisters?|brothers?|nan|gran|grandma|grandad|"
                       r"cousins?|aunt|uncle|in-laws)\b", re.IGNORECASE)
_PREPOSITION = re.compile(r"(?:\b(?:in|outside|near|from|at|of|round|around)\s+(?:the\s+)?|,\s*)$",
                          re.IGNORECASE)


@dataclass(frozen=True)
class Event:
    what: str
    months: frozenset[int]


@dataclass(frozen=True)
class Place:
    """One place of the sheet. `highs`/`lows`: typical daily highs and lows by month, January
    first, in Fahrenheit (a UK place's Celsius rows converted). `key`: the state's name, or
    "uk.<region>"."""
    key: str
    country: str  # "US" or "UK"
    label: str
    zone: str
    point: tuple[float, float]
    coast: str  # "ocean", "lakes" or "none"
    mountains: bool
    humid: bool
    terrain: str
    geography: str
    cities: tuple[str, ...]
    words: tuple[str, ...]
    touchstones: tuple[str, ...]
    snow: frozenset[int]
    highs: tuple[float, ...]
    lows: tuple[float, ...]
    events: tuple[Event, ...]
    notes: tuple[tuple[str, str], ...]

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.zone)

    @property
    def celsius(self) -> bool:
        return self.country == "UK"

    def note(self, month: int) -> str:
        return dict(self.notes).get(SEASONS[month], "")

    def names(self) -> tuple[str, ...]:
        """Every name that points at this place: its label, cities and words."""
        return tuple(dict.fromkeys(
            (self.label.removeprefix("the "), *self.cities, *self.words)))


def _c_to_f(c: float) -> float:
    return c * 9 / 5 + 32


def _place(key: str, country: str, d: dict) -> Place:
    if country == "UK":
        highs = tuple(_c_to_f(x) for x in d["highs_c"])
        lows = tuple(_c_to_f(x) for x in d["lows_c"])
    else:
        highs, lows = tuple(float(x) for x in d["highs"]), tuple(float(x) for x in d["lows"])
    if len(highs) != 12 or len(lows) != 12 or any(lo > hi for lo, hi in zip(lows, highs)):
        raise ValueError(f"places.toml: {key}: twelve highs and lows, each low under its high")
    if d["coast"] not in ("ocean", "lakes", "none"):
        raise ValueError(f"places.toml: {key}: coast")
    ZoneInfo(d["zone"])  # a zone the system knows, or the sheet does not load
    return Place(
        key=key, country=country, label=d.get("label", key), zone=d["zone"],
        point=(float(d["point"][0]), float(d["point"][1])), coast=d["coast"],
        mountains=bool(d["mountains"]), humid=bool(d["humid"]), terrain=d["terrain"],
        geography=d.get("geography", ""), cities=tuple(d.get("cities", ())),
        words=tuple(d.get("words", ())), touchstones=tuple(d.get("touchstones", ())),
        snow=frozenset(d.get("snow", ())), highs=highs, lows=lows,
        events=tuple(Event(e["what"], frozenset(e["months"])) for e in d.get("events", ())),
        notes=tuple(sorted(d.get("notes", {}).items())))


@lru_cache(maxsize=1)
def sheet() -> dict[str, Place]:
    """Every place by key: the states (and the District of Columbia) by name, the UK's by
    "uk.<region>"."""
    raw = tomllib.loads(files("leeroy_mind.geography").joinpath("places.toml")
                        .read_text(encoding="utf-8"))
    out = {name: _place(name, "US", d) for name, d in raw["us"].items()}
    out.update({f"uk.{key}": _place(f"uk.{key}", "UK", d) for key, d in raw["uk"].items()})
    return out


def us_state(name: str | None) -> Place | None:
    return sheet().get(name) if name else None


def uk_place(key: str) -> Place | None:
    return sheet().get(f"uk.{key}")


@lru_cache(maxsize=1)
def _uk_words():
    """(word, region key, adjective) for every UK word, longest first, and their pattern."""
    found = []
    for place in sheet().values():
        if place.country == "UK" and place.key != f"uk.{UK_WHOLE}":
            for w in place.words:
                found.append((w, place.key, w in _ADJECTIVES))
    found.sort(key=lambda x: -len(x[0]))
    pattern = re.compile(r"\b(" + "|".join(re.escape(w) for w, _, _ in found) + r")\b")
    return {w: (k, adj) for w, k, adj in found}, pattern


def _mentions(text: str) -> list[tuple[str, bool, bool]]:
    """(region key, her own, adjective) for each UK place `text` names, left to right."""
    words, pattern = _uk_words()
    out = []
    for m in pattern.finditer(text or ""):
        word = m.group(1)
        if word in _CONTEXT_ONLY and not _PREPOSITION.search(text[:m.start()]):
            continue
        key, adjective = words[word]
        clause = re.split(r"[,;.]\s", text[:m.start()])[-1]
        out.append((key, not _RELATIVE.search(clause), adjective))
    return out


@lru_cache(maxsize=512)
def _uk_region(texts: tuple[str, ...]) -> str | None:
    named = [m for t in texts for m in _mentions(t)]
    for want in ((True, False), (False, False), (True, True), (False, True)):  # own, adjective
        hit = next((key for key, own, adj in named if (own, adj) == want), None)
        if hit is not None:
            return hit
    return None


def uk_region(p) -> str | None:
    """The key ("uk.<region>") of the UK region her own words name, or None."""
    texts = tuple(getattr(p.real_life, f) or "" for f in FIELDS) + (p.mock_interview or "",)
    return _uk_region(texts)


def place_of(p) -> Place | None:
    """Where she lives: her home state, her UK region (or the UK as a whole), None for a US
    member without a home (or a home the sheet does not hold)."""
    if p is None:
        return None
    region = getattr(p.identity, "region", "")
    if region == "US":
        return us_state(getattr(p.identity, "home", None))
    if region == "UK":
        return sheet().get(uk_region(p) or f"uk.{UK_WHOLE}")
    return None


_DC = frozenset({"dc", "d.c.", "d.c", "washington dc", "washington d.c.", "washington, dc",
                 "washington, d.c.", "district of columbia", "the district of columbia"})
_UK_WHOLE_NAMES = frozenset({"uk", "the uk", "united kingdom", "the united kingdom", "britain",
                             "great britain", "england", "gb"})


def lookup(name: str) -> Place | None:
    """The place a written name stands for (the owner's `[owner] home`): a state's full name,
    its postal code, the District of Columbia in its usual spellings, a UK region by its label
    (with or without "the"), its key or one of its words, or the UK as a whole. None for
    anything else. Case does not matter."""
    if not isinstance(name, str):
        return None
    folded = " ".join(name.split()).casefold()
    if not folded:
        return None
    if folded in _DC:
        return sheet()["District of Columbia"]
    if folded in _UK_WHOLE_NAMES:
        return sheet()[f"uk.{UK_WHOLE}"]
    for place in sheet().values():
        if place.country == "US" and place.key.casefold() == folded:
            return place
    code = folded.upper()
    if len(code) == 2 and code in ABBREVIATIONS:
        return sheet().get(ABBREVIATIONS[code])
    for place in sheet().values():
        if place.country != "UK":
            continue
        labels = {place.label.casefold(), place.label.casefold().removeprefix("the "),
                  place.key.removeprefix("uk.").replace("_", " "),
                  place.label.casefold().removeprefix("the ").removesuffix(" of england")
                  + " england"}
        if folded in labels or folded in {w.casefold() for w in place.words
                                          if w not in _ADJECTIVES}:
            return place
    return None


def km_between(a: Place, b: Place) -> float:
    """The great-circle distance between two places' points, in kilometres."""
    (la1, lo1), (la2, lo2) = a.point, b.point
    p1, p2 = math.radians(la1), math.radians(la2)
    dp, dl = p2 - p1, math.radians(lo2 - lo1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(min(1.0, h)))


def all_names() -> frozenset[str]:
    """Every place name on the sheet, folded: states, labels, cities and UK words."""
    out = set()
    for place in sheet().values():
        out.update(n.casefold() for n in place.names())
        if place.country == "US":
            out.add(place.key.casefold())
    out.discard("uk")
    return frozenset(out)
