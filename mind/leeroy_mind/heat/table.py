"""The heat table: what each value 0-10 stands for. Data only.

Heat 9 is the behaviour from before the dial existed: the explicit ceiling at the legacy romance
dial 9, every multiplier 1.0, no escalation rule of its own and the full photo lists (P4)."""
from dataclasses import dataclass

HEAT_LO, HEAT_HI = 0, 10
OFFSET_LO, OFFSET_HI = -5, 5
TODAY = 9  # the value that reads exactly as the code did without heat
CLEAN_CAP = 2  # "keep it clean tonight": her heat is at most this for the session
OPEN_HEAT = 10  # "no limits tonight": her heat for the session

# The text tiers, lowest first. "none": no romance wording at all. "frank" sits between
# suggestive and explicit: direct about wanting, nothing graphic; its words are the owner's file
# (heat/frank.txt), and without one it reads the suggestive text.
TIERS = ("none", "warm", "flirty", "suggestive", "frank", "explicit")
PUBLIC_TIER = "flirty"  # with [heat] public_cap: the ceiling on every channel others read

# The photo tiers. Each is a subset of the next; P4 is the lists as they were (heat/photos.py).
P0, P1, P2, P3, P4 = range(5)
PHOTO_TIERS = ("conservative", "dressed", "going out", "gym", "swim")


@dataclass(frozen=True)
class Row:
    tier: str  # the text ceiling in private, to him
    dial: int  # the legacy romance dial (1-10) the older readers take in its place
    flirt: float  # on the weight of a flirt want and the chance a social turn becomes a flirt
    reassure: bool  # whether the reassurance want is offered
    lead: int  # his own heat lines a conversation needs before she reaches her ceiling
    starts: float  # on the romantic share of the whisper and opener clocks
    voice: str  # the highest tier a voice note may speak; above it the line is typed
    photo: int  # the photo tier
    offers: float  # on the chance a turn to him offers a `him` photo


ROWS: tuple[Row, ...] = (
    Row("none", 1, 0.0, False, 0, 0.0, "none", P0, 0.5),          # 0
    Row("warm", 1, 0.5, True, 2, 0.3, "warm", P0, 0.7),           # 1
    Row("warm", 2, 0.5, True, 2, 0.3, "warm", P0, 0.7),           # 2
    Row("warm", 3, 0.5, True, 2, 0.3, "warm", P0, 0.7),           # 3
    Row("flirty", 4, 1.0, True, 1, 0.6, "flirty", P1, 0.8),       # 4
    Row("flirty", 5, 1.0, True, 1, 0.6, "flirty", P1, 0.8),       # 5
    Row("suggestive", 7, 1.0, True, 1, 0.8, "suggestive", P2, 0.9),   # 6
    Row("suggestive", 8, 1.0, True, 1, 0.8, "suggestive", P2, 0.9),   # 7
    Row("frank", 8, 1.0, True, 1, 0.9, "suggestive", P3, 1.0),    # 8
    Row("explicit", 9, 1.0, True, 0, 1.0, "frank", P4, 1.0),      # 9: today
    Row("explicit", 10, 1.0, True, 0, 1.3, "explicit", P4, 1.2),  # 10
)
LATE_SELF = 8  # at this heat she may take one step herself late at night (no lead needed)
EVERY_TURN = 10  # from here every private turn of hers to him carries her romance

# The legacy romance dial (1-10) as a heat, for a world with no heat set and for a woman's own
# stored dial: the same band, and 9 and 10 are themselves.
DIAL_TO_HEAT = {1: 1, 2: 2, 3: 3, 4: 4, 5: 5, 6: 5, 7: 6, 8: 7, 9: 9, 10: 10}


def clamp(heat: int) -> int:
    return max(HEAT_LO, min(HEAT_HI, int(heat)))


def tier_index(tier: str) -> int:
    return TIERS.index(tier)


def lower(a: str, b: str) -> str:
    """The lower of two tiers."""
    return a if tier_index(a) <= tier_index(b) else b
