"""The night's photo-kind pass (P2 Task 3): the reflection model chooses the profile picture the
real person who plays a member would put on their own profile, as the persona's `photo` (kind,
framing, setting, outfit, expression), once the member has looks.

A reply is screened, never fixed. A refused one is logged by field and kind only (never the
reply's text), stored nowhere, and not asked again the same night. A save re-reads the stored
persona and fills `photo` only while it is still what the pass read (empty, for a first kind),
so a hand setting made while the pass ran wins.

The member's style (style.py) picks the rules. Everyone but a lust woman writes a free-text
kind from KIND_PROMPT, screened by screen_kind (EXPLICIT_RE as before, and the age guard AGE_RE
for everyone). A woman of the lust style writes no text: KIND_PROMPT_LUST (the owner's overlay
prompts/nightly_photo_lust.txt replaces it) asks her to pick each part from fixed, vetted lists
(LUST_SCHEMA: setting, outfit, colour, expression; kind and framing keep their enums), the code
builds the kind from the picks (compose_lust), swimwear goes only with the water settings and
the gym outfit only with the gym, the hot tub only in swimwear, swimwear only framed upper body
and never as a headshot (screen_lust; a pairing that fails is asked once more).

Over time: each kind the pass saves gets a record in meta (STYLE_KEY: the style that chose it
and a digest of the kind as saved). When a member's style no longer matches her kind's record
(she moved into lust, or out of it), the pass chooses her kind once more under her style now.
A kind that differs from its record was changed by hand, and one with no record and no photo
unit was set by hand: neither is ever chosen again. Kinds from before the records are the
pass's plain ones when it handled the member; the first night records them (ADOPTED)."""
import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import replace
from typing import TYPE_CHECKING, Callable

from leeroy_mind.human import HumanIdentity
from leeroy_mind.logs import log
from leeroy_mind.nightly.budget import register_counter
from leeroy_mind.nightly.enrich import LIKENESS_RE, _facts, _race_class
from leeroy_mind.nightly.ground import screen_text
from leeroy_mind.nightly.llm import ReflectionError, schema, system_prompt
from leeroy_mind.personas.io import load_persona, save_persona
from leeroy_mind.personas.schema import (Appearance, Persona, ProfilePhoto,
                                         persona_json_schema, validate_persona)
from leeroy_mind.personas.screens import has_word, names_real_name
from leeroy_mind.storage.store import Store
from leeroy_mind.text import has_meta

from . import wardrobe
from .prompt import (HAIR_COLOUR_WORDS, HAIR_STYLE_WORDS, lust_named, lust_phrase,
                     names_re)
from .safe import (BODY_SHOT_SHARE, NEW_SETTINGS, OUTFIT_WORDS, RETIRED_SETTINGS,
                   selfie_framing, settle_framing)
from .store import queue_restyled
from .style import AGE_RE, LUST, PLAIN, STYLES, style_of, suggestive

if TYPE_CHECKING:
    from leeroy_mind.nightly.runner import NightContext

STAGE = "photo"
LUST_PROMPT = "photo_lust"  # its overlay: prompts/nightly_photo_lust.txt
STYLE_KEY = "selfies:photo-style:{guid}"
ADOPTED = "selfies:photo-style:adopted"

KIND_PROMPT = (
    "Choose the profile picture the real person who plays {name} would put on their own social "
    "or work profile.\n{card}\n{facts}\nHow they look: {looks}\n"
    "Choose what fits who they are, their work, age, region, habits and personality: the kind "
    "of photo (a phone selfie, a mirror selfie, a professional headshot, or a snapshot a friend "
    "took), the framing (face, head and shoulders, or upper body), where it was taken (a place "
    "in their own life, in a few words), what they are wearing in it, and their expression. "
    "Name no year, no decade, no device and no brand. Fully clothed, nothing revealing or "
    "sexual, never like anyone famous, and no one else's name.")
KIND_PROMPT_LUST = (
    "Choose the profile picture the real person who plays {name} would put on her own social "
    "profile, the photo she posts when she wants to be noticed.\n{card}\n{facts}\n"
    "How she looks: {looks}\n"
    "She knows she looks good and likes to show it a little: pick a flattering, flirtatious, "
    "confident photo that fits who she is, her age, region, habits and personality. Pick each "
    "part from the choices the answer allows: the kind of photo, the framing, where it was "
    "taken, what she is wearing and its colour, and her expression. Swimwear only at the "
    "beach, the pool deck, the lakeside dock, the hot tub, the boat or the beach bar, framed "
    "upper body and never as a headshot; the hot tub only in swimwear; the sports top and "
    "leggings only at the gym.")
