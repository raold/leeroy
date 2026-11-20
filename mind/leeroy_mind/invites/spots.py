"""The meeting spots his invitations name, mirrored from mod-leeroy's compiled table
(modules/mod-leeroy/src/Lib/LeeroyInviteSpots.h; tests/invites/test_spots.py reads that file and
holds the two equal), and how his words name them. Pure: nothing here talks to the game.

Each spot is where a world NPC of that place stands (world DB creature spawns, checked
2026-11-20); a spot whose name the Realm's places whitelist also has carries exactly its
coordinates (dal_fountain, dal_inn, dal_bank, org_inn, org_bank, uc_bank, tb_bank,
booty_bay_docks). Only places with known coordinates: a place he names that is none of these is
`UNKNOWN`, and she asks where; nothing moves."""
from dataclasses import dataclass
import re


@dataclass(frozen=True, slots=True)
class Spot:
    name: str
    map: int
    zone: int
    x: float
    y: float
    z: float
    o: float
    fish: bool
    kind: str   # fountain, landing, inn, bank, docks
    label: str  # how she names it: "the Eventide fountain", "Krasus' Landing", ...


SPOTS: tuple[Spot, ...] = (
    Spot("dal_fountain", 571, 4395, 5707.31, 612.214, 646.791, 1.69297, True, "fountain",
         "the Eventide fountain in Dalaran"),
    Spot("dal_krasus", 571, 4395, 5813.37, 453.403, 658.834, 4.45059, False, "landing",
         "Krasus' Landing in Dalaran"),
    Spot("dal_inn", 571, 4395, 5847.97, 635.435, 647.574, 1.0472, False, "inn",
         "the Legerdemain Lounge in Dalaran"),
    Spot("dal_horde_inn", 571, 4395, 5888.69, 507.898, 641.653, 1.8675, False, "inn",
         "the Filthy Animal in Dalaran"),
    Spot("dal_bank", 571, 4395, 5984.58, 607.113, 651.264, 2.79253, False, "bank",
         "the bank in Dalaran"),
    Spot("org_inn", 1, 1637, 1633.99, -4439.37, 15.7597, 2.75762, False, "inn",
         "the inn in Orgrimmar"),
    Spot("org_bank", 1, 1637, 1627.32, -4376.07, 12.0576, 3.4383, False, "bank",
         "the bank in Orgrimmar"),
    Spot("uc_inn", 0, 1497, 1635.4, 223.331, -43.0198, 2.84489, False, "inn",
         "the inn in the Undercity"),
    Spot("uc_bank", 0, 1497, 1599.96, 240.65, -52.0596, 0.087266, False, "bank",
         "the bank in the Undercity"),
    Spot("tb_inn", 1, 1638, -1300.28, 38.4858, 129.292, 0.558505, False, "inn",
         "the inn in Thunder Bluff"),
    Spot("tb_bank", 1, 1638, -1257.63, 19.5577, 128.27, 1.72788, False, "bank",
         "the bank in Thunder Bluff"),
    Spot("smc_inn", 530, 3487, 9684.1, -7363.6, 12.0145, 4.4855, False, "inn",
         "the inn in Silvermoon"),
    Spot("smc_bank", 530, 3487, 9808.54, -7488.17, 13.6369, 3.15905, False, "bank",
         "the bank in Silvermoon"),
    Spot("booty_bay_docks", 0, 33, -14297.2, 530.993, 8.77916, 3.98863, True, "docks",
         "the docks in Booty Bay"),
)
BY_NAME = {s.name: s for s in SPOTS}
SLOTS = 2  # two members may meet him at one spot (native kInviteSpotSlots)


class _Unknown:
    """A place he named that no spot is: she asks where."""

    def __repr__(self):
        return "UNKNOWN"


UNKNOWN = _Unknown()

