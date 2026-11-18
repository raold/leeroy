"""The photo tiers P0-P4: which of the vetted outfit and setting lists a heat value selects.

Every tier is a subset of lists that already exist (selfies/kind.py: LUST_OUTFITS, VARIED_OUTFITS,
LUST_SETTINGS, VARIED_LOOKS, VARIED_VIEWS) and each tier is a subset of the next. P4 is those
lists exactly as they are: at P4 every function here returns the list or schema object it was
given. Nothing is added: no outfit, setting, pose, screen or negative, and nothing here reads or
writes the NSFW gate or the age guard. Whether a member has the lust photo style at all
(style.suggestive: stance, 25 or older, looks that pass the age guard) is decided before any of
this is asked; everyone else keeps the plain rules untouched.

The split below is a proposal for the owner to edit (PR "Heat", ruling 6): moving a string from
one tuple to another is the whole change, and the import-time checks keep the tiers a partition
of the existing lists.

A lower tier filters only what is newly composed and what is sent. Nothing here deletes, hides
or re-renders a photo."""
from functools import lru_cache

from leeroy_mind.selfies import kind

from .table import P0, P1, P2, P3, P4

# -- outfits: the choice names of kind.LUST_OUTFITS and kind.VARIED_OUTFITS ---------------------
# P0 conservative: the most covered of the dressed outfits, and the two plain casual ones
P0_OUTFITS = ("fitted blazer dress", "cropped cardigan and wide-leg trousers",
              "square-neck top and a pencil skirt", "satin midi dress",
              "fitted t-shirt and high-waisted jeans", "flowy sundress")
# P1 dressed: the rest of the covered half of the dressed outfits
P1_OUTFITS = ("off-the-shoulder sweater dress", "sweetheart-neckline sundress",
              "off-the-shoulder top and jeans")
# P2 going out: every other dressed outfit
P2_OUTFITS = ("fitted little dress", "low-cut cocktail dress", "crop top and high-waisted shorts",
              "bodycon mini dress", "halter top and a miniskirt", "tank top and denim shorts",
              "backless summer dress", "plunging wrap dress",
              "one-shoulder top and leather trousers", "midi dress with a high slit",
              "sequin party dress", "cut-out dress", "ribbed tank dress",
              "leather jacket over a crop top", "silk blouse and a leather miniskirt")
# P3 gym wear (at the gym only: the pairings are kind.py's), P4 swimwear (at the water only)
P3_OUTFITS = tuple(sorted(kind.VARIED_GYM_OUTFITS))
P4_OUTFITS = tuple(sorted(kind.SWIM_OUTFITS))
_OUTFIT_STEPS = (P0_OUTFITS, P1_OUTFITS, P2_OUTFITS, P3_OUTFITS, P4_OUTFITS)
OUTFIT_TIER: dict[str, int] = {o: t for t, step in enumerate(_OUTFIT_STEPS) for o in step}

# -- settings: kind.LUST_SETTINGS ----------------------------------------------------------------
P0_SETTINGS = ("her living room couch", "the mirror in her hallway", "the front seat of her car",
               "a cafe terrace", "a park in summer")
P1_SETTINGS = ("a garden party", "a city street at night", "a ski lodge terrace")
P2_SETTINGS = ("a rooftop bar at night", "a wine bar", "a nightclub", "a dance floor",
               "a hotel balcony at dusk", "a concert crowd")
P3_SETTINGS = kind.GYM_SETTINGS
P4_SETTINGS = kind.WATER_SETTINGS
_SETTING_STEPS = (P0_SETTINGS, P1_SETTINGS, P2_SETTINGS, P3_SETTINGS, P4_SETTINGS)
SETTING_TIER: dict[str, int] = {s: t for t, step in enumerate(_SETTING_STEPS) for s in step}

# -- a varied set's poses and categories ----------------------------------------------------------
VIEW_TIER = {"front": P0, "side": P0, "rear_three_quarter": P2, "behind": P3}
CATEGORY_TIER = {"casual": P0, "going_out": P2, "gym": P3, "swim": P4}

# The tiers are a partition of the existing lists: nothing added, nothing left out.
_ALL_OUTFITS = set(kind.LUST_OUTFITS) | set(kind.VARIED_OUTFITS)
assert set(OUTFIT_TIER) == _ALL_OUTFITS and sum(map(len, _OUTFIT_STEPS)) == len(_ALL_OUTFITS)
assert set(SETTING_TIER) == set(kind.LUST_SETTINGS)
assert sum(map(len, _SETTING_STEPS)) == len(kind.LUST_SETTINGS)
assert set(VIEW_TIER) == set(kind.VARIED_VIEWS) and set(CATEGORY_TIER) == set(kind.CATEGORIES)


def clamp(tier) -> int:
    return P4 if type(tier) is not int else max(P0, min(P4, tier))


def outfit_ok(choice: str, tier: int) -> bool:
    return OUTFIT_TIER.get(choice, P4) <= clamp(tier)


def setting_ok(setting: str, tier: int) -> bool:
    return SETTING_TIER.get(setting, P4) <= clamp(tier)


def outfits(pool, tier: int):
    """`pool` (outfit choices, in order) as the tier allows; `pool` itself at P4."""
    if clamp(tier) >= P4:
        return pool
    return type(pool)(o for o in pool if outfit_ok(o, tier)) if isinstance(pool, (tuple, list)) \
        else tuple(o for o in pool if outfit_ok(o, tier))


