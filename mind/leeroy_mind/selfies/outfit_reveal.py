"""Optional clothing coverage, independent of romance and photo-frequency controls.

Only fixed, opaque beach/gym outfits are added. Existing age, style, heat-category,
name and place rules decide whether a subject can wear them. Omission preserves
the old choices exactly; the setting never changes image classification.
"""
import hashlib
from dataclasses import replace

from .style import AGE_RE, LUST, suggestive

# Each level is a coverage choice within the activity, rather than a different
# activity. A level-five gym photo remains gymwear, never swimwear or underwear.
POOLS = {
    "swim": (
        ("rash-guard swim top and swim shorts", "high-neck one-piece swimsuit"),
        ("tankini top and swim briefs", "sporty one-piece swimsuit"),
        ("triangle bikini top and full-coverage bikini bottoms", "halter bikini top and bikini briefs"),
        ("tie-side bikini with high-cut cheeky bottoms", "halter bikini with high-leg cheeky bottoms"),
        ("opaque triangle bikini top and thong bikini bottoms", "opaque halter bikini top and thong bikini bottoms"),
    ),
    "gym": (
        ("loose athletic tee and full-length leggings", "long-sleeved workout top and track trousers"),
        ("fitted workout tank top and full-length leggings", "sleeveless sports top and capri leggings"),
        ("cropped athletic top and high-waisted leggings", "fitted sports crop top and workout shorts"),
        ("racerback sports crop top and short fitted gym shorts", "scoop-neck sports crop top and high-waisted training shorts"),
        ("opaque strappy sports crop top and short compression shorts", "opaque low-back sports crop top and short fitted running shorts"),
    ),
}
CATALOG = {choice: "{colour} " + choice for levels in POOLS.values()
           for options in levels for choice in options}
CATEGORY = {choice: category for category, levels in POOLS.items()
            for options in levels for choice in options}
HEAT_TIER = {"swim": 4, "gym": 3}


def valid(level) -> bool:
    return type(level) is int and 1 <= level <= 5


# The top swim level is never photographed from behind: its varied look trades views with a
# casual or going-out one (restyle_looks). 2026-11-21: level 5 from behind scored 1.0 on every
# seed, and a slot refused three times refuses its whole set.
BACK_SAFE_MAX = 4
TRADE_WITH = ("casual", "going_out")


def choices(category: str, level: int) -> tuple[str, ...]:
    return POOLS[category][level - 1] if category in POOLS and valid(level) else ()


def allowed(p, category: str, level) -> bool:
    if not valid(level) or not suggestive(p) or category not in POOLS:
        return False
    from leeroy_mind.heat import live as heat_live
    return heat_live.photo_tier(p) >= HEAT_TIER[category]


def _index(seed, size: int) -> int:
    return int.from_bytes(hashlib.sha256(str(seed).encode()).digest()[:8], "big") % size


def pick(p, category: str, level, seed, colour: str, names=()) -> str | None:
    """A known choice whose final phrase survives every applicable name/age screen."""
    if not allowed(p, category, level):
        return None
    from .prompt import names_re
    from .kind import LUST_COLOURS
    if colour not in LUST_COLOURS:
        return None
    candidates = choices(category, level)
    offset = _index(seed, len(candidates))
    named = names_re(p)
    for choice in candidates[offset:] + candidates[:offset]:
        phrase = CATALOG[choice].format(colour=colour)
        if AGE_RE.search(phrase) or (named is not None and named.search(phrase)):
            continue
        # Fixed swimwear uses the existing enum exception, never a free-text
        # exception. Other fields still receive their usual independent screens.
        if any(name and name.casefold() in phrase.casefold() for name in names):
            continue
        return choice
    return None


def restyle_look(p, look, level, seed):
    from .varied import screen
    choice = pick(p, look.category, level, seed, look.colour)
    if choice is None:
        return look
    updated = replace(look, outfit=choice)
    return look if screen(p, updated) else updated