KIND_ASK = "Choose {name}'s profile picture."
LUST_ASK_AGAIN = (" Pick again: swimwear only at the beach, the pool deck, the lakeside dock, "
                  "the hot tub, the boat or the beach bar, framed upper body and never as a "
                  "headshot; the hot tub only in swimwear; the sports top and leggings only at "
                  "the gym; no part that shares a word with her name.")

_FIELDS = ("kind", "framing", "setting", "outfit", "expression")
_PROPS = persona_json_schema(enrichment=True)["properties"]["photo"]["anyOf"][0]["properties"]
KIND_SCHEMA = schema("photo", {k: _PROPS[k] for k in _FIELDS})

# The lust style's choices (owner, 2026-11-07, controller round 3). The model picks one value
# of each list and writes no text of its own; the code builds the stored kind from them, so the
# template, or the owner's overlay of it, can flavour the choice but never add a word. Every
# value was vetted to stay within the line: clothed, nothing see-through, no underwear or
# sleepwear, no bed, bath, shower or changing room, nothing sexual, nothing young-looking.
LUST_COLOURS = ("black", "red", "navy", "emerald", "burgundy", "cobalt blue", "teal", "coral",
                "plum", "olive", "pink", "gold")
_SWIM = ("{colour} string bikini", "{colour} halter bikini", "{colour} one-piece swimsuit",
         "{colour} high-cut one-piece swimsuit", "{colour} sarong over a bikini")
_GYM = ("{colour} sports top and leggings",)
_DRESSED = (
    "fitted little {colour} dress", "low-cut {colour} cocktail dress",
    "off-the-shoulder {colour} top and jeans", "{colour} crop top and high-waisted shorts",
    "{colour} bodycon mini dress", "{colour} halter top and a miniskirt",
    "{colour} satin midi dress", "{colour} tank top and denim shorts",
    "backless {colour} summer dress", "plunging {colour} wrap dress",
    "fitted {colour} blazer dress", "{colour} one-shoulder top and leather trousers",
    "{colour} sweetheart-neckline sundress", "{colour} midi dress with a high slit",
    "cropped {colour} cardigan and wide-leg trousers", "{colour} sequin party dress",
    "{colour} off-the-shoulder sweater dress", "{colour} cut-out dress",
    "{colour} ribbed tank dress", "{colour} leather jacket over a crop top",
    "{colour} square-neck top and a pencil skirt", "{colour} silk blouse and a leather miniskirt")


def _choice(template: str) -> str:
    return template.replace("{colour} ", "")


def _article(phrase: str) -> str:
    return f"{'an' if phrase[0].lower() in 'aeiou' else 'a'} {phrase}"


def _templates(templates) -> dict[str, str]:
    """choice -> how its colour goes in: the words the audit asked for (safe.OUTFIT_WORDS: a
    dress with its length, a jacket with its top and bottom, no bike shorts, no one-shoulder
    top), else the template itself. The choice (the key) never changes."""
    return {_choice(t): OUTFIT_WORDS.get(_choice(t), t) for t in templates}


# the outfit the model picks -> how its colour goes in
LUST_OUTFITS: dict[str, str] = _templates(_DRESSED + _SWIM + _GYM)
SWIM_OUTFITS = frozenset(_choice(t) for t in _SWIM)
GYM_OUTFITS = frozenset(_choice(t) for t in _GYM)
WATER_SETTINGS = ("the beach at sunset", "a hotel pool deck", "a lakeside dock",
                  "a hot tub on a wooden deck", "the deck of a sailing boat", "a beach bar")
GYM_SETTINGS = ("the gym",)
SWIM_ONLY_SETTINGS = ("a hot tub on a wooden deck",)  # nothing but swimwear there
# The mirror, the car's front seat and the concert crowd are retired (safe.RETIRED_SETTINGS: a
# wrong reflection, a cloned passenger, a crowd of faces); three plain places take their slots.
# A stored kind that names a retired one keeps its text and renders as safe.SETTING_WORDS says.
LUST_SETTINGS = WATER_SETTINGS + GYM_SETTINGS + (
    "a rooftop bar at night", "a city street at night", "a cafe terrace", "a park in summer",
    "a hotel balcony at dusk", "a nightclub", "her living room couch", *NEW_SETTINGS,
    "a wine bar", "a dance floor", "a garden party", "a ski lodge terrace")
assert not set(RETIRED_SETTINGS) & set(LUST_SETTINGS)
# The lust style's photo kinds: never a mirror selfie (25% defective in the audit, half of them
# a second or wrong reflection); a stored one renders as a selfie (safe.KIND_WORDS).
LUST_KINDS = ("selfie", "snapshot", "headshot")
# Its framings: waist-up or three-quarter, as every lust kind chosen so far (the outfit lists
# name no neckline for a closer photo).
LUST_FRAMINGS = ("upper_body", "three_quarter")
LUST_EXPRESSIONS = ("a playful smile", "a sultry look", "a confident smirk", "a teasing grin",
                    "an over-the-shoulder glance", "a wink", "biting her lip", "laughing")
