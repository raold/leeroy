"""What a situational photo shows (P4 Task 6): the scene the reflection model writes for a
member's coming days, its screen, and the prompts that render it with the member's own face.

A scene is the kind of photo (a phone selfie, a mirror selfie or a snapshot a friend took), its
framing, where it is, what they are doing, what they wear, their expression, the time of day it
belongs to (PHASES), who may see it (him only, or the whole guild) and one sentence of what it
shows. The reply is screened, never fixed, and a refusal names fields and codes only, never the
text. Adults only, clothed, no children, the member alone in it (a pet is fine), their looks
exactly as they are, no era, no likeness and no names.

The member's style (style.py) picks the rules, as it does for the profile photo (controller
ruling R2). Everyone but a lust woman writes the plain scene: SCENE_PROMPT (overlay
prompts/nightly_scene.txt), SCENE_SCHEMA, free text in every field. A woman of the lust style
writes the kind, framing, time of day, audience, activity and what it shows, and picks setting,
outfit, colour and expression from the profile photo's vetted lists in kind.py (SCENE_SCHEMA_LUST,
SCENE_PROMPT_LUST, overlay prompts/nightly_scene_lust.txt; the built-in text stays non-explicit):
kind.screen_lust checks the pairings (swimwear only by the water and framed upper body, the gym
outfit only at the gym, the hot tub only in swimwear) and kind.compose_lust puts the colour into
the outfit. Her free text (activity, shows) meets the same screen as everyone's, with the age
guard (AGE_RE), the wear guard (WEAR_RE) and UNDRESS_RE (controller rulings R12, R13). Her
outfit and setting render as one priority-0 phrase, prompt.lust_phrase, so the worker's fit
keeps both or neither, with the lust negative plus SNAP_NEGATIVE's "another person"; a lust
scene renders only while she still has the lust style (scene_renderable)."""
import re
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from leeroy_mind.human import HumanIdentity
from leeroy_mind.nightly.enrich import LIKENESS_RE
from leeroy_mind.nightly.llm import schema, system_prompt
from leeroy_mind.personas.schema import Persona
from leeroy_mind.personas.screens import has_word, names_real_name
from leeroy_mind.text import has_meta

from .kind import (ERA_RE, EXPLICIT_RE, LUST_COLOURS, LUST_EXPRESSIONS, LUST_OUTFITS,
                   LUST_SETTINGS, WEAR_RE, _enum, compose_lust, screen_lust)
from .prompt import (_FRAMINGS, _HEADSHOT_TAIL, _KINDS, _PHONE_TAIL, LUST_HELD, LUST_NAMED,
                     LUST_NEGATIVE, NEGATIVE, PromptSpec, eyes_face, hair_skin, looks_phrases,
                     lust_phrase, names_re, phrase_adder, renderable, subject_phrase)
from .style import AGE_RE, LUST, PLAIN, STYLES, style_of, suggestive

SCENE_NAME = "scene"             # its overlay: prompts/nightly_scene.txt
SCENE_LUST_NAME = "scene_lust"   # its overlay: prompts/nightly_scene_lust.txt

KINDS = ("selfie", "mirror_selfie", "snapshot")
FRAMINGS = ("face", "head_and_shoulders", "upper_body")
WHENS = ("morning", "day", "evening", "late", "any")
AUDIENCES = ("him", "anyone")


def _text(n: int) -> dict:
    return {"type": "string", "maxLength": n}


_COMMON = {"photo": {"type": "boolean"}, "kind": _enum(KINDS), "framing": _enum(FRAMINGS)}
_TAIL = {"when": _enum(WHENS), "audience": _enum(AUDIENCES), "shows": _text(120)}
SCENE_SCHEMA = schema(SCENE_NAME, {
    **_COMMON, "setting": _text(80), "activity": _text(80), "outfit": _text(80),
    "expression": _text(40), **_TAIL})