def restyle_looks(p, looks, level, seed):
    """A varied set's looks (None for a slot of her ordinary photo), each restyled as
    restyle_look does with seed "<seed>:<slot>". A swim look above BACK_SAFE_MAX with a back
    view trades view and framing with a casual or going-out look seen from the front or side,
    when both still pass the screens; with no such trade it keeps its view at BACK_SAFE_MAX.
    The set keeps one of each view, and a set restyled once comes back unchanged."""
    from .kind import BACK_VIEWS
    from .varied import screen
    out = [None if x is None else restyle_look(p, x, level, f"{seed}:{i}")
           for i, x in enumerate(looks)]
    if not valid(level) or level <= BACK_SAFE_MAX:
        return tuple(out)
    for i, x in enumerate(out):
        if x is None or x.category != "swim" or x.view not in BACK_VIEWS or x.outfit not in choices("swim", level):
            continue
        for j, y in enumerate(out):
            if y is None or y.category not in TRADE_WITH or y.view in BACK_VIEWS:
                continue
            front, back = replace(x, view=y.view, framing=y.framing), replace(y, view=x.view, framing=x.framing)
            if not screen(p, front) and not screen(p, back):
                out[i], out[j] = front, back
                break
        else:
            out[i] = restyle_look(p, x, BACK_SAFE_MAX, f"{seed}:{i}")
    return tuple(out)


def profile(p, level, seed, *, kind_style: str):
    """An ordinary profile render keeps its kind, setting, framing and expression."""
    if not valid(level) or kind_style != LUST or p.photo is None or not suggestive(p):
        return p
    from . import kind, wardrobe
    from leeroy_mind.heat.photos import choice_of
    previous = choice_of(p.photo.outfit)
    category = ("swim" if previous in kind.SWIM_OUTFITS else
                "gym" if previous in kind.VARIED_GYM_OUTFITS else CATEGORY.get(previous))
    if category is None:
        return p
    colours = wardrobe.colours(p, kind.LUST_COLOURS)
    colour = colours[_index(seed, len(colours))]
    choice = pick(p, category, level, seed, colour)
    if choice is None:
        return p
    data = {"kind": p.photo.kind, "framing": p.photo.framing, "setting": p.photo.setting,
            "outfit": choice, "colour": colour, "expression": p.photo.expression}
    return p if kind.screen_lust(p, data) else replace(p, photo=kind.compose_lust(data))


def restyle_scene(p, scene, *, level, category, seed, names=()):
    """Only private, eligible beach/gym scenes change; captions match their outfits."""
    category = "swim" if category == "swimwear" else category
    if scene.audience != "him" or scene.style != LUST or not allowed(p, category, level):
        return scene
    from . import kind, wardrobe
    from .scene import INDOOR_RE
    from .words_scene import BED_RE, WATER_RE, GYM_PLACE_RE
    if any(BED_RE.search(t) or INDOOR_RE.search(t) for t in (scene.setting, scene.activity)):
        return scene
    if (category == "swim" and scene.setting not in kind.WATER_SETTINGS
            and not WATER_RE.search(scene.setting)):
        return scene
    if category == "gym" and not GYM_PLACE_RE.search(scene.setting):
        return scene
    colours = wardrobe.colours(p, kind.LUST_COLOURS)
    colour = colours[_index(seed, len(colours))]
    choice = pick(p, category, level, seed, colour, names)
    if choice is None:
        return scene
    outfit = kind.outfit_phrase(choice, colour)
    shows = scene.shows.replace(scene.outfit, outfit) if scene.outfit in scene.shows else (
        scene.shows.rstrip(".") + f", wearing {outfit}.")
    return replace(scene, outfit=outfit, shows=shows, outfit_reveal=level)


def scene_valid(scene) -> bool:
    """Stored reveal metadata is only accepted with an exact catalogue outfit/place."""
    from .kind import LUST_COLOURS, WATER_SETTINGS
    from .words_scene import BED_RE, WATER_RE, GYM_PLACE_RE
    from .scene import INDOOR_RE
    if not valid(scene.outfit_reveal) or scene.audience != "him" or scene.style != LUST:
        return False
    if any(BED_RE.search(t) or INDOOR_RE.search(t) for t in (scene.setting, scene.activity)):
        return False
    for category in POOLS:
        if (category == "swim" and scene.setting not in WATER_SETTINGS
                and not WATER_RE.search(scene.setting)):
            continue
        if category == "gym" and not GYM_PLACE_RE.search(scene.setting):
            continue
        for choice in choices(category, scene.outfit_reveal):
            for colour in LUST_COLOURS:
                phrase = CATALOG[choice].format(colour=colour)
                if scene.outfit in ("a " + phrase, "an " + phrase):
                    return True
    return False