# zone -> (the words that name the city, its default spot when he names only the city)
CITIES: dict[int, tuple[tuple[str, ...], str]] = {
    4395: (("dalaran", "dala", "dal"), "dal_krasus"),
    1637: (("orgrimmar", "orgrimar", "orgri", "ogri", "org"), "org_inn"),
    1497: (("undercity", "uc"), "uc_inn"),
    1638: (("thunder bluff", "thunderbluff", "tb"), "tb_inn"),
    3487: (("silvermoon city", "silvermoon", "smc"), "smc_inn"),
    33: (("booty bay", "bootybay", "bb"), "booty_bay_docks"),
}

# words for a kind of place (in his zone, or the city he names)
_KINDS: tuple[tuple[str, str], ...] = (
    ("fountain", r"fountain|eventide"),
    ("landing", r"krasus(?:'?s?)?(?:'? landing)?|the landing|flight ?master|flight ?path|\bfp\b"),
    ("inn", r"inn|tavern|pub|bar|lounge|legerdemain|filthy animal"),
    ("bank", r"bank|banker"),
    ("docks", r"docks?|pier|harbou?r"),
)
_KIND_RE = [(k, re.compile(rf"\b(?:{pat})\b")) for k, pat in _KINDS]
# the spot itself, by its own name, wherever he is
_NAMED = ((re.compile(r"\blegerdemain\b"), "dal_inn"),
          (re.compile(r"\bfilthy animal\b"), "dal_horde_inn"),
          (re.compile(r"\bkrasus"), "dal_krasus"),
          (re.compile(r"\beventide\b"), "dal_fountain"))
# a place he names that is none of ours: "meet me at the docks" in Orgrimmar, "at the AH"
PLACE_WORDS = re.compile(
    r"\b(?:fountain|eventide|krasus|landing|flight ?master|flight ?path|inn|tavern|pub|bar|lounge|"
    r"legerdemain|filthy animal|bank|banker|docks?|pier|harbou?r|auction house|ah|mailbox|"
    r"barber|tower|square|gate|gates|zeppelin|zep|portal|arena|trainer|forge|throne|bridge|"
    r"lake|river|beach|shore|pond|square|statue|plaza|market)\b")
_CITY_RE = [(zone, re.compile(r"\b(?:" + "|".join(re.escape(w) for w in words) + r")\b"))
            for zone, (words, _) in CITIES.items()]
# Cities and towns he may name that have no spot of ours: a place there is UNKNOWN (she asks),
# never the same kind of place in the city he stands in.
OTHER_CITIES = re.compile(
    r"\b(?:shattrath|shatt|stormwind|ironforge|darnassus|darn|exodar|gadgetzan|ratchet|everlook|"
    r"theramore|crossroads|xroads|splintertree|tarren mill|brill|warsong hold|vengeance landing|"
    r"valiance keep|wyrmrest|moa'?ki|agmar'?s hammer|camp winterhoof|light'?s hope|area 52|"
    r"stonard|kargath|grom'?gol|revantusk|silverpine|sepulcher|garadar|thrallmar|zabra'?jin|"
    r"aldor rise|scryer'?s tier|ogri'?la|sporeggar|the argent tournament|argent tournament|"
    r"wintergrasp|ebon hold|dun morogh|goldshire)\b")


# After "in" or "at", words that name no other place: a time ("in a bit", "in 5", "in the
# morning", "at noon"), where they already are ("in here", "in town", "in the same place"), a
# group or a person ("in my group", "at you"), a spot inside the place ("at the back").
_NOT_A_PLACE = frozenset((
    "a", "an", "about", "around", "like", "maybe", "just", "prob", "probably", "some", "half",
    "few", "bit", "sec", "secs", "second", "seconds", "min", "mins", "minute", "minutes",
    "moment", "while", "hour", "hours", "morning", "mornin", "evening", "afternoon", "night",
    "noon", "midnight", "once", "least", "last", "all", "time", "case", "fact", "general",
    "tonight", "tomorrow", "today", "now", "here", "there", "town", "city", "same", "game",
    "wow", "azeroth", "front", "back", "side", "middle", "corner", "inside", "outside", "top",
    "bottom", "my", "your", "our", "his", "her", "their", "this", "that", "it", "me", "you",
    "him", "us", "them", "group", "party", "raid", "guild", "chat", "discord", "voice", "vc"))
