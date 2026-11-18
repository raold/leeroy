"""Opt-in, non-explicit styling for a private photo, after its original screens.

Romance selects clothes and expressions, never a destination, body, age or likeness.
No model call, I/O or logging. The composed scene is the immutable render recipe; old
photos and manually chosen profile kinds are untouched.
"""
import hashlib
import re
from dataclasses import replace

from leeroy_mind.personas.schema import Persona
from leeroy_mind.talk.romance import dial_for, effective_band, forward

from . import wardrobe
from .kind import CLOTHED_OUTFITS, GYM_OUTFITS, LUST_COLOURS, SWIM_OUTFITS
from .prompt import names_re
from .scene import Scene, _text_codes
from .style import LUST, SUGGESTIVE_MIN_AGE, looks_age_coded
from .styling_history import History

EXPRESSIONS = {
    "warm": ("a warm smile", "a relaxed smile", "a gentle smile", "a cheerful smile",
             "a warm smile with a slight head tilt"),
    "playful": ("a bright grin", "a playful smile", "a happy laugh", "a quick wink",
                "a playful smile with a slight head tilt"),
    "confident": ("a confident smile", "an affectionate smile", "a knowing smile",
                  "a delighted grin", "a confident smile with a slight head tilt"),
}
POSES = {"direct": "looking directly at the camera", "tilt": "head tilted slightly",
         "angled": "shoulders turned slightly toward the camera",
         "glance": "looking over one shoulder at the camera"}
LIGHTS = {"soft_daylight": "soft daylight on the face",
          "side_daylight": "gentle daylight from the side",
          "soft_lamp": "soft warm lamp light", "side_lamp": "warm lamplight from the side",
          "golden": "warm golden evening light"}
_INDOOR = re.compile(r"\b(room|kitchen|hallway|desk|office|couch|sofa|cafe|gym)\b", re.I)
_OUTDOOR = re.compile(r"\b(beach|pool|park|garden|rooftop|terrace|street|trail|dock|boat|balcony)\b", re.I)
_TEMPLATES = frozenset(t for pool in CLOTHED_OUTFITS.values() for t in pool)
_COLOURS = re.compile(r"\b(?:" + '|'.join(re.escape(c) for c in LUST_COLOURS) + r")\b")
_BOLD = ("crop", "mini", "off-the-shoulder", "off-shoulder", "one-shoulder", "halter",
         "sweetheart", "bodycon")
_FITTED = ("fitted", "ruched", "sleeveless", "scoop-neck", "wrap", "shorts", "leggings")


def boldness(template: str) -> int:
    """Fashion categories within the vetted, clothed catalogue; never a content threshold."""
    return 2 if any(t in template for t in _BOLD) else int(any(t in template for t in _FITTED))


def template_of(outfit: str) -> str:
    phrase = outfit.removeprefix('an ').removeprefix('a ')
    template = _COLOURS.sub('{colour}', phrase, count=1)
    return template if template in _TEMPLATES else ''


def features(template: str) -> frozenset[str]:
    if template not in _TEMPLATES:
        return frozenset()
    return frozenset(k for k, terms in {
        'dress': ('dress',), 'skirt': ('skirt',), 'shorts': ('shorts',),
        'trousers': ('trousers', 'jeans', 'chinos'), 'knit': ('knit', 'sweater', 'cardigan'),
        'fitted': _FITTED, 'date': _BOLD, 'sport': ('training', 'sports', 'athletic', 'exercise'),
    }.items() if any(t in template for t in terms))


def _preference(history, field, choice):
    """A soft bias, capped at 2x either way. Silence never contributes a vote."""
    credit = 0.0
    mine = features(choice) if field == 'outfit' else None
    for scene, vote in history.rated:
        old = scene.get(field, '')
        if field == 'outfit':
            other = template_of(old)
            shared = mine & features(other)
            credit += vote * (1 if choice == other else len(shared) / max(1, len(mine)) * .5)
        elif field == 'colour':
            match = _COLOURS.search(scene.get('outfit', ''))
            credit += vote if match and match[0] == choice else 0
        else:
            credit += vote if old == choice else 0
    return 2 ** max(-1, min(1, credit / 3))