PAIRINGS = ("photo.outfit: swimwear away from the water",
            "photo.outfit: gym wear away from the gym",
            "photo.setting: only swimwear in the hot tub",
            "photo.framing: swimwear only framed upper body",
            "photo.kind: no swimwear in a headshot")

# Varied sets (owner, 2026-11-14; varied.py): the four candidates of one set each get their own
# look, composed by code from these lists, one of each category, never four dresses. The lists
# reuse the vetted lust values and add a few gym and casual ones vetted the same way (clothed,
# nothing see-through, no underwear or sleepwear, nothing young-looking). They are not in
# LUST_OUTFITS: the night's photo-kind pass and LUST_SCHEMA are unchanged.
_GYM_MORE = ("{colour} racerback sports top and leggings",
             "cropped {colour} sports top and high-waisted leggings",
             "fitted {colour} tank top and bike shorts")
_CASUAL_MORE = ("fitted {colour} t-shirt and high-waisted jeans", "flowy {colour} sundress")
# No string bikini in a varied set: its swimwear is shot full-length, and from behind
VARIED_OUTFITS: dict[str, str] = {
    **{k: v for k, v in LUST_OUTFITS.items() if k != "string bikini"},
    **_templates(_GYM_MORE + _CASUAL_MORE)}
# choice -> its template before safe.OUTFIT_WORDS, to read a stored kind's outfit (safe.outfit_words)
OLD_TEMPLATES = {_choice(t): t for t in _DRESSED + _SWIM + _GYM + _GYM_MORE + _CASUAL_MORE
                 if _choice(t) in OUTFIT_WORDS}
assert set(OLD_TEMPLATES) == set(OUTFIT_WORDS)
VARIED_GYM_OUTFITS = GYM_OUTFITS | frozenset(_choice(t) for t in _GYM_MORE)
VARIED_LOOKS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {  # category: outfits, settings
    "swim": (tuple(_choice(t) for t in _SWIM if _choice(t) in VARIED_OUTFITS),
             tuple(s for s in WATER_SETTINGS if s not in SWIM_ONLY_SETTINGS)),
    "gym": (tuple(_choice(t) for t in _GYM + _GYM_MORE), GYM_SETTINGS),
    "casual": (("off-the-shoulder top and jeans", "crop top and high-waisted shorts",
                "tank top and denim shorts", "sweetheart-neckline sundress",
                *(_choice(t) for t in _CASUAL_MORE)),
               ("a cafe terrace", "a park in summer", "a garden party", "a city street at night")),
    "going_out": (("halter top and a miniskirt", "one-shoulder top and leather trousers",
                   "silk blouse and a leather miniskirt", "square-neck top and a pencil skirt",
                   "leather jacket over a crop top", "bodycon mini dress", "cut-out dress"),
                  ("a rooftop bar at night", "a nightclub", "a wine bar", "a dance floor",
                   "a hotel balcony at dusk")),
}
CATEGORIES = tuple(VARIED_LOOKS)
# The pose each candidate is photographed in, and how much of her is in frame: her figure shows
VARIED_VIEWS = ("behind", "rear_three_quarter", "side", "front")
BACK_VIEWS = ("behind", "rear_three_quarter")
VARIED_FRAMINGS = ("full_body", "three_quarter", "upper_body")
VARIED_BODY_FRAMINGS = ("full_body", "three_quarter")  # her figure shows (safe.body_slots)
VIEW_OUTSIDE = "photo.view: a pose only in a varied set"
SWIM_BACK = "photo.framing: swimwear from behind only full-length"
# a composed part that holds her character or real name (a colour or a season can be one): the
# prompt's name filter would drop the whole joined phrase, so the pick is asked once more
NAME_CLASHES = tuple(f"photo.{f}: shares her name" for f in ("setting", "outfit", "expression"))


def _enum(values) -> dict:
    return {"type": "string", "enum": list(values)}


LUST_SCHEMA = schema(LUST_PROMPT, {
    "kind": _enum(LUST_KINDS), "framing": _enum(LUST_FRAMINGS), "setting": _enum(LUST_SETTINGS),
    "outfit": _enum(LUST_OUTFITS), "colour": _enum(LUST_COLOURS),
    "expression": _enum(LUST_EXPRESSIONS)})

ERA_RE = re.compile(
    r"\b(1[89]\d\d|20\d\d|'?\d0s|retro|vintage|old[- ]fashioned|antique|flip[- ]?phone|webcam|"
    r"crt|camcorder|polaroid|disposable camera|digital camera|myspace)\b", re.I)