# The lust style's settings for a situational photo: her profile photo's, less the one that
# names a crowd (she is the only person in a situational photo).
SCENE_LUST_SETTINGS = tuple(s for s in LUST_SETTINGS if s != "a concert crowd")
SCENE_SCHEMA_LUST = schema(SCENE_LUST_NAME, {
    **_COMMON, "setting": _enum(SCENE_LUST_SETTINGS), "activity": _text(80),
    "outfit": _enum(LUST_OUTFITS), "colour": _enum(LUST_COLOURS),
    "expression": _enum(LUST_EXPRESSIONS), **_TAIL})

SCENE_PROMPT = (
    "Choose a photo the real person who plays {name} might take in the coming days and send to "
    "a friend from the guild.\n{card}\n{facts}\nHow they look: {looks}\n"
    "What is going on: {about}\n"
    "Say whether they would take a photo of it at all. If they would, describe it: the kind of "
    "photo (a phone selfie, a mirror selfie, or a snapshot a friend took), the framing (face, "
    "head and shoulders, or upper body), where it is (a place in their own life, in a few "
    "words), what they are doing in it, what they are wearing, their expression, the time of "
    "day it belongs to (morning, day, evening, late night, or any), whether it is only for the "
    "friend they send it to or fine for the whole guild, and in one plain sentence what the "
    "photo shows. They are the only person in it; a pet is fine. Their looks stay exactly as "
    "described: no new haircut or hair colour, no glasses they don't wear, no beard they don't "
    "have, no change in weight. Name no year, no decade, no device and no brand. Fully clothed, "
    "nothing revealing or sexual, no children, never like anyone famous, and no one else's "
    "name.")
SCENE_PROMPT_LUST = (
    "Choose a photo the real person who plays {name} might take in the coming days and send to "
    "a friend from the guild, the kind she sends when she wants to be noticed.\n{card}\n"
    "{facts}\nHow she looks: {looks}\nWhat is going on: {about}\n"
    "Say whether she would take a photo of it at all. If she would, she knows she looks good "
    "and likes to show it a little: a flattering, flirtatious, confident photo that fits who "
    "she is and what is going on. Pick the kind of photo, the framing, where it is, what she is "
    "wearing and its colour, and her expression from the choices the answer allows. Then say "
    "in a few words what she is doing in it, never what she wears; the time of day it belongs "
    "to (morning, day, evening, late night, or any); whether it is only for the friend she "
    "sends it to or fine for the whole guild; and in one plain sentence what the photo shows, "
    "again without what she wears. She is the only person in it; a pet is fine. Her looks stay "
    "exactly as described: no new haircut or hair colour, no glasses she doesn't wear, no "
    "change in weight. Swimwear only at the beach, the pool deck, the lakeside dock, the hot "
    "tub, the boat or the beach bar, framed upper body; the hot tub only in swimwear; the "
    "sports top and leggings only at the gym. Name no year, no decade, no device and no brand, "
    "never like anyone famous, and no one else's name.")
SCENE_ASK = "Choose {name}'s photo."
ABOUT = {"life": "Tonight in their life away from the game: {beat}",
         "birthday": "It is their birthday tomorrow.",
         "everyday": "An ordinary day for them this week."}

SNAP_NEGATIVE = NEGATIVE + (("another person, crowd, group of people", 1),)
SNAP_NEGATIVE_LUST = LUST_NEGATIVE + (("another person, crowd, group of people", 1),)

# A looks change: her looks stay exactly as described in a situational photo. "Weights" and
# "a glass of wine" are not one.
LOOKS_RE = re.compile(
    r"\b(hair ?cut|new (?:hair|hairstyle|look|glasses|specs)|dyed|dyes?|bleach(?:ed)?|"
    r"highlights|fringe|bangs|perm|buzz ?cut|shaved (?:head|off)|beard|moustache|mustache|"
    r"goatee|stubble|eyeglasses|spectacles|wearing glasses|contact lenses|tattoo(?:s|ed)?|"
    r"piercings?|pierced|weight|slimmer|thinner|makeover|braces|wig|extensions)\b", re.I)
