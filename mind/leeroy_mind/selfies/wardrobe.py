"""A member's wardrobe and palette (owner, 2026-11-17), set by hand in the console's Looks panel
(schema.Appearance.wardrobe, palette): what they tend to wear and in which colours. They bias
the outfits the photos pick and never replace a list or add a looks word:

- The photo-kind pass (kind.py). A lust woman picks from the lust outfits her wardrobe's tags
  name and from her palette's colours (lust_schema: the same schema with shorter enums, so the
  pairings and screens are unchanged); everyone else, who writes a free-text kind, is told what
  they tend to wear in a sentence of fixed words (ask_hint), and the reply is screened as ever.
  A kind the pass chose is chosen once more when the wardrobe or palette changes (digest, which
  the kind's record keeps), and her queued sets wait for it (pending).
- Varied sets (varied.py): within each category the outfits her wardrobe names come first, and
  the colours are her palette's.
- Situational scenes (scene.py, scene_choices.py): the lust scene's enums as the kind pass's;
  a plain scene offers the scene outfits her wardrobe names (SCENE_OUTFITS, with one clothed
  outfit more for each tag the old four did not cover: SCENE_MORE).

Every outfit named here is one of the existing vetted lists', and each new string is clothed and
passes the explicit, wear and age screens (tests/selfies/test_wardrobe.py reads every one). The
swim tag counts only for a woman who has the lust style now, where the pairing rules allow
swimwear (at the water). With no wardrobe and no palette every function here answers what the
code did before: the same lists, the same schema object, no words."""
import hashlib
import json

from ..personas.schema import Persona
from .style import suggestive

# tag -> the lust and varied outfits (kind.LUST_OUTFITS, VARIED_OUTFITS choices) of that taste
LUST_TAGS: dict[str, tuple[str, ...]] = {
    "casual": ("off-the-shoulder top and jeans", "crop top and high-waisted shorts",
               "tank top and denim shorts", "fitted t-shirt and high-waisted jeans",
               "ribbed tank dress"),
    "sporty": ("sports top and leggings", "racerback sports top and leggings",
               "cropped sports top and high-waisted leggings", "fitted tank top and bike shorts",
               "tank top and denim shorts", "crop top and high-waisted shorts"),
    "smart": ("fitted blazer dress", "square-neck top and a pencil skirt",
              "cropped cardigan and wide-leg trousers", "satin midi dress"),
    "edgy": ("leather jacket over a crop top", "one-shoulder top and leather trousers",
             "cut-out dress", "silk blouse and a leather miniskirt"),
    "boho": ("flowy sundress", "sweetheart-neckline sundress", "backless summer dress",
             "off-the-shoulder top and jeans"),
    "glam": ("sequin party dress", "low-cut cocktail dress", "bodycon mini dress",
             "plunging wrap dress", "midi dress with a high slit", "fitted little dress",
             "halter top and a miniskirt"),
    "cosy": ("off-the-shoulder sweater dress", "cropped cardigan and wide-leg trousers"),
    "workwear": ("fitted blazer dress", "square-neck top and a pencil skirt",
                 "silk blouse and a leather miniskirt"),
    "vintage": ("sweetheart-neckline sundress", "satin midi dress",
                "square-neck top and a pencil skirt"),
    "outdoorsy": ("tank top and denim shorts", "crop top and high-waisted shorts",
                  "fitted t-shirt and high-waisted jeans"),
    "minimalist": ("ribbed tank dress", "fitted t-shirt and high-waisted jeans",
                   "cropped cardigan and wide-leg trousers", "fitted little dress"),
    "swim": ("string bikini", "halter bikini", "one-piece swimsuit",
             "high-cut one-piece swimsuit", "sarong over a bikini"),
}
LUST_ONLY = ("swim",)  # counts only for a woman of the lust style
# A plain scene's outfits (scene_choices.OUTFITS keys, then SCENE_MORE's) by tag. "exercise"
# stays the gym's own (scene_choices.pairing_codes), so sporty also names a track top.
SCENE_MORE = {"track": "a track jacket and joggers",
              "tailored": "a button-up shirt and tailored trousers",
              "dark": "a black denim jacket and dark jeans",
              "linen": "a loose linen shirt and trousers",
              "dressy": "a dressy shirt and tailored trousers",
              "classic": "a collared shirt and high-waisted trousers",
              "plain": "a plain tee and trousers"}
SCENE_TAGS: dict[str, tuple[str, ...]] = {
    "casual": ("casual",), "sporty": ("exercise", "track"), "smart": ("tailored",),
    "edgy": ("dark",), "boho": ("linen",), "glam": ("dressy",), "cosy": ("warm",),
    "workwear": ("tailored",), "vintage": ("classic",), "outdoorsy": ("outdoors",),
    "minimalist": ("plain",), "swim": (),
}
SCENE_GYM = "exercise"  # always offered: a gym scene needs it
# The sentence a free-text kind is asked with: fixed words by tag, nothing of the persona's
HINTS = {"casual": "casual everyday clothes", "sporty": "sporty, athletic clothes",
         "smart": "smart, tailored clothes", "edgy": "edgy, alternative clothes",
         "boho": "loose, flowing bohemian clothes", "glam": "dressed up for going out",
         "cosy": "cosy knitwear", "workwear": "what they wear to work",
         "vintage": "classic, timeless pieces", "outdoorsy": "practical outdoor clothes",
         "minimalist": "plain, simple pieces in solid colours"}