def settings(pool, tier: int):
    """`pool` (settings, in order) as the tier allows; `pool` itself at P4."""
    if clamp(tier) >= P4:
        return pool
    return type(pool)(s for s in pool if setting_ok(s, tier)) if isinstance(pool, (tuple, list)) \
        else tuple(s for s in pool if setting_ok(s, tier))


def views(tier: int) -> tuple[str, ...]:
    """A varied set's poses at the tier, in kind.VARIED_VIEWS order."""
    return tuple(v for v in kind.VARIED_VIEWS if VIEW_TIER[v] <= clamp(tier))


def categories(tier: int) -> tuple[str, ...]:
    """A varied set's categories at the tier, in kind.CATEGORIES order."""
    return tuple(c for c in kind.CATEGORIES if CATEGORY_TIER[c] <= clamp(tier))


def varied_looks(category: str, tier: int) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """kind.VARIED_LOOKS[category]'s (outfits, settings) as the tier allows."""
    wear, places = kind.VARIED_LOOKS[category]
    return outfits(wear, tier), settings(places, tier)


def narrow_schema(base: dict, tier: int) -> dict:
    """A lust reply schema (kind.LUST_SCHEMA, scene.SCENE_SCHEMA_LUST) with its outfit and
    setting enums as the tier allows; `base` itself at P4, or when a narrowed enum would be
    empty (it never is for the lists as they are). The pairings and every screen still run on
    the reply as before: a shorter enum only ever removes picks."""
    if clamp(tier) >= P4:
        return base
    props = base["properties"]
    wear = [o for o in props["outfit"]["enum"] if outfit_ok(o, tier)]
    places = [s for s in props["setting"]["enum"] if setting_ok(s, tier)]
    if not wear or not places:
        return base
    return {**base, "properties": {**props, "outfit": {**props["outfit"], "enum": wear},
                                   "setting": {**props["setting"], "enum": places}}}


def picks_ok(data: dict, tier: int) -> bool:
    """A lust reply's outfit and setting are within the tier (the schema already says so; this is
    the check on the reply itself)."""
    return outfit_ok(data.get("outfit", ""), tier) and setting_ok(data.get("setting", ""), tier)


# -- which tier a stored photo is ------------------------------------------------------------------

@lru_cache(maxsize=1)
def _phrases() -> dict[str, str]:
    """Every outfit phrase the lists can compose (colour put in, no article) -> its choice."""
    templates = {**kind.LUST_OUTFITS, **kind.VARIED_OUTFITS}
    return {t.format(colour=c): choice for choice, t in templates.items()
            for c in kind.LUST_COLOURS}


def choice_of(phrase: str) -> str | None:
    """The list choice a composed outfit phrase ("a red halter bikini") stands for, or None."""
    words = " ".join((phrase or "").split())
    for article in ("a ", "an "):
        if words.startswith(article):
            words = words[len(article):]
            break
    return _phrases().get(words)


def scene_tier(scene) -> int:
    """The lowest tier a stored scene (a snap's scene mapping) fits. A plain-style scene is P0:
    the plain rules are not heat's. A lust scene is the higher of its outfit's and its setting's
    tier; a scene read from his words (a free-text place, the outfit by code) goes by its outfit
    alone, and one whose outfit is from no list (a work uniform, what she wears at the computer)
    is P0. Anything unreadable is P4: sent only where everything is."""
    try:
        if scene.get("style", "plain") != "lust":
            return P0
        words = scene.get("words")
        worn = scene.get("outfit", "")
        choice = worn if worn in OUTFIT_TIER else choice_of(worn)  # a reply's pick, or a phrase
        if isinstance(words, dict):
            if choice is None:
                return P0 if words.get("wearing") in ("work_uniform", "as_now") else P4
            return OUTFIT_TIER[choice]
        if choice is None:
            return P4
        return max(OUTFIT_TIER[choice], SETTING_TIER.get(scene.get("setting", ""), P4))
    except (AttributeError, TypeError, KeyError):
        return P4


_VIEW_WORDS = (("from behind", P3), ("three-quarter rear view", P2))


def prompt_tier(prompt) -> int:
    """The lowest tier a rendered lust photo fits, read from the prompt it was rendered with
    (a gallery candidate keeps its own: what it shows, whatever her photo kind is now). The
    highest tier among the list outfits, settings and poses the prompt names; P4 when it names
    no list outfit or no list setting (fail closed: sent only where everything is)."""
    if not isinstance(prompt, str) or not prompt:
        return P4
    text = " ".join(prompt.lower().split())
    wear = [OUTFIT_TIER[choice] for phrase, choice in _phrases().items() if phrase in text]
    places = [tier for place, tier in SETTING_TIER.items() if place.lower() in text]
    if not wear or not places:
        return P4
    poses = [tier for words, tier in _VIEW_WORDS if words in text]
    return max(wear + places + poses)


def look_tier(look) -> int:
    """The lowest tier a varied look (selfies.varied.Look) fits."""
    try:
        return max(OUTFIT_TIER[look.outfit], SETTING_TIER[look.setting], VIEW_TIER[look.view])
    except (AttributeError, KeyError):
        return P4


def listing(tier: int) -> dict:
    """What a tier allows, as the lists' own strings: the page's photo preview (text, no render)."""
    tier = clamp(tier)
    return {"tier": tier,
            "outfits": [o for o in OUTFIT_TIER if OUTFIT_TIER[o] <= tier],
            "settings": [s for s in SETTING_TIER if SETTING_TIER[s] <= tier],
            "views": list(views(tier))}
