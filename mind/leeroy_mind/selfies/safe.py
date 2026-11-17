"""Compositions the photo audit showed render badly, designed out (2026-11-17).

A visual audit of about 1,290 rendered photos (covers, profile sets, situational photos) tied
each defect to the composition that produced it. Headshots were clean (1%); defects grew with
how much of the body shows (three-quarter 11%, full-length 21%) and with what the hands touch.
The triggers, and what this module does about each:

- A mirror (mirror selfies, the hallway mirror: 25-31%, half of them a second or wrong
  reflection), a phone held in view, a car's front seat (the subject cloned as a passenger), a
  crowd: the kinds render as a plain selfie (KIND_WORDS), the retired settings are said another
  way (SETTING_WORDS) and are no longer offered (kind.LUST_SETTINGS), and the body negatives name
  the mirror and the phone.
- "bike shorts" drew a bicycle into the gym (38%, always garbled): the outfit's words change
  (OUTFIT_WORDS), its key stays.
- Hands on hips with another arm action gave a third hand; hands gripping a glass, a mug, a
  rail, a pen, sticks: a body shot gets one simple standing pose (pose), props rest beside the
  subject (scene_choices.PROPS) and activities are said as what is there, not what the hands do
  (scene_choices.ACTIVITIES).
- Seated and splayed swimwear poses merged legs and feet: every pose stands.
- Garments: an outfit phrase at priority 2 never reached the worker's 75 tokens (0 of 12 plain
  waist-up prompts kept it, 7 of 42 men's), which rendered men shirtless; one-shoulder and
  one-strap words fused garments; a top without its bottom read as bottomless. Every outfit now
  names a top and a bottom, or a one-piece with its length (complete, is_complete), a man always
  a shirt, a headshot a top with a neckline, and it is required (`wearing ...`, priority 0).

Eyes were never wrong at severity 2 or more in the audit (0 of 1,288), so the body negative
(BODY_ANATOMY) trades the eye words of the earlier anatomy line for a third hand, a duplicate
person, the mirror, the phone and the missing-garment words.

How much body shows is a setting, `[selfies] body_shot_share` (BODY_SHOT_SHARE by default): the
share of photos framed to show the body (a varied set's full-length and three-quarter
candidates, a lust profile kind's three-quarter, a situational photo's waist-up or wider).

Pure text and draws: no I/O, no logging. Every string here meets the age guard, the explicit
and wear screens (tests/selfies/test_safe_compositions.py iterates them)."""
import hashlib
import re

BODY_SHOT_SHARE = 0.5

# -- kinds, settings, outfits -------------------------------------------------------------------

# How a photo kind is said. A selfie names no phone and no mirror: the arm's-length framing is
# the selfie, and "phone selfie" and "mirror selfie" drew a phone, a second phone or a mirror
# with a wrong reflection into the picture.
KIND_WORDS = {"selfie": "selfie, close arm's-length shot",
              "mirror_selfie": "selfie, close arm's-length shot",
              "headshot": "professional headshot", "snapshot": "candid photo taken by a friend"}
KIND_SHORT = {"selfie, close arm's-length shot": "selfie"}

# A stored setting said so it renders (the kind keeps its text; only the prompt changes). The
# first three are retired from the lists as well (RETIRED_SETTINGS).
SETTING_WORDS = {
    "the mirror in her hallway": "her hallway at home",
    "the front seat of her car": "a quiet street beside her parked car",
    "a concert crowd": "an open-air festival field at dusk",
    "a garden party": "a summer garden",
    "a dance floor": "an empty dance floor",
    "a wine bar": "a quiet wine bar",
    "the gym": "an empty gym",
}
RETIRED_SETTINGS = ("the mirror in her hallway", "the front seat of her car", "a concert crowd")
NEW_SETTINGS = ("her hallway at home", "a sunny window seat at home", "a bookshop")

# A list outfit's words where the audit, or the clothed rule, asks for other ones: the key the
# model picks (and a stored plan holds) stays, the template that reaches a prompt changes. A
# dress names its length; a jacket its top and bottom; "bike shorts" are gym shorts; a
# one-shoulder top (a strap on one side only, sleeves fused) is a wrap top; a miniskirt under a
# short top read as bottomless.
OUTFIT_WORDS = {
    "fitted little dress": "fitted short {colour} dress",
    "low-cut cocktail dress": "low-cut midi {colour} cocktail dress",
    "plunging wrap dress": "plunging midi {colour} wrap dress",
    "fitted blazer dress": "fitted short {colour} blazer dress",
    "sweetheart-neckline sundress": "midi {colour} sweetheart-neckline sundress",
    "sequin party dress": "short {colour} sequin party dress",
    "off-the-shoulder sweater dress": "short {colour} off-the-shoulder sweater dress",
    "cut-out dress": "midi {colour} cut-out dress",
    "ribbed tank dress": "midi {colour} ribbed tank dress",
    "backless summer dress": "backless midi {colour} summer dress",
    "flowy sundress": "flowy midi {colour} sundress",
    "leather jacket over a crop top": "{colour} leather jacket over a crop top and jeans",
    "one-shoulder top and leather trousers": "{colour} wrap top and leather trousers",
    "halter top and a miniskirt": "{colour} halter top and a denim skirt",
    "fitted tank top and bike shorts": "fitted {colour} tank top and gym shorts",
}


