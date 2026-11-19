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

from . import safe, wardrobe
from .kind import CLOTHED_OUTFITS, GYM_OUTFITS, LUST_COLOURS, SWIM_OUTFITS
from .prompt import names_re
from .scene import INDOOR_RE, Scene, _text_codes
from .style import LUST, SUGGESTIVE_MIN_AGE, looks_age_coded
from .styling_history import History
from .words_scene import BED_RE

# Short, so the prompt fit keeps them beside her identity (build_scene_prompt); a head tilt is
# a pose of its own (POSES["tilt"]), never said twice.
EXPRESSIONS = {
    "warm": ("a warm smile", "a relaxed smile", "a gentle smile", "a cheerful smile"),
    "playful": ("a bright grin", "a playful smile", "a happy laugh", "a quick wink"),
    "confident": ("a confident smile", "an affectionate smile", "a knowing smile",
                  "a delighted grin"),
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
GYM_PLACES = ("gym", "the gym")  # the enum keys/settings of the gym (scene_choices, kind)
# A short form of a catalogue outfit (short_outfit): its colour, each garment's noun with the
# word before it, a one-piece's length, and "over"/"and"; never fewer garments.
_OUTER = r"jackets?|coats?|raincoats?|blazers?|parkas?|windbreakers?|overshirts?"
_GARMENT = re.compile(rf"^(?:{safe._TOP}|{safe._BOTTOM}|{safe._ONE_PIECE}|{_OUTER})$", re.I)
_LENGTH = re.compile(rf"^(?:{safe._LENGTH})$", re.I)
_COLOUR_WORDS = frozenset(w for c in LUST_COLOURS for w in c.split())
_JOINS = frozenset(("and", "over", "a", "an"))


def boldness(template: str) -> int:
    """Fashion categories within the vetted, clothed catalogue; never a content threshold."""
    return 2 if any(t in template for t in _BOLD) else int(any(t in template for t in _FITTED))


def short_outfit(outfit: str, *, minimal: bool = False) -> str:
    """`outfit` (a composed catalogue phrase, "a cobalt blue cap-sleeve ribbed top and wide-leg
    cotton trousers") in a short form: what the prompt fit says when the whole one would cost
    her a required word or leave her expression or the activity no room (fit_choice). The short
    form keeps each garment's noun with the word before it ("a cobalt blue ribbed top and cotton
    trousers"), the `minimal` one only the nouns ("a cobalt blue top and trousers"); both keep its
    colour, a one-piece's length and "over" and "and": the same garments in the same colour,
    still a top and a bottom or a one-piece with its length (safe.is_complete). A phrase it
    cannot shorten so is returned as it is."""
    words = outfit.split()
    keep = set()
    for i, word in enumerate(words):
        if _GARMENT.match(word):
            keep.add(i)
            if not minimal and i and not _LENGTH.match(words[i - 1]):
                keep.add(i - 1)
        if _LENGTH.match(word) or word.lower() in _JOINS or word in _COLOUR_WORDS:
            keep.add(i)
    kept = [w for i, w in enumerate(words) if i in keep]
    if kept and kept[0].lower() in ("a", "an"):
        kept = kept[1:]
    if not kept:
        return outfit
    short = ("an " if kept[0][0].lower() in "aeiou" else "a ") + " ".join(kept)
    return short if safe.is_complete(short) and safe.is_complete(outfit) else outfit


def outfit_forms(outfit: str) -> list[str]:
    """A catalogue outfit (template_of) whole, then its short and minimal forms where they
    differ (short_outfit); any other outfit (swimwear, a lust list's, her wardrobe's, a uniform)
    only as it is."""
    if not template_of(outfit):
        return [outfit]
    return list(dict.fromkeys((outfit, short_outfit(outfit), short_outfit(outfit, minimal=True))))


def fit_choice(specs, outfits, expression: str, activity: str):
    """Of the prompts (PromptSpec) for one styled scene with its outfit said in each of `outfits`
    (outfit_forms: whole first), the most detailed one that costs nothing and keeps the most:
    its outfit is kept and every other required phrase is what the least detailed one's is (no
    hair style, skin, size, kind, framing or pose shortened or yielded for the outfit's words);
    of those, the ones that keep the expression, then the activity, where any form does
    (prompt._kept, the mind's count, which never counts below the worker's)."""
    from .prompt import _kept

    def required(spec):
        return [t for t, pr in spec.phrases if pr == 0 and not t.startswith("wearing ")]

    def kept(spec, text):
        return bool(text) and any(spec.phrases[i][0] == text for i in _kept(spec.phrases))
    least = required(specs[-1])
    fair = [spec for spec, outfit in zip(specs, outfits)
            if required(spec) == least and any(pr == 0 and outfit in t for t, pr in spec.phrases)]
    if not fair:
        return specs[-1]
    score = [(kept(spec, expression), kept(spec, activity)) for spec in fair]
    return fair[score.index(max(score))]


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
    # snapshots keep the action's pose. A selfie takes no body pose (safe.pose: an arm holds the
    # camera), only where she looks; a timer photo can turn when no activity was asked for.
    if framing == 'from_behind_over_shoulder' or (scene.kind == 'snapshot' and scene.activity):
        return ('',)
    out = ('direct', 'tilt')
    if (tone != 'warm' and scene.kind not in safe.SELFIE_KINDS
            and scene.framing not in safe.HEAD_FRAMINGS):
        out += ('angled',)
        if not scene.activity and framing == 'timer':
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
    from leeroy_mind.heat import live as heat_live  # [heat] set: her heat lowers it, 0 ends it
    legacy = dial_for(p, dial)
    dial = heat_live.member_dial(p, legacy)
    if dial != legacy and dial <= 0:
        return None
    return {"warm": "warm", "flirty": "playful", "suggestive": "confident",
            "explicit": "confident"}.get(effective_band(p.romance.stance, stage, dial))


def enum_category(data: dict) -> str | None:
    """Retain the chosen activity's wardrobe; never turn swimwear into evening wear. Gym wear
    only at the gym (the plain gym pairing, scene_choices.pairing_codes; the lust one,
    kind.screen_lust): warming up in the park takes the outdoors wardrobe."""
    if data["outfit"] in SWIM_OUTFITS:
        return 'swimwear'
    activity = data["activity"]
    if activity in ("exercise", "stretching") or data["outfit"] in GYM_OUTFITS:
        return "gym" if data.get("setting") in GYM_PLACES else "outdoors"
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
    including every name. The lust style's place pairings hold for every styled woman: a scene
    on a bed is never styled, and gym wear or swimwear indoors becomes casual.
    A collision keeps the original scene; it never bypasses a screen or retries a model.
    """
    if not enabled or scene.audience != "him" or category not in (
            "casual", "going_out", "warm", "pyjamas", "outdoors", "gym", "swimwear"):
        return scene
    if category == 'swimwear' and scene.style != LUST:
        return scene
    # The lust style's place pairings, for every styled woman: no styled outfit on a bed
    # (words_scene.BED_RE: a lust woman's place is never one), and gym wear or swimwear beside a
    # bed, sofa or the indoors (scene.INDOOR_RE) becomes casual.
    where = (scene.setting, scene.activity)
    if any(BED_RE.search(text) for text in where):
        return scene
    indoors = category in ("gym", "swimwear") and any(INDOOR_RE.search(t) for t in where)
    if indoors:
        category = "casual"
    tone = level(p, relationship, dial)
    if tone is None or (indoors and wardrobe.tags(p)):
        return scene  # her wardrobe would keep the scene's own clothes, which the indoors refuses
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
