"""The dials the owner can turn, and what each one stands for.

Two kinds of member dial:
- a score dial (argumentative, reliability) runs 1-10 and starts at the persona's own value;
- a level dial runs 0-10 with LEVEL_NEUTRAL (5) meaning "as the persona is written": its
  multiplier there is exactly 1.0 and it adds no wording. 0 is off or least, 10 the most (twice
  as often, for the dials that change how often).

World dials override mind.toml's own knobs, in mind.toml's own ranges, without editing it.

Dials with no behaviour behind them yet (crude, ego, patience, loot greed, raid comms: phase 3b
T12) are not here, so the endpoint refuses them and the page never shows them."""
from dataclasses import dataclass
from typing import Literal

LEVEL_NEUTRAL = 5
LEVEL_LO, LEVEL_HI = 0, 10
SCORE_LO, SCORE_HI = 1, 10
OFFSET_LO, OFFSET_HI = -10, 10
WORDING_STEP = 2  # a value this far from neutral (or from the persona's score) adds a tuning line

# The habits "party" means (owner, 2026-11-04: drinking and smoking nights), never coffee.
PARTY_SUBSTANCES = frozenset({"alcohol", "weed", "tobacco"})


@dataclass(frozen=True)
class Dial:
    name: str
    label: str
    kind: Literal["score", "level"]
    who: Literal["everyone", "women", "party"]
    when: str  # when a change is felt, in the page's words
    moves: str  # what it changes, in the page's words
    frequency: bool  # the preview shows numbers, not a sample line
    wording: bool = False  # far enough from neutral, it adds a tuning line to the prompt


MEMBER_DIALS: dict[str, Dial] = {d.name: d for d in (
    Dial("talkative", "talkative", "level", "everyone", "the next turn",
         "how often they are drawn to speak (the speaker draw), inside every volume brake",
         frequency=True),
    Dial("argumentative", "argumentative", "score", "everyone", "the next turn and the next night",
         "their agreeableness, the other way up: how they answer when you push back, slang "
         "uptake and the night's temper; far from their own it also changes their wording",
         frequency=False, wording=True),
    Dial("forward", "forward / flirty", "level", "women", "the next turn",
         "how often she flirts with you, carries her romance, whispers you and cheers your loot, "
         "and at the ends how bold her wording is; never her stance or attraction", frequency=True),
    Dial("humour", "humour (how much)", "level", "everyone", "the next turn",
         "how often their bits come out; far from 5 it also changes how much they joke. Their "
         "kind of humour stays as written", frequency=False, wording=True),
    Dial("theorycraft", "theorycrafter", "level", "everyone", "the next turn",
         "how much stats, gear and rotations pull them into talk and your game questions",
         frequency=False, wording=True),
    Dial("lore", "lore nerd", "level", "everyone", "the next turn",
         "how much the story of the world pulls them into talk and your lore questions",
         frequency=False, wording=True),
    Dial("drama", "drama", "level", "everyone", "the next night",
         "how readily they retell what passes the privacy gates, confront, needle and gossip "
         "at night; never what they may tell", frequency=True),
    Dial("jealousy", "jealousy", "level", "everyone", "the next turn and the next night",
         "how likely they are to be the one who reacts: a man's envy asides, a woman's rivalry, "
         "and how fast the night's jealousy grows", frequency=True),
    Dial("party", "party", "level", "party", "his next login",
         "how often their drinking and smoking habits are on (never coffee), inside the cap on "
         "how many are in one state", frequency=True),
    Dial("openup", "opening up", "level", "everyone", "the next night",
         "how readily they open up about themselves: the trust their next disclosure needs and "
         "whether they confide at night", frequency=True),
    Dial("reliability", "reliability", "score", "everyone", "the next raid",
         "whether they turn up on time for a summon (3 or less: late); never their discretion",
         frequency=True),
)}


@dataclass(frozen=True)
class WorldDial:
    name: str
    label: str
    lo: int
    hi: int
    section: str  # mind.toml's
    key: str
    when: str
    moves: str


WORLD_DIALS: dict[str, WorldDial] = {d.name: d for d in (
    WorldDial("chattiness", "chattiness", 0, 10, "talk", "chattiness", "at once",
              "how often the guild starts talking on its own outside raids"),
    WorldDial("romance", "romance", 1, 10, "romance", "dial", "at once",
              "how far the women's wording goes (a woman's own dial still wins for her)"),
    WorldDial("jealousy", "jealousy", 0, 10, "romance", "jealousy", "at once",
              "how many envy asides an hour, the women's rivalry and the night's jealousy"),
    WorldDial("selfies", "selfies", 0, 10, "talk", "selfies", "at once",
              "how many photos members send you a week: made at night, sent while you play"),
)}


def is_score(dial: str) -> bool:
    return MEMBER_DIALS[dial].kind == "score"


def bounds(dial: str) -> tuple[int, int]:
    return (SCORE_LO, SCORE_HI) if is_score(dial) else (LEVEL_LO, LEVEL_HI)


def base_value(dial: str, p) -> int:
    """The dial's value as the persona is written."""
    if dial == "argumentative":
        return SCORE_HI + 1 - p.big_five.agreeableness.score
    if dial == "reliability":
        return p.big_five.conscientiousness.score
    return LEVEL_NEUTRAL


def has_party_habit(p) -> bool:
    return any(h.substance in PARTY_SUBSTANCES for h in p.habits)


def applies(dial: str, p) -> bool:
    """Whether the dial means anything for this member: forward is for women only (men stay
    friends), party only for someone with a drinking or smoking habit."""
    who = MEMBER_DIALS[dial].who
    if who == "women":
        return p.identity.player_gender == "female"
    if who == "party":
        return has_party_habit(p)
    return True


def not_applicable(dial: str, p) -> str:
    """Why a dial does nothing for this member, in plain words ("" when it does)."""
    if applies(dial, p):
        return ""
    if MEMBER_DIALS[dial].who == "women":
        return "Men stay friends: this dial has no effect on him."
    return "They have no drinking or smoking habit, so this dial has nothing to move."