def setting_words(setting: str) -> str:
    """A setting as its prompt says it (SETTING_WORDS), else as it is."""
    return SETTING_WORDS.get(setting, setting)


def _legacy_outfits() -> dict[str, str]:
    """A stored lust outfit phrase from before OUTFIT_WORDS ("a fitted little red dress") ->
    the phrase its choice composes now, for every colour."""
    from .kind import LUST_COLOURS, OLD_TEMPLATES, _article
    return {_article(OLD_TEMPLATES[choice].format(colour=colour)):
            _article(new.format(colour=colour))
            for choice, new in OUTFIT_WORDS.items() for colour in LUST_COLOURS}


_LEGACY: dict[str, str] | None = None


def outfit_words(outfit: str) -> str:
    """A stored list outfit as its prompt says it now: one composed before OUTFIT_WORDS takes its
    choice's current words (the stored kind keeps its text); anything else is as it is."""
    global _LEGACY
    if _LEGACY is None:
        _LEGACY = _legacy_outfits()
    return _LEGACY.get(outfit, outfit)


# -- clothed by construction ----------------------------------------------------------------------

_TOP = (r"tops?|t-?shirts?|tees?|shirts?|blouses?|polos?|jumpers?|sweaters?|sweatshirts?|"
        r"hoodies?|cardigans?|pullovers?|turtlenecks?|henleys?|jerseys?|tunics?|flannels?|"
        r"fleeces?|vests?|whites|scrubs")
_BOTTOM = (r"trousers|jeans|leggings|shorts|skirts?|miniskirts?|chinos|joggers|slacks|"
           r"sweatpants|culottes|scrubs|sarongs?")
_ONE_PIECE = r"dress|sundress|jumpsuit|dungarees|overalls|boilersuit|swimsuit|bikini"
_LENGTH = (r"mini|midi|maxi|short|knee-length|ankle-length|floor-length|full-length|"
           r"one-piece|bikini|swimsuit|jumpsuit|dungarees|overalls|boilersuit")
# a garment with a neckline of its own, for a head-and-shoulders photo
_NECK = (r"t-?shirts?|tees?|shirts?|blouses?|polos?|jumpers?|sweaters?|sweatshirts?|hoodies?|"
         r"cardigans?|pullovers?|turtlenecks?|henleys?|jerseys?|tunics?|flannels?|fleeces?|"
         r"crew-?neck|v-?neck|collar(?:ed)?|neckline|halter|scrubs|whites|dress|sundress|"
         r"jumpsuit|tops?")
# over a top: a jacket, coat, blazer or apron alone is no top
TOP_RE = re.compile(rf"\b(?:{_TOP})\b", re.I)
BOTTOM_RE = re.compile(rf"\b(?:{_BOTTOM})\b", re.I)
ONE_PIECE_RE = re.compile(rf"\b(?:{_ONE_PIECE})\b", re.I)
LENGTH_RE = re.compile(rf"\b(?:{_LENGTH})\b", re.I)
NECK_RE = re.compile(rf"\b(?:{_NECK})\b", re.I)
SHIRT_RE = re.compile(r"\b(?:t-?shirts?|tees?|shirts?|polos?|jumpers?|sweaters?|sweatshirts?|"
                      r"hoodies?|cardigans?|pullovers?|turtlenecks?|henleys?|jerseys?|flannels?|"
                      r"fleeces?|tunics?|whites|scrubs)\b", re.I)
HEAD_FRAMINGS = ("face", "head_and_shoulders")
BODY_FRAMINGS = ("three_quarter", "full_body")  # hands and legs in frame
DEFAULT_TOP = {"male": "a crew-neck t-shirt", "female": "a crew-neck top"}
DEFAULT_BOTTOM = "trousers"
WEAR_MAX_WORDS = 12  # a longer free-text outfit gives way to the default garments


def _top(gender: str) -> str:
    return DEFAULT_TOP["male" if gender == "male" else "female"]


def is_complete(outfit: str, *, gender: str = "female", head: bool = False) -> bool:
    """The clothed rule for one outfit phrase: a top and a bottom, or a one-piece with its
    length; a man always a shirt; for a head-and-shoulders photo (`head`) a top with a neckline
    is enough."""
    if gender == "male" and not SHIRT_RE.search(outfit):
        return False
    if head:
        return NECK_RE.search(outfit) is not None
    if ONE_PIECE_RE.search(outfit):
        return LENGTH_RE.search(outfit) is not None
    return TOP_RE.search(outfit) is not None and BOTTOM_RE.search(outfit) is not None


