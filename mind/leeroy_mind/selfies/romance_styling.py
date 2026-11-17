"""Opt-in, non-explicit styling for a private photo, after its original screens.

Romance selects clothes and expressions, never a destination, body, age or likeness.
No model call, I/O or logging. The composed scene is the immutable render recipe; old
photos and manually chosen profile kinds are untouched.
"""
import hashlib
from dataclasses import replace

from leeroy_mind.personas.schema import Persona
from leeroy_mind.talk.romance import dial_for, effective_band, forward

from . import wardrobe
from .kind import CLOTHED_OUTFITS, GYM_OUTFITS, LUST_COLOURS, SWIM_OUTFITS
from .prompt import names_re
from .scene import Scene, _text_codes
from .style import SUGGESTIVE_MIN_AGE, looks_age_coded

EXPRESSIONS = {
    "warm": ("a warm smile", "a relaxed smile", "a gentle smile", "a cheerful smile",
             "a warm smile with a slight head tilt"),
    "playful": ("a bright grin", "a playful smile", "a happy laugh", "a quick wink",
                "a playful smile with a slight head tilt"),
    "confident": ("a confident smile", "an affectionate smile", "a knowing smile",
                  "a delighted grin", "a confident smile with a slight head tilt"),
}


def level(p: Persona | None, relationship, dial: int) -> str | None:
    """The existing romance controls, mapped to three clothed, non-explicit styles."""
    if (p is None or p.identity.age < SUGGESTIVE_MIN_AGE or looks_age_coded(p)
            or p.identity.player_gender != "female" or forward(p) <= 0):
        return None
    stage = relationship.romance_stage if relationship is not None else "none"
    if stage == "ex":
        return None
    return {"warm": "warm", "flirty": "playful", "suggestive": "confident",
            "explicit": "confident"}.get(
                effective_band(p.romance.stance, stage, dial_for(p, dial)))


def enum_category(data: dict) -> str | None:
    """Retain the chosen activity's wardrobe; never turn swimwear into evening wear."""
    if data["outfit"] in SWIM_OUTFITS:
        return None
    activity = data["activity"]
    if activity in ("exercise", "stretching") or data["outfit"] in GYM_OUTFITS:
        return "gym"
    if activity in ("dog_walk", "walking", "hiking", "gardening", "watering", "birdwatching"):
        return "outdoors"
    if activity in ("cooking", "baking", "chopping", "repairing", "organizing"):
        return "casual"
    if "colour" in data:
        return "going_out"
    return {"casual": "casual", "warm": "warm", "outdoors": "outdoors", "exercise": "gym",
            "track": "casual", "tailored": "going_out", "dark": "casual", "linen": "casual",
            "dressy": "going_out", "classic": "going_out", "plain": "casual"}.get(data["outfit"])


def _draw(choices, seed, salt):
    digest = hashlib.sha256(f"{seed}|{salt}".encode()).digest()
    return choices[int.from_bytes(digest[:4], "big") % len(choices)]


def restyle(p: Persona | None, scene: Scene, *, enabled: bool, category: str | None,
            relationship, dial: int, seed, ident, names) -> Scene:
    """Style a screened private scene. Work uniforms, as-now clothes and swimwear keep
    their choices. A wardrobe selected by the owner keeps the scene's chosen outfit;
    otherwise new clothes use her colour palette. New phrases meet all the free-text screens,
    including every name.
    A collision keeps the original scene; it never bypasses a screen or retries a model.
    """
    if not enabled or scene.audience != "him" or category not in (
            "casual", "going_out", "warm", "pyjamas", "outdoors", "gym"):
        return scene
    tone = level(p, relationship, dial)
    if tone is None:
        return scene
    if category == "casual":
        pools = {"warm": ("casual",), "playful": ("casual", "polished"),
                 "confident": ("polished",)}[tone]
    elif category == "going_out":
        pools = {"warm": ("polished",), "playful": ("polished", "evening"),
                 "confident": ("evening",)}[tone]
    else:
        pools = ("warm" if category == "pyjamas" else category,)
    key = f"{p.guid}:{seed}"
    template = _draw(tuple(t for pool in pools for t in CLOTHED_OUTFITS[pool]), key, "outfit")
    phrase = template.format(colour=_draw(wardrobe.colours(p, LUST_COLOURS), key, "colour"))
    outfit = scene.outfit if wardrobe.tags(p) else ("an " if phrase[0] in "aeiou" else "a ") + phrase
    expression = _draw(EXPRESSIONS[tone], key, "expression")
    named = names_re(p)
    names = [n for n in (*names, ident.nickname, ident.character_name, ident.real_name) if n]
    # An unchanged outfit has already passed the scene's own screens (including the lust
    # enum path). Only new words pass the stricter free-text screens here.
    changed = [("expression", expression)]
    if outfit != scene.outfit:
        changed.append(("outfit", outfit))
    for field, text in changed:
        if ((named is not None and named.search(text))
                or _text_codes(field, text, ident=ident, names=names)):
            return scene
    # The caption and later memory must describe the same clothes the worker draws.
    shows = scene.shows.rstrip(".")
    if scene.outfit in shows:
        shows = shows.replace(scene.outfit, outfit)
    else:
        shows += f", wearing {outfit}"
    shows += f", {expression}."
    return replace(scene, outfit=outfit, expression=expression, shows=shows, romance_style=tone)
