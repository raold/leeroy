"""The night's photo-kind pass (P2 Task 3): the reflection model chooses the profile picture the
real person who plays a member would put on their own profile, as the persona's `photo` (kind,
framing, setting, outfit, expression), once the member has looks.

A reply is screened, never fixed. A refused one is logged by field and kind only (never the
reply's text), stored nowhere, and not asked again the same night. A save re-reads the stored
persona and fills `photo` only while it is still empty there, so a hand setting made while the
pass ran wins."""
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

if TYPE_CHECKING:
    from leeroy_mind.nightly.runner import NightContext

STAGE = "photo"

KIND_PROMPT = (
    "Choose the profile picture the real person who plays {name} would put on their own social "
    "or work profile.\n{card}\n{facts}\nHow they look: {looks}\n"
    "Choose what fits who they are, their work, age, region, habits and personality: the kind "
    "of photo (a phone selfie, a mirror selfie, a professional headshot, or a snapshot a friend "
    "took), the framing (face, head and shoulders, or upper body), where it was taken (a place "
    "in their own life, in a few words), what they are wearing in it, and their expression. "
    "Name no year, no decade, no device and no brand. Fully clothed, nothing revealing or "
    "sexual, never like anyone famous, and no one else's name.")
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


def looks_line(a: Appearance) -> str:
    """One sentence from the appearance's fields, in schema order."""
    face = f", {', '.join(a.face)}" if a.face else ""
    return (f"{a.height_cm} cm, {a.build} build, {a.weight_kg} kg, {a.hair_colour} "
            f"{a.hair_length} hair worn {a.hair_style}, {a.skin_tone} skin, {a.eye_colour} "
            f"eyes{face}; at the computer they wear {a.at_the_pc}.")


def needs_kind(p: Persona | None) -> bool:
    """A stored persona with looks and no photo kind yet."""
    return p is not None and p.appearance is not None and p.photo is None


def screen_kind(p: Persona, data: dict, *, ident: HumanIdentity,
                member_names: Iterable[str]) -> list[str]:
    """The problems with a reply (KIND_SCHEMA's shape) for `p`, as `photo.<field>: <kind>:
    "<text>"` with his real name redacted from the text; it never fixes text. Per field, in
    order: meta, his real name, a likeness, a member's name (any case), an era, explicit wear;
    then whatever validate_persona says of `p` with that photo."""
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
        if EXPLICIT_RE.search(text):
            problems.append(f"photo.{field}: explicit: {quoted}")
    try:
        problems += validate_persona(replace(p, photo=ProfilePhoto(**data)))
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
    """One JSON call (no think pass) on the stored persona: the photo kind and no problems, or
    (None, problems). A fatal ReflectionError is raised."""
    p = ctx.base(guid)
    name = ctx.name(guid)
    system = system_prompt(
        STAGE, KIND_PROMPT, ctx.settings.data_dir, human=ctx.ident, name=name,
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


def _saver(ctx: "NightContext", guid: int, photo: ProfilePhoto) -> Callable[[Store], None]:
    def write(store: Store) -> None:
        stored = load_persona(store, guid)
        if stored is None:
            raise LookupError(f"guid {guid} has no stored persona")
        if stored.photo is not None:  # set by hand while the pass ran
            return
        race, class_name = _race_class(store, guid)
        save_persona(store, replace(stored, photo=photo), source="generated",
                     ts=ctx.clock.now(), race=race, class_name=class_name)
    return write


def _todo(ctx: "NightContext") -> list[int]:
    """The night's share: the first photo_per_night members by guid (away ones included) that
    need a kind, counting those whose unit finished tonight."""
    picks = [g for g in ctx.members(include_away=True)
             if needs_kind(ctx.personas.get(g)) or ctx.done(STAGE, str(g))]
    return picks[:ctx.settings.photo_per_night]


async def stage_photo(ctx: "NightContext") -> None:
    """Stage "photo", units str(guid): each member with looks and no photo kind, at most
    photo_per_night. A refused reply's unit is done with nothing written."""
    done = refused = 0

    async def one(unit: str) -> None:
        nonlocal done, refused
        guid = int(unit)
        photo, _ = await kind_one(ctx, guid)
        if photo is None:
            refused += 1
            ctx.skip(STAGE, unit)
        elif ctx.commit(STAGE, unit, _saver(ctx, guid, photo)):
            ctx.personas.invalidate(guid)
            done += 1
    await ctx.map_units(STAGE, [str(g) for g in _todo(ctx)], one)
    log("selfies_kind", night=ctx.night, done=done, refused=refused)


def _count(ctx: "NightContext") -> int:
    return min(ctx.settings.photo_per_night,
               sum(needs_kind(ctx.personas.get(g)) for g in ctx.members(include_away=True)))


register_counter(STAGE, _count)
