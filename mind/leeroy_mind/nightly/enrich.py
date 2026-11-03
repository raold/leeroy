"""The looks pass (P1c Task 7): one offline pass that writes each member's appearance, a woman's
tastes ("drawn to" and "turned off by", which size the attraction curve's gains and hurts: Task
12) and three or four disclosure layers (surface, middle, deep, which Task 17's 2am confessions
draw on) into the fields P1b's persona schema keeps for them.

The owner reads a dry run first (run_enrichment with dry_run: the scratch file and nothing
else). His first real run sets meta enrich_approved (Task 25's `nightly enrich`); from then on
the night's "enrich" stage fills only the members without an appearance, at most
enrich_per_night of them: newcomers, and those refused on an earlier night.

A reply is screened, never fixed (screen_enrichment). A refused one is logged with his real name
redacted, stored nowhere, and asked again on a later night, never the same one. A save re-reads
the stored persona and fills only those of the pass's fields that are still empty there, so his
hand settings, an import made while the pass ran included, are never overwritten (controller
ruling C1). Men stay friends: a man's tastes stay empty, whatever the model wrote."""
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from leeroy_mind.human import HumanIdentity
from leeroy_mind.logs import log
from leeroy_mind.nightly.budget import register_counter
from leeroy_mind.nightly.ground import screen_text
from leeroy_mind.nightly.llm import ReflectionError, schema, system_prompt
from leeroy_mind.personas.card import card_race_class
from leeroy_mind.personas.io import load_persona, save_persona
from leeroy_mind.personas.schema import (Appearance, Disclosure, Persona, persona_json_schema,
                                         persona_to_dict, validate_persona)
from leeroy_mind.personas.screens import check_romance, has_word, names_real_name
from leeroy_mind.personas.stances import is_woman
from leeroy_mind.storage.store import Store
from leeroy_mind.text import has_meta
from leeroy_mind.wow import CLASSES, RACES

if TYPE_CHECKING:
    from leeroy_mind.nightly.runner import NightContext

STAGE = "enrich"
APPROVED = "enrich_approved"  # meta: "1" once the owner has run the pass for real (Task 25)

ENRICH_PROMPT = (
    "Describe the real person who plays {name}, the way they are at their keyboard in 2009.\n"
    "{card}\n{facts}\n"
    "Give how they look: realistic for their age, region and background, an ordinary person, "
    "never like anyone famous. {tastes} Then three or four things about their life they would "
    "only tell someone step by step: one surface layer (work, music, daily life), one or two "
    "middle layers (money, family, a partner who minds the hours), and one deep layer (an old "
    "loss, a bad year, why they only play at night). Keep everything non-explicit.")
# The words are the curve's act words (Task 12's ACT_WORDS, NEGLECT_WORDS and HURT_WORDS), so the
# lists size the gains and hurts.
TASTES_WOMAN = (
    "Then up to six things that draw them to someone and up to six that turn them off. Write "
    "each one as something {human} could do or be in game or in chat, using these words where "
    "they fit: flirting, compliments, being remembered, being listened to, replies, attention, "
    "questing together, helping, teasing back, being ignored, rudeness, being dismissed. Never "
    "name a guildmate.")
TASTES_MAN = "Leave drawn_to and turned_off_by empty."
ENRICH_ASK = "Describe the person who plays {name}."  # the user turn of both calls
LIKENESS_RE = re.compile(
    r"\b(looks? like|resembl\w*|look-?alike|doppelg[aä]nger|spitting image|celebrity|famous)\b",
    re.I)