# A child in the scene: nobody else is in it, and never a child.
MINOR_RE = re.compile(
    r"\b(child|children|kids?|kiddo|bab(?:y|ies)|toddlers?|infants?|sons?|daughters?|boys?|"
    r"girls?|teens?|teenagers?|nieces?|nephews?|grand(?:child|children|son|daughter)s?|"
    r"schoolkids?|pupils?)\b", re.I)

# Undressing and bathing (controller ruling R12): a towel, a shower or a bath, changing clothes,
# being undressed, underwear, sheets or a robe, in any scene's free text whatever the style.
# Whole words only, so a robin and a wardrobe are not a robe; "getting dressed up" is dressed.
UNDRESS_RE = re.compile(
    r"\b(towel(?:s|ing|ling|ed|led)?|shower(?:s|ing|ed)?|bath(?:s|ing|ed)?|bathes|"
    r"bathtubs?|bubble[\s-]*baths?|undress(?:es|ing|ed)?|"
    r"chang(?:e|es|ed|ing)(?: (?:her|his|their|my|your))? clothes|"
    r"(?:get|gets|getting|got) changed|(?:get|gets|getting|got) dressed(?! up)|"
    r"naked|nudes?|topless|bras?|underwear|lingerie|sheets|bed[\s-]*sheets?|robes?|"
    r"bath[\s-]*robes?)\b", re.I)  # a clothed scene in bed or a bedroom is fine (owner, R12)

_TEXT_FIELDS = ("setting", "activity", "outfit", "expression", "shows")
_LUST_TEXT_FIELDS = ("activity", "shows")  # the rest are her picks
_ENUMS = {"kind": KINDS, "framing": FRAMINGS, "when": WHENS, "audience": AUDIENCES}
OTHER_STYLE = "scene: picks for another style"

_LIGHT = {"morning": "soft morning light", "day": "daylight", "evening": "warm evening light",
          "late": "at night, lit by a lamp or a screen", "any": None}
_LATE_TAIL = _PHONE_TAIL.replace("natural light, ", "")
_CROPS = {"face": (0.0, 0.0, 1.0, 1.0), "head_and_shoulders": (0.125, 0.05, 0.875, 0.8),
          "upper_body": (0.25, 0.05, 0.75, 0.55)}

PHASES = {"morning": ("morning",), "day": ("day",), "evening": ("evening",),
          "late": ("late", "dead"), "any": ("morning", "day", "evening", "late", "dead")}
LIFE_DAYS = 3
EVERYDAY_DAYS = 5
GUILD_DAYS = 3
BIRTHDAY_S = 64800.0
_DAY_S = 86400.0
_LASTS = {"life": LIFE_DAYS * _DAY_S, "raid": GUILD_DAYS * _DAY_S,
          "everyday": EVERYDAY_DAYS * _DAY_S, "birthday": BIRTHDAY_S}


@dataclass(frozen=True)
class Scene:
    kind: str
    framing: str
    setting: str
    activity: str
    outfit: str
    expression: str
    when: str
    audience: str
    shows: str
    style: str = PLAIN  # LUST: setting, outfit and expression were her picks (R2)


def scene_from(data: dict) -> Scene:
    """The Scene a screened reply stands for, or the one scene_json stored. A lust reply (it has
    a colour) is composed as her profile photo is (kind.compose_lust: the colour put into the
    outfit, an article in front) and is of the lust style; a stored one says its style."""
    outfit, style = data["outfit"], data.get("style", PLAIN)
    if "colour" in data:
        outfit, style = compose_lust({f: data[f] for f in (
            "kind", "framing", "setting", "outfit", "colour", "expression")}).outfit, LUST
    if style not in STYLES:
        raise ValueError("scene: unknown style")
    return Scene(**{f.name: data[f.name] for f in fields(Scene)
                    if f.name not in ("outfit", "style")}, outfit=outfit, style=style)