# Where a place phrase after "in" or "at" ends: the next of these, or the end of its clause.
_PHRASE_STOP = frozenset(("in", "at", "with", "and", "or", "but", "for", "to", "so", "if", "when",
                          "then", "near", "by", "on", "of", "from", "after", "before"))
_FISHING = re.compile(r"\bfish(?:ing)?\b")


def unplaced(text: str, start: int) -> bool:
    """He names a destination after `start` that is none of ours: "the inn in thousand needles",
    "fishing in nagrand". Only "in" and "at" phrases of the clause that begins at `start` count
    (up to , . ; ! ? or a dash), so "come to the inn, i'm in trouble" names none. A time, where
    they already are, a group or a person, a spot inside the place, a kind of place, a known
    landmark word and a city of ours are no other destination."""
    clause = re.split(r"[,.;!?]| - ", text[start:], maxsplit=1)[0]
    words = re.findall(r"[a-z0-9']+", clause)
    for i, word in enumerate(words):
        if word not in ("in", "at"):
            continue
        phrase = []
        for nxt in words[i + 1:i + 4]:
            if nxt in _PHRASE_STOP:
                break
            phrase.append(nxt)
        if phrase and phrase[0] == "the":
            phrase = phrase[1:]
        if not phrase:
            continue
        first, said = phrase[0], " ".join(phrase)
        if (first[0].isdigit() or first in _NOT_A_PLACE or kind_of(said) is not None
                or PLACE_WORDS.match(said) or city_of(said) is not None
                or any(rx.match(said) for rx, _ in _NAMED)):
            continue
        return True
    return False


def city_of(text: str) -> int | None:
    """The zone of a city his words name ("in dalaran", "org"), None for none."""
    for zone, rx in _CITY_RE:
        if rx.search(text):
            return zone
    return None


def kind_of(text: str) -> str | None:
    """The kind of place his words name ("the inn", "the bank"), None for none."""
    return _kind(text)[0]


def _kind(text: str) -> tuple[str | None, int]:
    """The kind of place his words name and where its words end in `text` (0 for none)."""
    for kind, rx in _KIND_RE:
        found = rx.search(text)
        if found is not None:
            return kind, found.end()
    return None, 0


def in_zone(zone: int, kind: str) -> Spot | None:
    """The first spot of `kind` in `zone` (the file's order puts the inn he means first)."""
    return next((s for s in SPOTS if s.zone == zone and s.kind == kind), None)


def fishing_spot(zone: int) -> Spot | None:
    return next((s for s in SPOTS if s.zone == zone and s.fish), None)


def resolve(text: str, *, his_zone: int, fishing: bool = False):
    """The spot his words name, None when they name no place, UNKNOWN when they name one that
    is none of ours (she asks where). `text` is lowercased. A spot named by its own name wins;
    then a kind of place in the city he names, else in his zone; a city alone is its default
    spot (its fishing spot when they are going fishing)."""
    for rx, name in _NAMED:
        if rx.search(text):
            return BY_NAME[name]
    city = city_of(text)
    if city is None and OTHER_CITIES.search(text):
        return UNKNOWN  # a city with no spot of ours: never his own city's place instead
    kind, after = _kind(text)
    if city is None:
        # a destination he names after the place or the fishing that is none of ours ("the inn
        # in thousand needles", "fishing in nagrand"): she asks, never his own city's place
        fished = _FISHING.search(text) if fishing and kind is None else None
        start = after if kind is not None else fished.end() if fished is not None else None
        if start is not None and unplaced(text, start):
            return UNKNOWN
    zone = city if city is not None else his_zone
    if kind is not None:
        spot = in_zone(zone, kind) if zone else None
        return spot if spot is not None else UNKNOWN
    if city is not None:
        if fishing:
            spot = fishing_spot(city)
            # fishing in a city without a known fishing spot: beside him if he is there
            return spot if spot is not None else (None if city == his_zone else UNKNOWN)
        return BY_NAME[CITIES[city][1]]
    if PLACE_WORDS.search(text):
        return UNKNOWN
    return None