# The reply: the persona schema's own bounds for the four fields, and three or four layers.
_FIELDS = persona_json_schema(enrichment=True)["properties"]
ENRICH_SCHEMA = schema("enrich", {
    "appearance": _FIELDS["appearance"]["anyOf"][0],
    "drawn_to": _FIELDS["romance"]["properties"]["drawn_to"],
    "turned_off_by": _FIELDS["romance"]["properties"]["turned_off_by"],
    "disclosures": {**_FIELDS["disclosures"], "minItems": 3},
})
# where each part of a reply lives in the persona, so a problem names the path he can edit
_PATHS = {"appearance": "appearance", "drawn_to": "romance.drawn_to",
          "turned_off_by": "romance.turned_off_by", "disclosures": "disclosures"}


@dataclass
class EnrichReport:
    """done: the guids enriched without a problem (saved, unless it was a dry run); refused: the
    others with their problems, his real name redacted; path: the dry run's file."""
    done: list[int]
    refused: dict[int, list[str]]
    path: Path | None


def _facts(p: Persona) -> str:
    """The persona's age, region, gender, job, household, rhythm, life thread, likes and
    dislikes, one per line; an empty one leaves its line out."""
    rows = (("Age", str(p.identity.age)), ("Region", p.identity.region),
            ("Gender", p.identity.player_gender), ("Job", p.real_life.job),
            ("Household", p.real_life.household), ("Rhythm", p.real_life.rhythm),
            ("Life thread", p.real_life.life_thread), ("Likes", "; ".join(p.likes)),
            ("Dislikes", "; ".join(p.dislikes)))
    return "\n".join(f"{label}: {' '.join(value.split())}" for label, value in rows
                     if value.strip())


def _with(p: Persona, appearance: Appearance | None, disclosures: tuple[Disclosure, ...],
          drawn_to: tuple[str, ...], turned_off_by: tuple[str, ...]) -> Persona:
    if not is_woman(p):  # men stay friends
        drawn_to = turned_off_by = ()
    return replace(p, appearance=appearance, disclosures=disclosures,
                   romance=replace(p.romance, drawn_to=drawn_to, turned_off_by=turned_off_by))


def merge(p: Persona, data: dict) -> Persona:
    """`p` with a reply's appearance, layers and tastes; a man's tastes are (), whatever the
    model wrote."""
    looks = data["appearance"]
    return _with(p, Appearance(**{**looks, "face": tuple(looks["face"])}),
                 tuple(Disclosure(d["depth"], d["text"]) for d in data["disclosures"]),
                 tuple(data["drawn_to"]), tuple(data["turned_off_by"]))


def _fill(stored: Persona, enriched: Persona) -> Persona:
    """The stored persona with the pass's fields only where its own are empty, so a hand
    setting, or an import made while the pass ran, wins (C1)."""
    return _with(stored,
                 enriched.appearance if stored.appearance is None else stored.appearance,
                 stored.disclosures or enriched.disclosures,
                 stored.romance.drawn_to or enriched.romance.drawn_to,
                 stored.romance.turned_off_by or enriched.romance.turned_off_by)


def _strings(data: dict):
    """(persona path, text) for every string in a reply."""
    def walk(v, path: str):
        if isinstance(v, str):
            yield path, v
        elif isinstance(v, dict):
            for k, x in v.items():
                yield from walk(x, f"{path}.{k}")
        elif isinstance(v, list):
            for i, x in enumerate(v):
                yield from walk(x, f"{path}[{i}]")
    for key, path in _PATHS.items():
        yield from walk(data[key], path)