def scene_json(s: Scene) -> dict:
    """What a snap row stores: the scene's fields, and its style only when it is the lust one
    (a plain scene keeps the plain shape)."""
    out = asdict(s)
    if s.style == PLAIN:
        del out["style"]
    return out


def scene_schema(p: Persona | None) -> dict:
    """The reply schema for p's scene: the lust style's picks, or the plain free text."""
    return SCENE_SCHEMA_LUST if style_of(p) == LUST else SCENE_SCHEMA


def scene_system(p: Persona | None, data_dir: Path, *, human: HumanIdentity, name: str,
                 card: str, facts: str, looks: str, about: str) -> str:
    """The scene request's system prompt for p under p's style: the owner's overlay
    prompts/nightly_scene_lust.txt or SCENE_PROMPT_LUST for a lust woman, else
    prompts/nightly_scene.txt or SCENE_PROMPT."""
    if style_of(p) == LUST:
        return system_prompt(SCENE_LUST_NAME, SCENE_PROMPT_LUST, data_dir, human=human,
                             name=name, card=card, facts=facts, looks=looks, about=about)
    return system_prompt(SCENE_NAME, SCENE_PROMPT, data_dir, human=human, name=name, card=card,
                         facts=facts, looks=looks, about=about)


def _text_codes(field: str, text: str, *, ident: HumanIdentity, names: list[str]) -> list[str]:
    found = [("meta", has_meta(text)), ("real_name", names_real_name(text, ident)),
             ("likeness", LIKENESS_RE.search(text) is not None),
             ("member_name", any(has_word(text, n) for n in names)),
             ("era", ERA_RE.search(text) is not None),
             ("explicit", EXPLICIT_RE.search(text) is not None
              or WEAR_RE.search(text) is not None),
             ("minor", MINOR_RE.search(text) is not None),
             ("looks", LOOKS_RE.search(text) is not None),
             ("age", AGE_RE.search(text) is not None),
             ("undress", UNDRESS_RE.search(text) is not None)]
    return [f"scene.{field}: {code}" for code, hit in found if hit]


def screen_scene(p: Persona, data: dict, *, ident: HumanIdentity,
                 member_names) -> list[str]:
    """The problems with a scene reply for `p`, as codes `scene.<field>: <code>` only, never the
    text. Per free-text field, in order: meta, his real name (real_name), a likeness, another
    member's name (member_name, any case), an era, explicit wear (EXPLICIT_RE and WEAR_RE, as
    kind.screen_kind), a minor, a looks change, anything that codes a person as young (AGE_RE,
    age) and undressing or bathing (UNDRESS_RE, undress); a clothed bed or bedroom scene is
    fine. The reply must be p's style's shape (OTHER_STYLE otherwise). A lust reply's
    free text is its activity and shows; its picks meet kind.screen_lust: on its list, the
    pairings, her name in a part, the age guard (its `photo.` codes reported as `scene.`)."""
    lust = style_of(p) == LUST
    if lust != ("colour" in data):
        return [OTHER_STYLE]
    names = [n for n in member_names if n]
    codes = [f"scene.{f}: not on its list" for f, values in _ENUMS.items()
             if data.get(f) not in values]
    for field in _LUST_TEXT_FIELDS if lust else _TEXT_FIELDS:
        codes += _text_codes(field, data.get(field) or "", ident=ident, names=names)
    if lust:
        if data.get("setting") not in SCENE_LUST_SETTINGS:
            codes.append("scene.setting: not on its list")
        picks = {f: data.get(f) for f in ("kind", "framing", "setting", "outfit", "colour",
                                          "expression")}
        codes += ["scene" + m[len("photo"):] if m.startswith("photo") else m
                  for m in screen_lust(p, picks)]
    return list(dict.fromkeys(codes))