EXPLICIT_RE = re.compile(
    r"\b(nude|naked|topless|shirtless|braless|lingerie|underwear|bra|panties|bikini|swimsuit|"
    r"swimwear|see-through|sheer|sexy|sexual|seductive|nsfw|cleavage)\b", re.I)
# Underwear, sleepwear and see-through wear EXPLICIT_RE does not name, refused beside it in every
# free-text kind (controller ruling, safety): a teddy bear is not clothing and stays allowed.
WEAR_RE = re.compile(
    r"\b(bralettes?|bralets?|bustiers?|babydolls?|baby-dolls?|teddy(?![- ]bears?)|teddies|"
    r"body ?stockings?|neglig[eé]es?|nightgowns?|nightdress(?:es)?|nighties?|chemises?|"
    r"camisoles?|pajamas?|pyjamas?|pjs|sleepwear|nightwear|mesh|fishnets?|translucent)\b", re.I)


def looks_line(a: Appearance) -> str:
    """One sentence from the appearance's fields, in schema order."""
    face = f", {', '.join(a.face)}" if a.face else ""
    # the hair the owner set in the Looks panel stands in for the looks' own (prompt.hair_words)
    colour = HAIR_COLOUR_WORDS[a.owner_hair_colour] if a.owner_hair_colour else a.hair_colour
    length = a.owner_hair_length or a.hair_length
    style = HAIR_STYLE_WORDS[a.owner_hair_style] if a.owner_hair_style else a.hair_style
    return (f"{a.height_cm} cm, {a.build} build, {a.weight_kg} kg, {colour} "
            f"{length} hair worn {style}, {a.skin_tone} skin, {a.eye_colour} "
            f"eyes{face}; at the computer they wear {a.at_the_pc}.")


def needs_kind(p: Persona | None) -> bool:
    """A stored persona with looks and no photo kind yet."""
    return p is not None and p.appearance is not None and p.photo is None