def screen_enrichment(p: Persona, data: dict, *, ident: HumanIdentity,
                      member_names: Iterable[str]) -> list[str]:
    """The problems with a reply (ENRICH_SCHEMA's shape) for `p`, as "path: problem" with the
    persona's paths; it never fixes text. One problem each for: meta in any string; his real
    name, by P1b's matcher (ruling I4; the reply has no name field); a likeness (LIKENESS_RE)
    anywhere; a member named in the tastes (in any case), which describe what draws her to
    someone, never a guildmate, or in a layer (as written), which is the player's life away from
    the guild and never needs one (review I1); romance aimed at a member (P1b's check_romance
    over the tastes and the layers, the members' names in any case, as P1b's import screen reads
    them); and anything validate_persona says about merge(p, data). A quoted text has his real
    name redacted."""
    names = [n for n in member_names if n]
    problems = []
    for path, text in _strings(data):
        quoted = f'"{screen_text(text, ident)}"'
        if has_meta(text):  # validate_persona's own words, so the two are one problem
            problems.append(f"{path}: reads like an assistant, not a person")
        if names_real_name(text, ident):
            problems.append(f"{path}: real name: {quoted}")
        if LIKENESS_RE.search(text):
            problems.append(f"{path}: likeness: {quoted}")
        if path.startswith(("romance.", "disclosures")):
            # a layer reads names as written: a member's name may be an ordinary word
            as_written = path.startswith("disclosures")
            problems += [f"{path}: names a member ({n}): {quoted}" for n in names
                         if has_word(text, n, case_sensitive=as_written)]
    try:
        check_romance({"romance": {k: data[k] for k in ("drawn_to", "turned_off_by")},
                       "disclosures": data["disclosures"]}, ident, (), characters=names)
    except ValueError as e:
        problems.append(str(e))
    problems += validate_persona(merge(p, data))
    return list(dict.fromkeys(problems))


def _refused(ctx: "NightContext", guid: int, problems: list[str]) -> tuple[None, list[str]]:
    shown = [screen_text(m, ctx.ident) for m in problems]
    log("nightly_enrich_refused", night=ctx.night, guid=guid, problems=shown)
    return None, shown


async def enrich_one(ctx: "NightContext", guid: int) -> tuple[Persona | None, list[str]]:
    """A persona's looks, tastes and layers from the reflection model: one think pass, then
    the JSON answer at temperature 0.8 with the pass's notes, then merge and the screens. The
    merged persona and no problems, or (None, problems) when anything is wrong, a reply the
    model got wrong twice included; it is not asked again the same night. A fatal
    ReflectionError (the model is not there) is raised."""
    p = ctx.persona(guid)
    name = ctx.name(guid)
    system = system_prompt(
        STAGE, ENRICH_PROMPT, ctx.settings.data_dir, human=ctx.ident, name=name,
        card=ctx.card(guid), facts=_facts(p),
        tastes=TASTES_WOMAN.format(human=ctx.ident.called) if is_woman(p) else TASTES_MAN)
    ask = ENRICH_ASK.format(name=name)
    try:
        notes = await ctx.reasoner.think(system, ask)
        data = await ctx.reasoner.json(system, ask, ENRICH_SCHEMA, notes=notes, temperature=0.8)
    except ReflectionError as e:
        if e.fatal:
            raise
        return _refused(ctx, guid, [f"model: {e}"])
    members = [ctx.name(g) for g in ctx.members(include_away=True) if g != guid]
    problems = screen_enrichment(p, data, ident=ctx.ident, member_names=members)
    if problems:
        return _refused(ctx, guid, problems)
    return merge(p, data), []


def _bare(ctx: "NightContext", guid: int) -> bool:
    """A stored persona with no appearance yet."""
    p = ctx.personas.get(guid)
    return p is not None and p.appearance is None


def _race_class(store: Store, guid: int) -> tuple[str, str]:
    """From the stored roster; for a guid it lacks, the stored card's (as P1b's import does)."""
    m = store.cast().get(guid)
    if m is not None:
        return RACES.get(m.race, ""), CLASSES.get(m.class_, "")
    return card_race_class(store.get_card(guid) or "")


def _saver(ctx: "NightContext", guid: int, enriched: Persona) -> Callable[[Store], None]:
    def write(store: Store) -> None:
        stored = load_persona(store, guid)
        if stored is None:
            raise LookupError(f"guid {guid} has no stored persona")
        race, class_name = _race_class(store, guid)
        save_persona(store, _fill(stored, enriched), source="generated", ts=ctx.clock.now(),
                     race=race, class_name=class_name)
    return write