def _choose(choices, seed, field, history, *, tone=None):
    choices = tuple(choices)
    recent = [s.get(field, '') for s in history.recent[:8 if field == 'outfit' else 1]]
    if field == 'outfit':
        recent = [template_of(x) for x in recent]
    choices = tuple(c for c in choices if c not in recent) or choices
    weights = [_preference(history, field, c) * (
        {'warm': (8, 2, 1), 'playful': (2, 6, 4), 'confident': (1, 3, 10)}[tone][boldness(c)]
        if field == 'outfit' else 1) for c in choices]
    point = int.from_bytes(hashlib.sha256(f'{seed}|{field}'.encode()).digest()[:8], 'big') / 2**64
    point *= sum(weights)
    for choice, weight in zip(choices, weights):
        point -= weight
        if point < 0:
            return choice
    return choices[-1]


def pose_choices(scene: Scene, tone: str, framing=None) -> tuple[str, ...]:
    # A requested rear view already has its own required pose and identity scale. Activity
    # snapshots keep the action's pose; a mirror/timer can turn when no activity was asked for.
    if framing == 'from_behind_over_shoulder' or (scene.kind == 'snapshot' and scene.activity):
        return ('',)
    out = ('direct', 'tilt')
    if tone != 'warm' and scene.framing not in ('face', 'head_and_shoulders'):
        out += ('angled',)
        if not scene.activity and framing in ('mirror_selfie', 'timer'):
            out += ('glance',)
    return out


def light_choices(scene: Scene, lighting=None) -> tuple[str, ...]:
    if lighting is not None:  # preserve the source of light he asked for
        return {'daylight': ('soft_daylight', 'side_daylight'),
                'lamp': ('soft_lamp', 'side_lamp')}.get(lighting, ('',))
    if scene.when in ('morning', 'day'):
        return ('soft_daylight', 'side_daylight')
    if scene.when == 'evening' and _OUTDOOR.search(scene.setting):
        return ('golden',)
    if scene.when in ('evening', 'late') and _INDOOR.search(scene.setting):
        return ('soft_lamp', 'side_lamp')
    return ('',)


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
        return 'swimwear'
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


def restyle(p: Persona | None, scene: Scene, *, enabled: bool, category: str | None,
            relationship, dial: int, seed, ident, names, history: History = History(),
            framing=None, lighting=None) -> Scene:
    """Style a screened private scene. Work uniforms, as-now clothes and swimwear keep
    their choices. A wardrobe selected by the owner keeps the scene's chosen outfit;
    otherwise new clothes use her colour palette. New phrases meet all the free-text screens,
    including every name.
    A collision keeps the original scene; it never bypasses a screen or retries a model.
    """
    if not enabled or scene.audience != "him" or category not in (
            "casual", "going_out", "warm", "pyjamas", "outdoors", "gym", "swimwear"):
        return scene
    if category == 'swimwear' and scene.style != LUST:
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
    outfit = scene.outfit
    if not wardrobe.tags(p) and category != 'swimwear':
        template = _choose(tuple(t for pool in pools for t in CLOTHED_OUTFITS[pool]), key,
                           'outfit', history, tone=tone)
        phrase = template.format(colour=_choose(wardrobe.colours(p, LUST_COLOURS), key, 'colour', history))
        outfit = ("an " if phrase[0] in "aeiou" else "a ") + phrase
    expression = _choose(EXPRESSIONS[tone], key, 'expression', history)
    pose = _choose(pose_choices(scene, tone, framing), key, 'romance_pose', history)
    light = _choose(light_choices(scene, lighting), key, 'romance_light', history)
    named = names_re(p)
    names = [n for n in (*names, ident.nickname, ident.character_name, ident.real_name) if n]
    # An unchanged outfit has already passed the scene's own screens (including the lust
    # enum path). Only new words pass the stricter free-text screens here.
    changed = [("expression", expression), ('pose', POSES.get(pose, '')),
               ('light', LIGHTS.get(light, ''))]
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
    shows += f", {expression}"
    if pose:
        shows += ', ' + POSES[pose]
    if light:
        shows += ', ' + LIGHTS[light]
    return replace(scene, outfit=outfit, expression=expression, shows=shows + '.',
                   romance_style=tone, romance_pose=pose, romance_light=light)