WARDROBE_HELD = "photo kind awaits a re-choice for the wardrobe"


def tags(p: Persona | None) -> tuple[str, ...]:
    """Her wardrobe's tags that count now: all of them, the lust-only ones (swim) only while she
    has the lust style."""
    a = p.appearance if p is not None else None
    if a is None:
        return ()
    lust = suggestive(p)
    return tuple(t for t in a.wardrobe if lust or t not in LUST_ONLY)


def palette(p: Persona | None) -> tuple[str, ...]:
    a = p.appearance if p is not None else None
    return () if a is None else tuple(a.palette)


def _named(p: Persona | None) -> set[str]:
    return {o for t in tags(p) for o in LUST_TAGS[t]}


def outfits(p: Persona | None, pool) -> tuple[str, ...]:
    """`pool` (lust or varied outfit choices, in order) narrowed to those her wardrobe names;
    all of `pool` with no wardrobe, or when it names none of them."""
    pool = tuple(pool)
    named = _named(p)
    return tuple(o for o in pool if o in named) or pool


def ordered(p: Persona | None, options: list, outfit=lambda x: x) -> list:
    """`options` with those whose outfit her wardrobe names first, each group in its order (a
    stable sort: with no wardrobe the list as it was)."""
    named = _named(p)
    return sorted(options, key=lambda x: outfit(x) not in named) if named else options


def colours(p: Persona | None, pool) -> list[str]:
    """`pool` (colours, in order) narrowed to her palette; all of it with none set."""
    pool, chosen = list(pool), set(palette(p))
    return [c for c in pool if c in chosen] or pool


def lust_schema(p: Persona | None, base: dict, *, swim_only_settings=()) -> dict:
    """`base` (kind.LUST_SCHEMA, scene.SCENE_SCHEMA_LUST) with its outfit enum narrowed to her
    wardrobe and its colour enum to her palette; `base` itself with neither set. With no
    swimwear left among the outfits, the settings that take only swimwear (the hot tub) go
    too, so no pick is left that the pairings must refuse."""
    props = base["properties"]
    wear = list(outfits(p, props["outfit"]["enum"]))
    tint = colours(p, props["colour"]["enum"])
    if wear == props["outfit"]["enum"] and tint == props["colour"]["enum"]:
        return base
    places = props["setting"]["enum"]
    if not set(wear) & set(LUST_TAGS["swim"]):
        places = [s for s in places if s not in swim_only_settings]
    return {**base, "properties": {
        **props, "outfit": {**props["outfit"], "enum": wear},
        "colour": {**props["colour"], "enum": tint},
        "setting": {**props["setting"], "enum": places}}}


def scene_outfits(p: Persona | None, base) -> tuple[str, ...]:
    """A plain scene's outfit keys for her: `base` (scene_choices.OUTFITS) with no wardrobe, or
    when it names none; else the keys her tags name, in SCENE_TAGS order, with the gym's own."""
    base = tuple(base)
    named = list(dict.fromkeys(k for t in tags(p) for k in SCENE_TAGS[t]))
    if not [k for k in named if k != SCENE_GYM]:
        return base
    return tuple(named if SCENE_GYM in named else [*named, SCENE_GYM])


def scene_schema(p: Persona | None, base: dict, outfit_keys) -> dict:
    """`base` (scene.SCENE_SCHEMA) with its outfit enum as scene_outfits says; `base` itself
    when that is the list it has."""
    keys = list(scene_outfits(p, outfit_keys))
    props = base["properties"]
    if keys == props["outfit"]["enum"]:
        return base
    return {**base, "properties": {**props, "outfit": {**props["outfit"], "enum": keys}}}


def ask_hint(p: Persona | None) -> str:
    """What a free-text kind's ask adds: a sentence of fixed words on what they tend to wear
    and their colours, or "" with neither set."""
    said = [HINTS[t] for t in tags(p) if t in HINTS]
    out = ""
    if said:
        out += f" They tend to wear {', '.join(said)}: dress them in keeping with it."
    if palette(p):
        out += f" Colours they favour in what they wear: {', '.join(palette(p))}."
    return out


def digest(p: Persona | None) -> str | None:
    """Her wardrobe and palette as stored, as a digest a kind's record keeps (kind._record);
    None with neither set, so a record from before them still matches."""
    a = p.appearance if p is not None else None
    if a is None or not (a.wardrobe or a.palette):
        return None
    text = json.dumps({"wardrobe": list(a.wardrobe), "palette": list(a.palette)}, sort_keys=True)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