def _entry(ctx: "NightContext", guid: int, enriched: Persona | None, problems: list[str]) -> dict:
    d = persona_to_dict(enriched) if enriched is not None else {
        "appearance": None, "disclosures": [], "romance": {"drawn_to": [], "turned_off_by": []}}
    return {"guid": guid, "name": ctx.name(guid), "appearance": d["appearance"],
            "drawn_to": d["romance"]["drawn_to"], "turned_off_by": d["romance"]["turned_off_by"],
            "disclosures": d["disclosures"], "problems": problems}


def _write_scratch(ctx: "NightContext", entries: list[dict]) -> Path:
    path = Path(ctx.settings.data_dir) / "scratch" / f"enrich-{ctx.night}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entries, indent=2, ensure_ascii=False) + "\n", encoding="utf-8",
                    newline="\n")
    return path


async def run_enrichment(ctx: "NightContext", guids: Iterable[int], *,
                         dry_run: bool) -> EnrichReport:
    """enrich_one for each guid whose stored persona has no appearance, at most
    settings.parallel at once (map_units), each a unit of stage "enrich".

    A dry run writes scratch/enrich-{night}.json in the data dir, the list of {guid, name,
    appearance, drawn_to, turned_off_by, disclosures, problems} the owner reads, and nothing
    else. Otherwise each persona is one transaction together with its unit's row: the stored
    persona is read again, the pass's fields go in where it has none (_fill, ruling C1), and it
    is saved as a new version (source "generated"); the cache drops it once that commits. A
    refused persona's unit is done with nothing written, so the same night never asks again."""
    todo = [g for g in dict.fromkeys(guids) if _bare(ctx, g)]
    done, refused, entries = [], {}, {}

    async def one(unit: str) -> None:
        guid = int(unit)
        enriched, problems = await enrich_one(ctx, guid)
        if dry_run:
            entries[guid] = _entry(ctx, guid, enriched, problems)
        if enriched is None:
            refused[guid] = problems
            if not dry_run:
                ctx.skip(STAGE, unit)
        elif dry_run:
            done.append(guid)
        elif ctx.commit(STAGE, unit, _saver(ctx, guid, enriched)):
            ctx.personas.invalidate(guid)
            done.append(guid)
    await ctx.map_units(STAGE, [str(g) for g in todo], one)
    path = _write_scratch(ctx, [entries[g] for g in sorted(entries)]) if dry_run else None
    log("nightly_enrich", night=ctx.night, dry_run=dry_run, done=len(done), refused=len(refused))
    return EnrichReport(sorted(done), dict(sorted(refused.items())), path)


def approved(store: Store) -> bool:
    """The owner has run the pass for real once (Task 25 sets the meta)."""
    return store.get_meta(APPROVED) == "1"


def _tonight(ctx: "NightContext") -> list[int]:
    """The night's share: the first enrich_per_night members by guid (away ones included) with
    no appearance, counting those whose unit finished tonight, so a resumed night takes on no
    more than a night's share."""
    picks = [g for g in ctx.members(include_away=True) if _bare(ctx, g) or ctx.done(STAGE, str(g))]
    return picks[:ctx.settings.enrich_per_night]


async def stage_enrich(ctx: "NightContext") -> None:
    """Stage "enrich", units str(guid): once the pass is approved, the night's share of the
    members without an appearance (personas who joined since the pass, or who failed it)."""
    if approved(ctx.store):
        await run_enrichment(ctx, _tonight(ctx), dry_run=False)


def _count(ctx: "NightContext") -> int:
    """The budget's count: the members without an appearance, at most enrich_per_night, once
    the pass is approved (0 before)."""
    if not approved(ctx.store):
        return 0
    return min(ctx.settings.enrich_per_night,
               sum(_bare(ctx, g) for g in ctx.members(include_away=True)))


register_counter(STAGE, _count)