def scene_renderable(p: Persona | None, s: Scene) -> str | None:
    """None when s can be rendered for p, else why not: renderable's reasons, then a lust scene
    while she no longer has the lust style (LUST_HELD: it is never rendered plainly) or while
    her name filter would drop its joined phrase (LUST_NAMED, a rename since)."""
    missing = renderable(p)
    if missing:
        return missing
    if s.style == LUST and not suggestive(p):
        return LUST_HELD
    named = names_re(p)
    if s.style == LUST and named is not None and named.search(lust_phrase(s)):
        return LUST_NAMED
    return None


def build_scene_prompt(p: Persona, s: Scene) -> PromptSpec:
    """The prompt for scene s with p's face, in this order: kind, framing, subject (0), the
    activity (1), the setting (1), hair and skin (1), looking at the camera (3, not a snapshot),
    eyes and face details (2), the outfit (2), the light of its time of day (2), the expression
    (3), the build (3) and the phone look (1, without natural light late at night). A lust
    scene's outfit and setting go in as one priority-0 phrase where the setting stands, with
    SNAP_NEGATIVE_LUST. Free text holding the persona's names is dropped. ValueError when
    scene_renderable says why not."""
    missing = scene_renderable(p, s)
    if missing:
        raise ValueError(missing)
    lust = s.style == LUST
    name_re = names_re(p)
    out: list[tuple[str, int]] = []
    add = phrase_adder(out, name_re)
    add(_KINDS[s.kind], 0)
    add(_FRAMINGS[s.framing], 0)
    add(*subject_phrase(p))
    add(s.activity, 1, True)
    if lust:  # one phrase: the fit keeps both or neither; scene_renderable checked the names
        add(lust_phrase(s), 0, True)
    else:
        add(s.setting, 1, True)
    out += hair_skin(p, name_re)
    if s.kind != "snapshot":
        add("looking at the camera", 3)
    out += eyes_face(p, name_re)
    if not lust:
        add(s.outfit, 2, True)
    if _LIGHT[s.when]:
        add(_LIGHT[s.when], 2)
    add(s.expression, 3, True)
    add(f"{p.appearance.build} build", 3)
    add(_LATE_TAIL if s.when == "late" else _PHONE_TAIL, 1)
    return PromptSpec(tuple(out), SNAP_NEGATIVE_LUST if lust else SNAP_NEGATIVE)


def build_tile_prompt(p: Persona) -> PromptSpec:
    """A member's tile of the guild photo, the raid on a video call: alone at their desk at
    night with a headset, in what they wear at the computer. The same for every style.
    ValueError when renderable says why not."""
    missing = renderable(p)
    if missing:
        raise ValueError(missing)
    name_re = names_re(p)
    out: list[tuple[str, int]] = []
    add = phrase_adder(out, name_re)
    add("frame from a video call", 0)
    add("head and shoulders", 0)
    add(*subject_phrase(p))
    add("at their desk at home at night, wearing a headset, lit by the monitor", 1)
    out += looks_phrases(p, name_re)
    add(p.appearance.at_the_pc, 2, True)
    add("looking at the screen, smiling", 3)
    add(_HEADSHOT_TAIL, 1)
    return PromptSpec(tuple(out), SNAP_NEGATIVE)


def ref_crop(framing: str) -> tuple[float, float, float, float]:
    """The square of the chosen photo the identity reference is cut from, as fractions (left,
    top, right, bottom): the whole of a face photo, the face of a wider one, top-aligned."""
    return _CROPS[framing]


def window(source: str, night_end: float) -> tuple[float, float]:
    """(not_before, expires) for a photo planned from `source` the night that ends at
    `night_end`: from then, for 3 days (life, raid), 5 days (everyday) or 18 hours (birthday)."""
    if source not in _LASTS:
        raise ValueError(f"unknown photo source {source!r}")
    return night_end, night_end + _LASTS[source]
