"""The night's photo-kind pass (P2 Task 3): the reflection model chooses the profile picture the
real person who plays a member would put on their own profile, as the persona's `photo` (kind,
framing, setting, outfit, expression), once the member has looks.

A reply is screened, never fixed. A refused one is logged by field and kind only (never the
reply's text), stored nowhere, and not asked again the same night. A save re-reads the stored
persona and fills `photo` only while it is still what the pass read (empty, for a first kind),
so a hand setting made while the pass ran wins.

The member's style (style.py) picks the rules: a woman of the lust style gets KIND_PROMPT_LUST
(the owner's overlay prompts/nightly_photo_lust.txt replaces it) and the lust screen: her outfit
must name a garment of GARMENT_RE, a reply using a word of LUST_EXPLICIT_RE (or a bed in the
setting or outfit) is refused, and swimwear passes only by the water (see the screen's
comment); everyone else gets KIND_PROMPT and EXPLICIT_RE as they were.

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

from .style import PLAIN, STYLES, style_of, suggestive

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
    "She knows she looks good and likes to show it a little: choose a flattering, flirtatious, "
    "confident photo that still fits who she is, her work, age, region, habits and personality. "
    "Choose the kind of photo (a phone selfie, a mirror selfie, or a snapshot a friend took), "
    "the framing (head and shoulders, or upper body), where it was taken (a place in her own "
    "life, in a few words), what she is wearing in it (name each piece of clothing), and her "
    "expression (playful, sultry or "
    "confident). Something fitted, low-cut, off the shoulder or short suits it; swimwear only "
    "at a beach, a pool, a lake or a hot tub. Name no year, no decade, no device and no brand. "
    "Always clothed: never nude, topless, see-through, sheer, mesh or wet, never lingerie, "
    "underwear, a bralette, a bustier, a robe or a towel as the outfit, never in a shower, a "
    "bath or a bed, nothing sexual in the pose, never like anyone famous, and no one else's "
    "name.")
KIND_ASK = "Choose {name}'s profile picture."

_FIELDS = ("kind", "framing", "setting", "outfit", "expression")
_PROPS = persona_json_schema(enrichment=True)["properties"]["photo"]["anyOf"][0]["properties"]
KIND_SCHEMA = schema("photo", {k: _PROPS[k] for k in _FIELDS})

ERA_RE = re.compile(
    r"\b(1[89]\d\d|20\d\d|'?\d0s|retro|vintage|old[- ]fashioned|antique|flip[- ]?phone|webcam|"
    r"crt|camcorder|polaroid|disposable camera|digital camera|myspace)\b", re.I)
EXPLICIT_RE = re.compile(
    r"\b(nude|naked|topless|shirtless|braless|lingerie|underwear|bra|panties|bikini|swimsuit|"
    r"swimwear|see-through|sheer|sexy|sexual|seductive|nsfw|cleavage)\b", re.I)
# The lust style's screen. A reply is refused when any field uses a word of _LUST_REFUSED (a
# bed also in the setting or the outfit: LUST_BED_RE), when the outfit names no garment of
# GARMENT_RE, or when swimwear appears without a beach, pool, lake or hot tub in a setting that
# names no room or other indoor place and does not point away from the water. Cleavage,
# low-cut, fitted, sexy, flirty and sultry are not refused. It is a word screen: what it does
# not list passes it, and the NSFW gate still checks every image.
_LUST_REFUSED = (
    # nudity, or the hint of it
    r"nude|nudes|naked|nudity|topless|shirtless|braless|undress\w*|unclothed|nipples?|pasties|"
    r"body[- ]?paint(?:ed)?|unbutton\w*|unzipped|wet|soaked|dripping|oiled|"
    r"nothing (?:but|else|underneath|on|at all)|wearing nothing|not wearing anything|"
    r"birthday suit|au naturel|(?:no|without) (?:a )?(?:top|bra|pants|shirt|clothes|clothing)|"
    r"(?:just|only) (?:a|an|her|his|my) (?:[\w'-]+ ){0,2}(?:shirt|hoodie|sweater|jersey)s?|"
    r"barely (?:dressed|there)|scantily|skimpy|half[- ]dressed|partially dressed|"
    r"covering (?:her|his|my) (?:chest|body)|bare[- ]?(?:chested|breast\w*)|breasts?|"
    r"\w*boob\w*|areolas?|"
    # underwear, sleepwear and see-through wear
    r"lingerie|underwear|undies|bra|bras|bralettes?|bralets?|brassieres?|bustiers?|panties|"
    r"knickers|thong|g-string|boyshorts|negligee|nightie|nightgown|nightdress|nightwear|"
    r"sleepwear|pajamas?|pyjamas?|pjs|chemise|camisole|bodysuit|babydolls?|baby-doll|teddy|"
    r"teddies|corset|garters?|body ?stockings?|stockings|suspenders|bandeau|catsuit|latex|"
    r"harness|fishnets?|mesh|towels?|robes?|bathrobes?|monokini|micro[- ]?bikini|"
    r"see[- ]?thr(?:ough|u)|sheer|transparent|"
    # where people undress
    r"shower\w*|baths?|bathtubs?|bubble bath|(?<!hot )(?<!hot-)tubs?|in bed|sheets?|covers|"
    r"blankets?|duvets?|sauna|steam room|locker room|changing room|fitting room|"
    # anything sexual
    r"nsfw|sex|sexual|sexually|sexuali[sz]ed|seductive|erotic|erotica|explicit|porn\w*|fetish|"
    r"boudoir|horny|aroused|kinky|lewd|xxx|onlyfans|on all fours|bent over|straddl\w*|"
    r"legs spread|spread legs")
LUST_EXPLICIT_RE = re.compile(rf"\b(?:{_LUST_REFUSED})\b", re.I)
LUST_BED_RE = re.compile(r"\bbeds?\b", re.I)  # in the setting and the outfit
_BED_FIELDS = ("setting", "outfit")
GARMENT_RE = re.compile(  # a lust outfit names at least one of these
    r"\b(?:dress(?:es)?|sundress(?:es)?|minidress(?:es)?|gowns?|tops?|blouses?|shirts?|"
    r"t-shirts?|tees?|sweaters?|jumpers?|cardigans?|jackets?|blazers?|coats?|hoodies?|"
    r"sweatshirts?|jerseys?|jeans|shorts|skirts?|miniskirts?|leggings|yoga pants|trousers|"
    r"jumpsuits?|rompers?|playsuits?|bikinis?|swimsuits?|one-pieces?|sarongs?|cover-ups?|"
    r"kaftans?|overalls|dungarees|scrubs|uniforms?|turtlenecks?)\b", re.I)
SWIMWEAR_RE = re.compile(
    r"\b(bikinis?|swimsuits?|swimwear|swimming costume|bathing suit|tankini)\b", re.I)
WATER_RE = re.compile(  # where swimwear belongs, in the reply's setting
    r"\b(?:beach(?:es|side)?|lake(?:side|shore)?|hot[- ]tub|"
    r"(?<!car )(?<!car-)pool(?:s|side)?(?![- ](?:hall|table|cue|league|room)))\b", re.I)
INDOOR_RE = re.compile(  # never swimwear here, whatever water the setting also names
    r"\b(?:bedrooms?|beds?|bathrooms?|kitchens?|offices?|hallways?|rooms?|mirrors?)\b", re.I)
AWAY_RE = re.compile(  # a setting that points away from the water
    r"\b(?:anywhere but|not (?:at |on |by |in )?the|nowhere near|far from|away from)\b", re.I)


def looks_line(a: Appearance) -> str:
    """One sentence from the appearance's fields, in schema order."""
    face = f", {', '.join(a.face)}" if a.face else ""
    return (f"{a.height_cm} cm, {a.build} build, {a.weight_kg} kg, {a.hair_colour} "
            f"{a.hair_length} hair worn {a.hair_style}, {a.skin_tone} skin, {a.eye_colour} "
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


def _record(store: Store, guid: int, style: str, photo: ProfilePhoto) -> None:
    """Which rules chose `photo` for `guid`: the style and the kind's digest, never its text."""
    store.set_meta(STYLE_KEY.format(guid=guid),
                   json.dumps({"style": style, "kind": _digest(photo)}, sort_keys=True))


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
    """The problems with a reply (KIND_SCHEMA's shape) for `p`, as `photo.<field>: <kind>:
    "<text>"` with his real name redacted from the text; it never fixes text. Per field, in
    order: meta, his real name, a likeness, a member's name (any case), an era, explicit wear
    (EXPLICIT_RE; for the lust style LUST_EXPLICIT_RE, a bed in the setting or outfit,
    swimwear away from the water, and an outfit that names no garment); then whatever
    validate_persona says of `p` with that photo."""
    names = [n for n in member_names if n]
    lust = suggestive(p)
    explicit = LUST_EXPLICIT_RE if lust else EXPLICIT_RE
    setting = str(data.get("setting", ""))
    by_water = (lust and WATER_RE.search(setting) is not None
                and not INDOOR_RE.search(setting) and not AWAY_RE.search(setting))
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
        if explicit.search(text) or (lust and field in _BED_FIELDS and LUST_BED_RE.search(text)):
            problems.append(f"photo.{field}: explicit: {quoted}")
        if lust and not by_water and SWIMWEAR_RE.search(text):
            problems.append(f"photo.{field}: swimwear away from the water: {quoted}")
        if lust and field == "outfit" and not GARMENT_RE.search(text):
            problems.append(f"photo.outfit: names no garment: {quoted}")
    try:
        # the photo's own problems only: another field's message can quote persona text, and an
        # unrelated old problem must not refuse a photo
        problems += [m for m in validate_persona(replace(p, photo=ProfilePhoto(**data)))
                     if m.startswith(("photo.", "photo:"))]
    except TypeError as e:
        problems.append(f"photo: {type(e).__name__}")
    return list(dict.fromkeys(problems))


def _refused(ctx: "NightContext", guid: int, problems: list[str]) -> tuple[None, list[str]]:
    shown = [screen_text(m, ctx.ident) for m in problems]
    # the log names the field and the kind of problem, never the reply's text
    log("selfies_kind_refused", night=ctx.night, guid=guid,
        problems=[m.split(': "', 1)[0] for m in shown])
    return None, shown


async def kind_one(ctx: "NightContext", guid: int) -> tuple[ProfilePhoto | None, list[str]]:
    """One JSON call (no think pass) on the stored persona, under the rules of its style: the
    photo kind and no problems, or (None, problems). A fatal ReflectionError is raised."""
    p = ctx.base(guid)
    name = ctx.name(guid)
    prompt, template = (LUST_PROMPT, KIND_PROMPT_LUST) if suggestive(p) else (STAGE, KIND_PROMPT)
    system = system_prompt(
        prompt, template, ctx.settings.data_dir, human=ctx.ident, name=name,
        card=ctx.base_card(guid), facts=_facts(p), looks=looks_line(p.appearance))
    try:
        data = await ctx.reasoner.json(system, KIND_ASK.format(name=name), KIND_SCHEMA,
                                       temperature=0.7)
    except ReflectionError as e:
        if e.fatal:
            raise
        return _refused(ctx, guid, [f"model: {type(e).__name__}"])
    members = [ctx.name(g) for g in ctx.members(include_away=True) if g != guid]
    problems = screen_kind(p, data, ident=ctx.ident, member_names=members)
    if problems:
        return _refused(ctx, guid, problems)
    return ProfilePhoto(**data), []


def _saver(ctx: "NightContext", guid: int, photo: ProfilePhoto, *, style: str | None = None,
           replacing: ProfilePhoto | None = None) -> Callable[[Store], None]:
    """Save `photo` and its record (`style`, by default the stored persona's) while the stored
    kind is still `replacing`, the one the pass read (None: a first kind)."""
    def write(store: Store) -> None:
        stored = load_persona(store, guid)
        if stored is None:
            raise LookupError(f"guid {guid} has no stored persona")
        if stored.photo != replacing:  # set by hand while the pass ran
            return
        race, class_name = _race_class(store, guid)
        save_persona(store, replace(stored, photo=photo), source="generated",
                     ts=ctx.clock.now(), race=race, class_name=class_name)
        _record(store, guid, style_of(stored) if style is None else style,
                load_persona(store, guid).photo)  # the kind as stored
    return write


def _stored(ctx: "NightContext", guid: int) -> Persona | None:
    try:
        return ctx.base(guid)
    except KeyError:  # none, or unreadable: the cache logs it
        return None


def _due(ctx: "NightContext", guid: int) -> bool:
    return needs_kind(ctx.personas.get(guid)) or needs_rechoice(ctx.store, _stored(ctx, guid))


def _todo(ctx: "NightContext") -> list[int]:
    """The night's share: the first photo_per_night members by guid (away ones included) that
    need a kind or a new one, counting those whose unit finished tonight."""
    picks = [g for g in ctx.members(include_away=True)
             if _due(ctx, g) or ctx.done(STAGE, str(g))]
    return picks[:ctx.settings.photo_per_night]


async def stage_photo(ctx: "NightContext") -> None:
    """Stage "photo", units str(guid): each member with looks and no photo kind, or whose kind
    the pass chose under other rules than her style now (needs_rechoice), at most
    photo_per_night. A refused reply's unit is done with nothing written. Kinds from before the
    records are recorded first, once (_adopt)."""
    done = refused = rechosen = 0
    if adopted := _adopt(ctx.store, now=ctx.clock.now()):
        log("selfies_kind_adopted", night=ctx.night, kinds=adopted)

    async def one(unit: str) -> None:
        nonlocal done, refused, rechosen
        guid = int(unit)
        p = ctx.base(guid)
        photo, _ = await kind_one(ctx, guid)
        if photo is None:
            refused += 1
            ctx.skip(STAGE, unit)
        elif ctx.commit(STAGE, unit, _saver(ctx, guid, photo, style=style_of(p),
                                            replacing=p.photo)):
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