def _digest(photo: ProfilePhoto) -> str:
    """The kind's five fields (_FIELDS) only, so a field added to ProfilePhoto later does not
    make every recorded kind look changed by hand."""
    text = json.dumps({f: getattr(photo, f) for f in _FIELDS}, sort_keys=True,
                      ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


_STORED = object()  # _record: the wardrobe and palette of the persona as stored now


def _record(store: Store, guid: int, style: str, photo: ProfilePhoto, worn=_STORED) -> None:
    """Which rules chose `photo` for `guid`: the style and the kind's digest, never its text,
    and the digest of the wardrobe and palette it was chosen with (wardrobe.digest; left out
    with neither set), so a later change of them shows. `worn` is that digest: the photo pass
    gives the one of the persona it read (an edit while its model call ran is then still
    pending); every other writer (restyle, variety, world photos) records the stored one."""
    rec = {"style": style, "kind": _digest(photo)}
    if worn is _STORED:
        try:
            worn = wardrobe.digest(load_persona(store, guid))
        except ValueError:  # unreadable: nothing to say of her wardrobe
            worn = None
    if worn is not None:
        rec["wardrobe"] = worn
    store.set_meta(STYLE_KEY.format(guid=guid), json.dumps(rec, sort_keys=True))


def release_wardrobe(store: Store, p: Persona, *, night: str, reason: str) -> bool:
    """The re-choice for p's wardrobe was refused: her kind stays as it is and its record takes
    the wardrobe's digest as attempted, so she is not asked again for this wardrobe and her
    queued sets render with the kind she has (review of #358: the hold had no way out when
    every reply was refused, as for a member named after her only palette colour). True when
    it released one."""
    if not wardrobe_pending(store, p):
        return False
    rec = json.loads(store.get_meta(STYLE_KEY.format(guid=p.guid)))
    worn = wardrobe.digest(p)
    rec.pop("wardrobe", None)
    if worn is not None:
        rec["wardrobe"] = worn
    store.set_meta(STYLE_KEY.format(guid=p.guid), json.dumps(rec, sort_keys=True))
    log("selfies_wardrobe_released", night=night, guid=p.guid, reason=reason)
    return True


def wardrobe_pending(store: Store, p: Persona | None) -> bool:
    """The pass chose p's photo kind as it is now, with another wardrobe or palette than she
    has now (the owner set or changed them in the Looks panel since): the pass chooses it once
    more, and her queued sets wait for that (render._held). Never a kind set by hand."""
    if p is None or p.appearance is None or chosen_style(store, p) is None:
        return False
    raw = store.get_meta(STYLE_KEY.format(guid=p.guid))
    try:
        recorded = json.loads(raw).get("wardrobe") if raw else None
    except (ValueError, TypeError, AttributeError):
        return False
    return recorded != wardrobe.digest(p)


def _pass_ran(store: Store, guid: int) -> bool:
    """The photo pass has had a unit for `guid` on some night."""
    with store.transaction():
        return store.conn.execute("SELECT 1 FROM night_step WHERE stage = ? AND unit = ? LIMIT 1",
                                  (STAGE, str(guid))).fetchone() is not None


def chosen_style(store: Store, p: Persona | None) -> str | None:
    """Which rules chose p's stored photo kind: PLAIN or LUST when the photo pass chose it as it
    is now, None when there is none or it came by hand. A kind with no record is from before
    the records: the pass's plain one when the pass handled the member, until the first night
    has recorded those (ADOPTED); after that, and without such a unit, it was set by hand. An
    unreadable record counts as by hand."""
    if p is None or p.photo is None:
        return None
    raw = store.get_meta(STYLE_KEY.format(guid=p.guid))
    if raw is None:
        if store.get_meta(ADOPTED) is None and _pass_ran(store, p.guid):
            return PLAIN
        return None
    try:
        rec = json.loads(raw)
        style, digest = rec["style"], rec["kind"]
    except (ValueError, TypeError, KeyError):
        return None
    return style if style in STYLES and digest == _digest(p.photo) else None


def needs_rechoice(store: Store, p: Persona | None) -> bool:
    """A member with looks whose photo kind the pass chose under other rules than her style
    calls for now (style.style_of): she moved into lust, or out of it. Never a kind set by hand,
    and once chosen again its record matches, so never twice for one move."""
    if p is None or p.appearance is None:
        return False
    chosen = chosen_style(store, p)
    if chosen == LUST and style_of(p) == LUST and lust_named(p):
        return True  # renamed since: her name filter would drop the joined phrase
    if chosen is not None and wardrobe_pending(store, p):
        return True  # her wardrobe or palette changed since the pass chose it
    return chosen is not None and chosen != style_of(p)


def _adopt(store: Store, *, now: float) -> int:
    """Once (ADOPTED): a record (PLAIN) for each stored kind from before the records that the
    pass chose, so a later hand change shows. One transaction; how many it recorded."""
    if store.get_meta(ADOPTED) is not None:
        return 0
    made = 0
    with store.transaction():
        for guid in sorted(store.persona_versions()):
            try:
                p = load_persona(store, guid)
            except ValueError:  # unreadable (PersonaError, JSON): never the pass's to record
                continue
            if (p is not None and p.photo is not None
                    and store.get_meta(STYLE_KEY.format(guid=guid)) is None
                    and _pass_ran(store, guid)):
                _record(store, guid, PLAIN, p.photo)
                made += 1
        store.set_meta(ADOPTED, repr(now))
    return made


def screen_kind(p: Persona, data: dict, *, ident: HumanIdentity,
                member_names: Iterable[str]) -> list[str]:
    """The problems with a free-text reply (KIND_SCHEMA's shape, the plain style's) for `p`, as
    `photo.<field>: <kind>: "<text>"` with his real name redacted from the text; it never fixes
    text. Per field, in order: meta, his real name, a likeness, a member's name (any case), an
    era, explicit wear (EXPLICIT_RE, and WEAR_RE beside it), anything that codes a person as
    young (AGE_RE); then
    whatever validate_persona says of `p` with that photo. The lust style writes no free text:
    screen_lust checks its picks."""
    names = [n for n in member_names if n]
    problems = []
    for field in _FIELDS:
        text = data[field]
        quoted = f'"{screen_text(text, ident)}"'
        if has_meta(text):
            problems.append(f"photo.{field}: reads like an assistant, not a person: {quoted}")
        if names_real_name(text, ident):
            problems.append(f"photo.{field}: real name: {quoted}")
        if LIKENESS_RE.search(text):
            problems.append(f"photo.{field}: likeness: {quoted}")
        if any(has_word(text, n) for n in names):
            problems.append(f"photo.{field}: names a member: {quoted}")
        if ERA_RE.search(text):
            problems.append(f"photo.{field}: era: {quoted}")
        if EXPLICIT_RE.search(text) or WEAR_RE.search(text):
            problems.append(f"photo.{field}: explicit: {quoted}")
        if AGE_RE.search(text):
            problems.append(f"photo.{field}: age: {quoted}")
    from .style_variety import plain_pairings
    return list(dict.fromkeys(problems + _photo_problems(p, data) + plain_pairings(p,data)))


def _photo_problems(p: Persona, data: dict) -> list[str]:
    try:
        # the photo's own problems only: another field's message can quote persona text, and an
        # unrelated old problem must not refuse a photo
        return [m for m in validate_persona(replace(p, photo=ProfilePhoto(**data)))
                if m.startswith(("photo.", "photo:"))]
    except TypeError as e:
        return [f"photo: {type(e).__name__}"]


def outfit_phrase(choice: str, colour: str, outfits: dict[str, str] = LUST_OUTFITS) -> str:
    """The outfit `choice` with its colour put in and an article in front."""
    return _article(outfits[choice].format(colour=colour))


def compose_lust(data: dict) -> ProfilePhoto:
    """The kind a lust reply (LUST_SCHEMA's shape) stands for: its picks as they are, and the
    outfit with its colour put in and an article in front."""
    return ProfilePhoto(kind=data["kind"], framing=data["framing"], setting=data["setting"],
                        outfit=outfit_phrase(data["outfit"], data["colour"]),
                        expression=data["expression"])


def screen_lust(p: Persona, data: dict, *, varied: bool = False) -> list[str]:
    """The problems with a lust reply, as `photo.<field>: <kind>` (every value is from the
    lists, so nothing to quote): a value off its list (the schema refuses that first), then the
    PAIRINGS: swimwear away from WATER_SETTINGS, gym wear away from GYM_SETTINGS, anything but
    swimwear in SWIM_ONLY_SETTINGS, swimwear framed other than upper body or in a headshot;
    then, on the kind it composes, her name in a part (NAME_CLASHES), the age guard and
    validate_persona.

    `varied`: one candidate's look in a varied set (varied.py), with a `view` and its own
    framings (VARIED_VIEWS, VARIED_FRAMINGS) and outfits (VARIED_OUTFITS). Only there may
    swimwear be framed full-length or three-quarter and photographed from behind; it stays at
    the water, gym wear at the gym, the hot tub swimwear only, and a pose (`view`) anywhere
    else is refused (VIEW_OUTSIDE). The age guard and the name screens also read the pose and
    framing phrases the prompt adds."""
    lists = (("setting", LUST_SETTINGS), ("outfit", VARIED_OUTFITS if varied else LUST_OUTFITS),
             ("colour", LUST_COLOURS), ("expression", LUST_EXPRESSIONS))
    if varied:
        lists += (("framing", VARIED_FRAMINGS), ("view", VARIED_VIEWS))
    elif data.get("kind") not in LUST_KINDS:  # never a mirror selfie; its framing is the
        return ["photo.kind: not on its list"]  # caller's (a scene may be closer)
    problems = [f"photo.{field}: not on its list" for field, values in lists
                if data.get(field) not in values]
    if not varied and data.get("view") is not None:
        problems.append(VIEW_OUTSIDE)
    if problems:
        return problems
    swim = data["outfit"] in SWIM_OUTFITS
    if swim and data["setting"] not in WATER_SETTINGS:
        problems.append(PAIRINGS[0])
    if (data["outfit"] in (VARIED_GYM_OUTFITS if varied else GYM_OUTFITS)
            and data["setting"] not in GYM_SETTINGS):
        problems.append(PAIRINGS[1])
    if data["setting"] in SWIM_ONLY_SETTINGS and not swim:
        problems.append(PAIRINGS[2])
    if swim and not varied and data.get("framing") != "upper_body":
        problems.append(PAIRINGS[3])
    if swim and varied and data["view"] in BACK_VIEWS and data["framing"] != "full_body":
        problems.append(SWIM_BACK)
    if swim and data.get("kind") == "headshot":
        problems.append(PAIRINGS[4])
    if problems:
        return problems
    if varied:
        return _screen_varied(p, data)
    photo = compose_lust(data)
    named = names_re(p)
    problems = [f"photo.{f}: shares her name" for f in ("setting", "outfit", "expression")
                if named is not None and named.search(getattr(photo, f))]
    if named is not None and not problems and named.search(lust_phrase(photo)):
        problems.append(NAME_CLASHES[1])  # the joined phrase itself, "wearing" and all
    problems += [f"photo.{f}: age" for f in _FIELDS if AGE_RE.search(getattr(photo, f))]
    return problems + _photo_problems(p, {f: getattr(photo, f) for f in _FIELDS})


def _screen_varied(p: Persona, data: dict) -> list[str]:
    """A varied look's composed phrases (prompt.varied_parts): her name in any, the age guard on
    each, then validate_persona on the outfit, setting and expression as a photo kind (its
    framing as the nearest kind's: full_body is the varied set's own)."""
    from .prompt import varied_parts
    parts = varied_parts(data, outfit_phrase(data["outfit"], data["colour"], VARIED_OUTFITS))
    named = names_re(p)
    problems = [f"photo.{f}: shares her name" for f, text in parts.items()
                if named is not None and named.search(text)]
    problems += [f"photo.{f}: age" for f, text in parts.items() if AGE_RE.search(text)]
    return problems + _photo_problems(p, {
        "kind": data["kind"], "framing": "three_quarter", "setting": data["setting"],
        "outfit": outfit_phrase(data["outfit"], data["colour"], VARIED_OUTFITS),
        "expression": data["expression"]})


def _refused(ctx: "NightContext", guid: int, problems: list[str]) -> tuple[None, list[str]]:
    shown = [screen_text(m, ctx.ident) for m in problems]
    # the log names the field and the kind of problem, never the reply's text
    log("selfies_kind_refused", night=ctx.night, guid=guid,
        problems=[m.split(': "', 1)[0] for m in shown] if getattr(ctx, "private_logs", True)
        else ["world_screen_refused"])
    return None, shown


async def kind_one(ctx: "NightContext", guid: int, *, variety=False,
                   share: float = BODY_SHOT_SHARE) -> tuple[ProfilePhoto | None, list[str]]:
    """One JSON call (no think pass) on the stored persona, under the rules of its style: the
    photo kind and no problems, or (None, problems). The plain style writes a free-text kind
    (KIND_SCHEMA, screen_kind); the lust style picks from lists (LUST_SCHEMA, screen_lust) and
    is asked once more when only a pairing or a name clash failed. A fatal ReflectionError is
    raised."""
    p = ctx.base(guid)
    name = ctx.name(guid)
    lust = suggestive(p)
    fields = dict(human=ctx.ident, name=name, card=ctx.base_card(guid), facts=_facts(p),
                  looks=looks_line(p.appearance))
    # each name a module constant at its own call, so night_prompt_names (the doctor) sees both
    system = (system_prompt(LUST_PROMPT, KIND_PROMPT_LUST, ctx.settings.data_dir, **fields)
              if lust else system_prompt(STAGE, KIND_PROMPT, ctx.settings.data_dir, **fields))
    ask = KIND_ASK.format(name=name)
    if not lust:
        ask += wardrobe.ask_hint(p)  # what they tend to wear; nothing with no wardrobe set
    lust_schema = wardrobe.lust_schema(p, LUST_SCHEMA)
    if variety:
        from . import photo_extra, style_variety
        all_personas = _personas(ctx)
        members = [ctx.name(g) for g in ctx.members(include_away=True)] + [
            member.identity.real_name for member in all_personas.values()]
        if lust:
            picks,_,_,_ = style_variety.assign(all_personas,[guid],seed=ctx.night,ident=ctx.ident,
                                               names=members,share=share)
            return (picks[guid].photo,[]) if guid in picks else _refused(ctx,guid,["photo: no_safe_capacity"])
        ask += photo_extra.common(ctx.store,ctx.clock.now(),persona=p,ident=ctx.ident,names=members)
        ask += (" Prefer a coherent crop: headshots near the face, mirror selfies showing the upper body, "
                "and clothing appropriate to the setting. These are preferences for this choice.")
    for again in (False, True):
        try:
            data = await ctx.reasoner.json(system, ask + (LUST_ASK_AGAIN if again else ""),
                                           lust_schema if lust else KIND_SCHEMA, temperature=0.7)
        except ReflectionError as e:
            if e.fatal:
                raise
            return _refused(ctx, guid, [f"model: {type(e).__name__}"])
        if not lust:
            members = [ctx.name(g) for g in ctx.members(include_away=True) if g != guid]
            problems = screen_kind(p, data, ident=ctx.ident, member_names=members)
            if problems:
                return _refused(ctx, guid, problems)
            return ProfilePhoto(**data), []
        if data.get("outfit") not in SWIM_OUTFITS:  # swimwear is framed waist-up anyway
            # three-quarter for `share` of the members, else waist-up ([selfies] body_shot_share)
            data = {**data, "framing": settle_framing(data.get("framing"), share,
                                                      f"{guid}:{ctx.night}")}
        # a lust selfie is waist-up, never three-quarter (safe.selfie_framing)
        data = {**data, "framing": selfie_framing(data.get("kind"), data.get("framing"))}
        problems = screen_lust(p, data)
        if not problems:
            return compose_lust(data), []
        if again or not set(problems) <= set(PAIRINGS) | set(NAME_CLASHES):
            return _refused(ctx, guid, problems)
        log("selfies_kind_asked_again", night=ctx.night, guid=guid, problems=problems)
    raise AssertionError("unreachable")


def _saver(ctx: "NightContext", guid: int, photo: ProfilePhoto, *, style: str | None = None,
           replacing: ProfilePhoto | None = None, variety=False,
           worn=_STORED) -> Callable[[Store], None]:
    """Save `photo` and its record (`style`, by default the stored persona's) while the stored
    kind is still `replacing`, the one the pass read (None: a first kind). A kind chosen again
    into the lust style also queues a fresh set for her (queue_restyled), so new candidates come
    without his "4 more"."""
    def write(store: Store) -> None:
        stored = load_persona(store, guid)
        if stored is None:
            raise LookupError(f"guid {guid} has no stored persona")
        if stored.photo != replacing:  # set by hand while the pass ran
            return
        race, class_name = _race_class(store, guid)
        save_persona(store, replace(stored, photo=photo), source="generated",
                     ts=ctx.clock.now(), race=race, class_name=class_name)
        chose = style_of(stored) if style is None else style
        # the kind as stored, with the wardrobe the pass read (`worn`), not the one stored now
        _record(store, guid, chose, load_persona(store, guid).photo, worn=worn)
        if variety:
            from . import photo_extra
            saved = load_persona(store,guid)
            light = photo_extra.choose_light(saved,ctx.night,ident=ctx.ident,
                names=[ctx.name(g) for g in ctx.members(include_away=True)] +
                      [p.identity.real_name for p in _personas(ctx).values()])
            photo_extra.record(store,saved,light,ctx.clock.now())
        pending = f"selfies:restyle:pending:{guid}"
        if (replacing is not None and chose == LUST) or store.get_meta(pending) is not None:
            queue_restyled(store, guid, now=ctx.clock.now())
            store.conn.execute("DELETE FROM meta WHERE key=?", (pending,))
    return write


def _stored(ctx: "NightContext", guid: int) -> Persona | None:
    try:
        return ctx.base(guid)
    except KeyError:  # none, or unreadable: the cache logs it
        return None


def _personas(ctx):
    result = {}
    for guid in ctx.members(include_away=True):
        member = _stored(ctx,guid)
        if member is not None:
            result[guid] = member
    return result


def make_stage(cfg):
    """Capture startup configuration; per-member work never opens mind.toml."""
    async def photo(ctx):
        await stage_photo(ctx, variety=cfg.variety,
                          share=getattr(cfg, "body_shot_share", BODY_SHOT_SHARE))
    return photo


def _due(ctx: "NightContext", guid: int) -> bool:
    return needs_kind(ctx.personas.get(guid)) or needs_rechoice(ctx.store, _stored(ctx, guid))


def _todo(ctx: "NightContext") -> list[int]:
    """The night's share: the first photo_per_night members by guid (away ones included) that
    need a kind or a new one, counting those whose unit finished tonight."""
    picks = [g for g in ctx.members(include_away=True)
             if _due(ctx, g) or ctx.done(STAGE, str(g))]
    return picks[:ctx.settings.photo_per_night]


async def stage_photo(ctx: "NightContext", *, variety=False,
                      share: float = BODY_SHOT_SHARE) -> None:
    """Stage "photo", units str(guid): each member with looks and no photo kind, or whose kind
    the pass chose under other rules than her style now (needs_rechoice), at most
    photo_per_night. A refused reply's unit is done with nothing written. Kinds from before the
    records are recorded first, once (_adopt)."""
    done = refused = rechosen = 0
    if adopted := _adopt(ctx.store, now=ctx.clock.now()):
        log("selfies_kind_adopted", night=ctx.night, kinds=adopted)
    if variety:
        from . import photo_extra
        photo_extra.seed(ctx.store,_personas(ctx),ctx.clock.now())

    async def one(unit: str) -> None:
        nonlocal done, refused, rechosen
        guid = int(unit)
        p = ctx.base(guid)
        photo, _ = await kind_one(ctx, guid, variety=variety, share=share)
        if photo is None:
            refused += 1
            ctx.skip(STAGE, unit)
            if p.photo is not None:  # one refusal ends a wardrobe's re-choice: her kind stays
                release_wardrobe(ctx.store, p, night=ctx.night, reason="refused")
        elif ctx.commit(STAGE, unit, _saver(ctx, guid, photo, style=style_of(p),
                                            replacing=p.photo, variety=variety,
                                            worn=wardrobe.digest(p))):
            ctx.personas.invalidate(guid)
            done += 1
            if p.photo is not None and chosen_style(ctx.store, ctx.base(guid)) == style_of(p):
                rechosen += 1  # landed: not a kind he set by hand while it ran
                log("selfies_kind_rechosen", night=ctx.night, guid=guid, style=style_of(p))
    await ctx.map_units(STAGE, [str(g) for g in _todo(ctx)], one)
    log("selfies_kind", night=ctx.night, done=done, refused=refused, rechosen=rechosen)


def _count(ctx: "NightContext") -> int:
    return min(ctx.settings.photo_per_night,
               sum(_due(ctx, g) for g in ctx.members(include_away=True)))


register_counter(STAGE, _count)