def complete(outfit: str, *, gender: str, framing: str) -> str:
    """`outfit` (a persona's or a list's words, already screened) made to meet the clothed rule
    for `framing`, by adding only default garments: a top with a neckline when it names none (a
    man: a shirt), and for a waist-up or wider photo a bottom when it names none. A blank or
    overlong one (WEAR_MAX_WORDS) is the default garments alone."""
    outfit = " ".join((outfit or "").split()).rstrip(".,;")
    head = framing in HEAD_FRAMINGS
    if not outfit or len(outfit.split()) > WEAR_MAX_WORDS:
        return _top(gender) if head else f"{_top(gender)} and {DEFAULT_BOTTOM}"
    one_piece = ONE_PIECE_RE.search(outfit) is not None and gender != "male"
    has_top = (SHIRT_RE if gender == "male" else NECK_RE if head else TOP_RE).search(outfit)
    if not one_piece and not has_top:
        outfit = f"{outfit} over {_top(gender)}"
    if not head and not one_piece and not BOTTOM_RE.search(outfit):
        outfit = f"{outfit} and {DEFAULT_BOTTOM}"
    return outfit


def wearing(outfit: str, *, gender: str, framing: str) -> str:
    """The required clothing phrase of a plain prompt. Free text that names swimwear, underwear
    or sleepwear (kind.EXPLICIT_RE, WEAR_RE: a kind set by hand, or one stored before those
    screens) is never required: the default garments stand in, and the negative keeps its
    words."""
    from .kind import EXPLICIT_RE, WEAR_RE
    if EXPLICIT_RE.search(outfit or "") or WEAR_RE.search(outfit or ""):
        outfit = ""
    return f"wearing {complete(outfit, gender=gender, framing=framing)}"


# -- poses ----------------------------------------------------------------------------------------

# One small set of standing poses per view, hands simple or out of sight (the audit: no hand in
# view 1.6% defective, one or more 12%; a hand on the hip 44%, and every third hand had one).
# None puts a hand on a hip, a rail, a glass or anything else, and none sits or kneels.
POSES = {
    "front": ("standing, arms at her sides", "standing, hands behind her back"),
    "side": ("standing, arms at her sides",),
    "behind": ("standing, arms at her sides",),
    "rear_three_quarter": ("standing, arms at her sides",),
}
POSE_WAIST_UP = "arms down, hands out of frame"  # a waist-up photo
ALONE = "alone, no one else in frame"  # a busy place (a bar, a club, a street, a party)
BUSY_RE = re.compile(r"\b(?:bar|nightclub|club|dance floor|street|party|festival|concert|cafe|"
                     r"terrace|lobby|bookshop|pool deck)\b", re.I)


def _draw(seed, salt: str) -> float:
    digest = hashlib.sha256(f"{seed}|{salt}".encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2 ** 64


def pose(view: str, framing: str, seed="", *, pos: str = "her") -> str:
    """The pose phrase for a photo seen from `view` ("front" for an ordinary one), framed
    `framing`: a standing pose of POSES for a body shot, POSE_WAIST_UP for a waist-up one, none
    for a head-and-shoulders or face photo. Deterministic in `seed`."""
    if framing in HEAD_FRAMINGS:
        return ""
    if framing not in BODY_FRAMINGS:
        return POSE_WAIST_UP
    poses = POSES.get(view, POSES["front"])
    return poses[int(_draw(seed, "pose") * len(poses))].replace("her", pos)


def pose_priority(framing: str) -> int:
    """A body shot's pose is required (0): it is what keeps a hand off her hip; a waist-up
    photo's is kept when it fits (1)."""
    return 0 if framing in BODY_FRAMINGS else 1


def alone(setting: str) -> str:
    """ALONE for a place where other people would be (BUSY_RE), else ""."""
    return ALONE if BUSY_RE.search(setting or "") else ""


# -- how much body shows --------------------------------------------------------------------------

def body_shot(share: float, seed, salt: str = "body") -> bool:
    """A seeded draw: True for `share` of the seeds."""
    return _draw(seed, salt) < share


def settle_framing(framing: str, share: float, seed, *, wide=BODY_FRAMINGS,
                   narrow: str = "upper_body") -> str:
    """`framing`, or `narrow` when it is one of `wide` and the seed's draw falls outside
    `share`: with share 1 every framing stays, with 0 none of `wide` does."""
    return framing if framing not in wide or body_shot(share, seed) else narrow


def body_slots(share: float, n: int = 4) -> int:
    """How many of a varied set's `n` candidates are body shots."""
    return max(0, min(n, round(share * n)))


# -- negatives ------------------------------------------------------------------------------------

# A body shot's anatomy line, at priority 0. The earlier line (scene.SNAP_ANATOMY until
# 2026-11-17) spent 10 tokens on eyes, which the audit never found wrong, and 11 on "extra
# limbs", "missing fingers" and "mutated, disfigured" beside the arms, legs, hands and fingers
# it already named; they go to a third hand and the two lines below.
BODY_ANATOMY = ("extra arms, extra legs, extra hands, fused fingers, extra fingers, "
                "malformed hands, deformed feet")
BODY_GARMENT = "bottomless, shirtless"  # a top with no bottom, a man with no shirt
# garbled signs and equipment lettering came with the busy places: "text" is required here
BODY_ALONE = "another person, duplicate person, mirror, phone, text"
