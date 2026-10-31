"""Seeding: read the owner's private sketches at run time, map each onto a real guild character
and convert it to a persona with the reflection model. Nothing from the sketches lives in this
repository; the file is only ever read from the path the owner passes in."""
import dataclasses
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ..human import HumanIdentity, human_identity
from ..llm.client import LLMClient
from ..llm.presets import STRUCTURE, JSONJobFailed, chat_json
from ..protocol import Member
from ..storage.rows import Relationship
from ..storage.store import Store
from ..text import estimate_tokens
from ..wow import CAST_MIN_LEVEL, CLASSES, RACES
from .io import load_persona, save_persona
from .screens import (check_no_real_name, check_romance, has_word, mentions_real_name,
                      redact_real_name, refers_to_human)
from .schema import (JEALOUSY_MODES, TYPING_RULE, Persona, persona_from_dict,
                     persona_json_schema, validate_persona)
from .typing_fix import cased_like, repair_typing

CONTEXT_TOKEN_CAP = 2500
MAX_SAMPLE_LINES = 12
_CLASS_ALIASES = {"DK": "Death Knight"}
_GUILD_ROLE = {"Officers": "officer", "Raiders": "raider", "Socials": "social"}
_ACCENT = {"UK": "en-gb", "US": "en-us"}
_RANGED_CLASSES = frozenset({"Mage", "Warlock", "Hunter", "Priest"})
# class -> {main spec: seed raid role}; seed tanks are Prot only, so every DK spec is melee
SEED_SPECS = {
    "Warrior": {"Arms": "melee", "Fury": "melee", "Protection": "tank"},
    "Paladin": {"Holy": "healer", "Protection": "tank", "Retribution": "melee"},
    "Death Knight": {"Blood": "melee", "Frost": "melee", "Unholy": "melee"},
    "Druid": {"Balance": "ranged", "Feral": "melee", "Restoration": "healer"},
    "Priest": {"Discipline": "healer", "Holy": "healer", "Shadow": "ranged"},
    "Shaman": {"Elemental": "ranged", "Enhancement": "melee", "Restoration": "healer"},
    "Mage": {"Arcane": "ranged", "Fire": "ranged", "Frost": "ranged"},
    "Warlock": {"Affliction": "ranged", "Demonology": "ranged", "Destruction": "ranged"},
    "Rogue": {"Assassination": "melee", "Combat": "melee", "Subtlety": "melee"},
    "Hunter": {"Beast Mastery": "ranged", "Marksmanship": "ranged", "Survival": "ranged"},
}
# a sketch's spec words, matched whole ("unholy" is not "holy"), to the main spec they name
_SPEC_WORDS = {
    "prot": "Protection", "protection": "Protection", "holy": "Holy", "disc": "Discipline",
    "discipline": "Discipline", "resto": "Restoration", "restoration": "Restoration",
    "unholy": "Unholy", "frost": "Frost", "blood": "Blood", "arms": "Arms", "fury": "Fury",
    "ret": "Retribution", "retribution": "Retribution", "balance": "Balance",
    "boomkin": "Balance", "moonkin": "Balance", "feral": "Feral", "elemental": "Elemental",
    "ele": "Elemental", "enhancement": "Enhancement", "shadow": "Shadow",
    "affliction": "Affliction", "demonology": "Demonology", "destruction": "Destruction",
    "fire": "Fire", "arcane": "Arcane", "beast": "Beast Mastery", "bm": "Beast Mastery",
    "marksmanship": "Marksmanship", "mm": "Marksmanship", "survival": "Survival",
    "assassination": "Assassination", "combat": "Combat", "subtlety": "Subtlety",
}
_STYLES = ("bold", "shy", "earnest", "theatrical", "guarded", "competitive")
_JEALOUSY_TAGS = {k: m for m in JEALOUSY_MODES  # "sulker", "good sport", "play-by-play"
                  for k in (m, m.replace("_", " "), m.replace("_", "-"))}

_HEADING = re.compile(
    r"^ {0,4}(?P<n>\d+)\. \*\*(?P<name>[^*]+)\*\* \((?P<real>[^,)]+), (?P<age>\d+)\)"
    r" · (?P<desc>[^·]+?) · (?P<g>[FM]) · (?P<region>UK|US)(?: · (?P<tags>.+?))?\.?\s*$")
_SECTION = re.compile(r"^## (\d+)\.")
_GROUP = re.compile(r"^### (\w+)\s*$")
_RACE_NAMES = sorted(RACES.values(), key=len, reverse=True)
_CLASS_NAMES = sorted([*CLASSES.values(), *_CLASS_ALIASES], key=len, reverse=True)


@dataclass(frozen=True)
class Sketch:
    index: int
    name: str
    real_name: str
    age: int
    race: str
    spec: str
    class_name: str
    gender: str
    region: str
    tags: tuple[str, ...]
    group: str
    body: str


@dataclass(frozen=True)
class Assignment:
    mapping: dict[int, int]
    fallbacks: dict[int, str]
    unmatched: list[int]


@dataclass
class SeedReport:
    assigned: int = 0
    created: list[int] = field(default_factory=list)
    skipped: list[int] = field(default_factory=list)
    unmatched: list[int] = field(default_factory=list)
    failed: dict[int, str] = field(default_factory=dict)
    table: list[str] = field(default_factory=list)
    unparsed: list[str] = field(default_factory=list)
    not_cast: list[str] = field(default_factory=list)
    unknown_relations: list[str] = field(default_factory=list)
    cut_lines: dict[int, int] = field(default_factory=dict)


def _section_lines(md: str, number: int) -> list[str]:
    out, on = [], False
    for line in md.splitlines():
        m = _SECTION.match(line)
        if m:
            on = int(m.group(1)) == number
            continue
        if on:
            out.append(line)
    return out


def _split_desc(desc: str) -> tuple[str, str, str] | None:
    desc = re.sub(r"\s*\(.*?\)", "", desc).strip()
    race = next((r for r in _RACE_NAMES if desc.startswith(r + " ")), None)
    if race is None:
        return None
    rest = desc[len(race):].strip()
    cls = next((c for c in _CLASS_NAMES if rest == c or rest.endswith(" " + c)), None)
    if cls is None:
        return None
    spec = rest[:len(rest) - len(cls)].strip()
    return race, spec, _CLASS_ALIASES.get(cls, cls)


# a numbered bold line shaped like a heading ("1. **Name** (..."); "1. **Bit:** ..." is body
_ITEM_START = re.compile(r"^ {0,4}(\d+)\. \*\*[^*]+\*\*\s*\(")


def parse_sketches(md: str) -> list[Sketch]:
    return parse_sketches_checked(md)[0]


def parse_sketches_checked(md: str) -> tuple[list[Sketch], list[str]]:
    """Reads only the section numbered 3; numbered rules elsewhere are ignored. Also returns
    the headings that look like sketches but could not be parsed (never merged into the
    previous sketch)."""
    unparsed: list[str] = []
    sketches: list[Sketch] = []
    group = ""
    cur: dict | None = None
    bullets: list[str] = []

    def flush():
        if cur is not None:
            sketches.append(Sketch(body="\n".join(bullets), **cur))

    for line in _section_lines(md, 3):
        g = _GROUP.match(line)
        if g:
            flush()
            cur, bullets, group = None, [], g.group(1)
            continue
        h = _HEADING.match(line)
        parts = _split_desc(h.group("desc")) if h else None
        item = _ITEM_START.match(line)
        if item and not (h and parts):
            flush()
            cur, bullets = None, []
            unparsed.append(f"item {item.group(1)}: heading not understood"
                            + ("" if h is None else " (unknown race or class)"))
            continue
        if h and parts:
            flush()
            race, spec, cls = parts
            tags = tuple(t.strip().strip("*").strip() for t in (h.group("tags") or "").split(",")
                         if t.strip().strip("*").strip())
            cur = dict(index=int(h.group("n")), name=h.group("name").strip(),
                       real_name=h.group("real").strip(), age=int(h.group("age")), race=race,
                       spec=spec, class_name=cls,
                       gender="female" if h.group("g") == "F" else "male",
                       region=h.group("region"), tags=tags, group=group)
            bullets = []
            continue
        if cur is None or not line.strip():
            continue
        if re.match(r"^\s*- ", line):
            bullets.append(line.strip()[2:].strip())
        elif bullets:
            bullets[-1] += " " + line.strip()
    flush()
    return sketches, unparsed


def sketch_context(md: str, sketch: Sketch) -> str:
    """The sketch body plus every line of sections 1-5 that mentions the name, capped."""
    pat = re.compile(rf"\b{re.escape(sketch.name)}\b")
    seen = {b.strip() for b in sketch.body.splitlines()}
    used = estimate_tokens(sketch.body)
    mentions: list[str] = []
    full = False
    for n in range(1, 6):
        for line in _section_lines(md, n):
            s = line.strip()
            if not s or not pat.search(s):
                continue
            s = s[2:].strip() if s.startswith("- ") else s
            if s in seen:
                continue
            seen.add(s)
            cost = estimate_tokens(s) + 1
            if used + cost > CONTEXT_TOKEN_CAP:
                full = True
                break
            used += cost
            mentions.append(s)
        if full:
            break
    out = [sketch.body]
    if mentions:
        out.append("Other notes that mention them:")
        out.extend(f"- {m}" for m in mentions)
    return "\n".join(out)


def assign_characters(sketches: list[Sketch], roster: list[Member], human_guid: int,
                      *, cast_only: bool = True) -> Assignment:
    """Two passes: every sketch with an exact race and class match is served first, then the
    rest take a same-class character. cast_only limits the pool to level 80+ members other
    than the human."""
    mapping: dict[int, int] = {}
    fallbacks: dict[int, str] = {}
    used = {human_guid}

    def pool(pred):
        c = [m for m in roster if m.guid not in used and pred(m)
             and (not cast_only or m.level >= CAST_MIN_LEVEL)]
        return sorted(c, key=lambda m: (m.level < CAST_MIN_LEVEL, m.guid))

    for s in sketches:
        exact = pool(lambda m: RACES.get(m.race) == s.race
                     and CLASSES.get(m.class_) == s.class_name)
        if exact:
            used.add(exact[0].guid)
            mapping[s.index] = exact[0].guid
    unmatched: list[int] = []
    for s in sketches:
        if s.index in mapping:
            continue
        same = pool(lambda m: CLASSES.get(m.class_) == s.class_name)
        if not same:
            unmatched.append(s.index)
            continue
        pick = same[0]
        used.add(pick.guid)
        mapping[s.index] = pick.guid
        fallbacks[s.index] = f"{RACES.get(pick.race, '?')} instead of {s.race}"
    return Assignment(mapping, fallbacks, unmatched)


def rename_map(sketches: list[Sketch], assignment: Assignment, roster: list[Member]
               ) -> dict[str, str]:
    by_guid = {m.guid: m for m in roster}
    return {s.name: by_guid[assignment.mapping[s.index]].name
            for s in sketches if s.index in assignment.mapping
            and by_guid[assignment.mapping[s.index]].name != s.name}


def apply_renames(text: str, renames: dict[str, str], *, any_case: bool = False) -> str:
    """Sketch names, as whole words, become character names. Prose matches them as written (a
    sketch name may also be an ordinary word); any_case is for typed chat, where a lower-case
    typist writes "quillo": it matches in any case and keeps the case it was typed in."""
    if not renames:
        return text
    names = sorted(renames, key=len, reverse=True)
    pat = re.compile(r"\b(?:" + "|".join(re.escape(n) for n in names) + r")\b",
                     re.IGNORECASE if any_case else 0)
    if not any_case:
        return pat.sub(lambda m: renames[m.group(0)], text)
    lookup = {k.casefold(): v for k, v in renames.items()}
    return pat.sub(lambda m: cased_like(m.group(0), lookup[m.group(0).casefold()]), text)


def _rename_tree(v, renames):
    if isinstance(v, str):
        return apply_renames(v, renames)
    if isinstance(v, list):
        return [_rename_tree(x, renames) for x in v]
    if isinstance(v, dict):
        return {apply_renames(k, renames): _rename_tree(x, renames) for k, x in v.items()}
    return v


def _quoted_lines(sketch: Sketch) -> list[str]:
    out = []
    for b in sketch.body.splitlines():
        if b.startswith('"'):
            out.extend(q.strip() for q in re.findall(r'"([^"]+)"', b) if q.strip())
    return out


SYSTEM_PROMPT = (
    "You write persona records for the adult members of a World of Warcraft guild on a private "
    "server. Each member is a real person at a keyboard, aged 18 or over, with a job, a life, "
    "habits and a voice. Chat is casual and plain. Nothing is explicit and nothing is "
    "non-consensual; any romance is light and aims only at the owner's character. Never write "
    "like an assistant. Reply with one JSON object that matches the schema. Respect every "
    "limit: likes and dislikes at most 6, values, nevers, knowledge gaps and speech habits at "
    "most 5, wants at most 4, relationships at most 8, sample lines at most 12, and each string "
    "short (300 characters at most, most far less).")


def _user_prompt(sketch: Sketch, context: str, character: Member, human: HumanIdentity) -> str:
    return (
        f"The sketch below calls this person {sketch.name}. Keep that name wherever the "
        f"character is meant; it is swapped for the real character name afterwards. They play "
        f"a {RACES.get(character.race, '')} {CLASSES.get(character.class_, '')} "
        f"(sketch: {sketch.race} {sketch.spec} {sketch.class_name}), {sketch.gender} player, "
        f"age {sketch.age}, {sketch.region}.\n"
        f'The owner plays {human.describe(include_real_name=False)}. Use "@human" as `other` in relationships that '
        f"point at the owner, and {human.called} in running text. Other members in the sketch keep "
        f"their sketch names.\n\n--- sketch ---\n{context}\n--- end ---\n\n"
        "Fill in every field of the record so it fits the sketch: personality scores with a "
        "behaviour each, typing habits, real life, habits, romance stance (drawn only when the "
        "sketch says so, otherwise fond or none), opinions, speech habits, relationships "
        "(friend, friction or stance), a few new sample lines in their voice, and up to two "
        f"bits. {TYPING_RULE} Do not invent anything explicit.")


def _sub(data: dict, key: str) -> dict:
    v = data.get(key)
    if not isinstance(v, dict):
        raise ValueError(f"{key}: must be an object")
    return v


def seed_spec(spec: str, class_name: str) -> tuple[str | None, str]:
    """(main spec, raid role) from a sketch's spec: the first whole word that names a spec
    of the class decides both, from SEED_SPECS. Without one the main spec is left to the
    model (None) and the seed deals damage, ranged or melee by class."""
    specs = SEED_SPECS.get(class_name, {})
    for word in re.findall(r"[a-z]+", spec.lower()):
        main = _SPEC_WORDS.get(word)
        if main in specs:
            return main, specs[main]
    return None, "ranged" if class_name in _RANGED_CLASSES else "melee"


def _build(data: dict, sketch: Sketch, character: Member, renames: dict[str, str],
           notes: list[str] | None = None, human: HumanIdentity | None = None) -> Persona:
    """`notes` gets how many sample lines were cut, for this attempt only (chat_json may call
    this again after a refusal)."""
    if sketch.age < 18:
        raise ValueError("sketch age is under 18")
    if notes is not None:
        notes.clear()
    model_lines = data.get("sample_lines")  # typed chat: renamed in any case below
    data = _rename_tree({k: v for k, v in data.items() if k != "sample_lines"}, renames)
    data["schema"] = 1
    data["guid"] = character.guid
    data["character_name"] = character.name
    data["core"] = True
    _sub(data, "identity").update(
        real_name=apply_renames(sketch.real_name, renames), age=sketch.age,
        player_gender=sketch.gender, region=sketch.region, accent=_ACCENT[sketch.region])
    play = _sub(data, "play")
    play["guild_role"] = _GUILD_ROLE.get(sketch.group, "social")
    main, play["raid_role"] = seed_spec(sketch.spec, sketch.class_name)
    if main is None:  # no spec word fits the class: the model's spec only if it plays that role
        fits = [s for s, role in SEED_SPECS.get(sketch.class_name, {}).items()
                if role == play["raid_role"]]
        if fits and play.get("main_spec") not in fits:
            main = fits[0]
    if main is not None:
        play["main_spec"] = main
        if play.get("off_spec") == main:
            play["off_spec"] = ""
    romance = _sub(data, "romance")
    romance.pop("dial", None)  # his to set by hand, never the model's
    # every man gets a jealousy mode, as generation gives each one (spec review Minor 5): the
    # sketch's tag, else the model's, else one by guid
    tag = next((_JEALOUSY_TAGS[t.casefold()] for t in sketch.tags
                if t.casefold() in _JEALOUSY_TAGS), None)
    if tag:
        romance["jealousy_mode"] = tag
    elif sketch.gender == "male" and romance.get("jealousy_mode") not in JEALOUSY_MODES:
        romance["jealousy_mode"] = JEALOUSY_MODES[character.guid % len(JEALOUSY_MODES)]
    if any(t.startswith("drawn to him") for t in sketch.tags):
        romance["stance"] = "drawn"
        style = next((t for t in sketch.tags if t in _STYLES), None)
        if style:
            romance["style"] = style
    elif romance.get("stance") in ("drawn", "lust"):
        romance["stance"] = "fond"
        romance["style"] = None
    quoted: list[dict] = []
    for q in _quoted_lines(sketch):
        t = apply_renames(q, renames, any_case=True)
        if all(t != x["text"] for x in quoted):
            quoted.append({"text": t, "approved": True})
    have = {q["text"] for q in quoted}
    extra = [{"text": t, "approved": False}
             for x in (model_lines if isinstance(model_lines, list) else [])
             if isinstance(x, dict) and isinstance(x.get("text"), str)
             and (t := apply_renames(x["text"], renames, any_case=True)) not in have]
    lines = quoted + extra
    if notes is not None and len(lines) > MAX_SAMPLE_LINES:
        notes.append(str(len(lines) - MAX_SAMPLE_LINES))
    data["sample_lines"] = lines[:MAX_SAMPLE_LINES]
    human = human or HumanIdentity(0, "", "", "", False)
    rels = data.get("relationships")
    if isinstance(rels, list):
        for r in rels:  # the human by any of his names is "@human", as generation writes it
            if isinstance(r, dict) and isinstance(r.get("other"), str) \
                    and refers_to_human(r["other"], human):
                r["other"] = "@human"
        data["relationships"] = [r for r in rels if not (
            isinstance(r, dict) and str(r.get("other", "")).casefold()
            == character.name.casefold())]
    repair_typing(data.get("typing"))  # a backwards map is turned round, emoji dropped
    check_no_real_name(data, human)
    check_romance(data, human, ())
    p = persona_from_dict(data)
    errs = validate_persona(p)
    if errs:
        raise ValueError("; ".join(errs[:25]))
    return p


def sketch_real_name_problem(sketch: Sketch, character: Member, renames: dict[str, str],
                             human: HumanIdentity) -> str | None:
    """The fields forced in after the model (the sketch's real name, its quoted lines and the
    character name) cannot be fixed by asking again, so they are checked before any call.
    The message never contains the name."""
    if not human.real_name:
        return None
    label = f"sketch {sketch.index} {sketch.name}"
    if has_word(apply_renames(sketch.real_name, renames), human.real_name) or (
            has_word(character.name, human.real_name)):
        return f"{label}: real name contains the owner's real name; use the nickname"
    for q in _quoted_lines(sketch):
        if mentions_real_name("sample_lines", apply_renames(q, renames), human):
            return f"{label}: a quoted sample line contains the owner's real name; use the nickname"
    return None


async def convert_sketch(llm: LLMClient, model: str, sketch: Sketch, context: str,
                         character: Member, renames: dict[str, str], human: HumanIdentity,
                         notes: list[str] | None = None) -> Persona:
    problem = sketch_real_name_problem(sketch, character, renames, human)
    if problem:
        raise ValueError(problem)
    context = redact_real_name(context, human, human.called)
    user = _user_prompt(sketch, context, character, human)
    schema = persona_json_schema()
    last: Exception | None = None
    for attempt in range(2):
        messages = [{"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user + (
                        "" if attempt == 0 else
                        "\n\nEarlier attempts were refused (" + str(last) + "). Keep every "
                        "list at or under its maximum and condense the wording instead of "
                        "listing more.")}]
        try:
            return await chat_json(llm, model, messages, schema, STRUCTURE,
                                   lambda d: _build(d, sketch, character, renames, notes, human))
        except JSONJobFailed as e:
            last = e
    raise last


# A member's starting row toward the human, by romance stance.
HUMAN_SEED = {
    "lust": dict(affinity=30.0, attraction=60.0, familiarity=30.0, romance_stage="notices"),
    "drawn": dict(affinity=30.0, attraction=40.0, familiarity=30.0, romance_stage="notices"),
    "fond": dict(affinity=30.0, familiarity=30.0),
    "none": dict(affinity=10.0, familiarity=20.0),
}


# What a member's own declaration toward the human adds to the stance's row (controller ruling
# "final I2"): a friend starts close to him (talk.prompt.is_close: affinity 40 or more), a
# friction starts annoyed with him, each with the declaration's note, and a "stance"
# declaration adds only its note. The stance's attraction and romance stage always stay.
HUMAN_KIND = {
    "friend": dict(affinity=40.0, trust=40.0, familiarity=60.0, label="friend"),
    "friction": dict(affinity=-20.0, familiarity=50.0, label="friction"),
    "stance": {},
}
KIND_ORDER = ("friend", "friction", "stance")  # which declaration decides when there are several


def human_seed(guid: int, human_guid: int, stance: str, kind: str = "stance",
               note: str = "") -> Relationship:
    """The row a member with this stance and this declaration toward the human starts with
    (last_seen 0)."""
    row = Relationship(a=guid, b=human_guid, **HUMAN_SEED.get(stance, HUMAN_SEED["none"]))
    return dataclasses.replace(row, **HUMAN_KIND.get(kind, {}), notes=note)


def kind_of(row: Relationship) -> str:
    """The declaration kind a seeded row toward the human was made from."""
    return row.label if row.label in ("friend", "friction") else "stance"


def seed_values(r: Relationship) -> tuple:
    """What seeding sets on a row: everything but the pair, last_seen and interactions."""
    return (r.affinity, r.trust, r.respect, r.attraction, r.familiarity, r.label,
            r.romance_stage, r.notes, tuple(r.pinned))


def human_ref(human: HumanIdentity, name_to_guid: dict[str, int] | None = None
              ) -> Callable[[str], bool]:
    """A test for relationship targets that mean the human: screens.refers_to_human, or a
    roster name whose guid is his."""
    mine = {n.casefold() for n, g in (name_to_guid or {}).items() if g == human.guid}
    return lambda other: refers_to_human(other, human) or str(other).casefold() in mine


def human_declaration(p: Persona, toward: Callable[[str], bool]) -> tuple[str, str]:
    """(kind, note) of what p declares toward the human: the first friend, else the first
    friction, else the first stance declaration; ("stance", "") when there is none."""
    mine = [r for r in p.relationships if toward(r.other)]
    for kind in KIND_ORDER:
        for r in mine:
            if r.kind == kind:
                return kind, " ".join(r.note.split())
    return "stance", ""


def seed_relationships(store: Store, personas, name_to_guid: dict[str, int], human_guid: int,
                       ts: float, *, human: HumanIdentity | None = None) -> int:
    """Starting values from each persona's stance toward the human with what it declares
    toward him (human_seed), and from its friend/friction list for everyone else. Targets
    match in any case; `human` lets his nickname, character name and "GM" mean him too. A pair
    that already has a row is left alone, so repeating this is safe. Returns the rows
    written."""
    seen = int(ts)
    toward = human_ref(dataclasses.replace(human or HumanIdentity(0, "", "", "", False),
                                           guid=human_guid), name_to_guid)
    by_name = {n.casefold(): g for n, g in name_to_guid.items()}
    rows: list[Relationship] = []
    for p in (personas.values() if isinstance(personas, dict) else personas):
        rows.append(human_seed(p.guid, human_guid, p.romance.stance,
                               *human_declaration(p, toward)))
        for r in p.relationships:
            other = None if toward(r.other) else by_name.get(r.other.casefold())
            if other is None or other in (p.guid, human_guid):
                continue
            if r.kind == "friend":
                rows.append(Relationship(a=p.guid, b=other, affinity=40, trust=40,
                                         familiarity=60, label="friend", notes=r.note))
            elif r.kind == "friction":
                rows.append(Relationship(a=p.guid, b=other, affinity=-20, familiarity=50,
                                         label="friction", notes=r.note))
    n = 0
    with store.transaction():
        for r in rows:
            if store.get_relationship(r.a, r.b) != Relationship(a=r.a, b=r.b):
                continue  # already seeded or lived in
            store.put_relationship(dataclasses.replace(r, last_seen=seen))
            n += 1
    return n


def _table(sketches, assignment, by_guid) -> list[str]:
    rows = []
    for s in sketches:
        label = f"{s.index:>2} {s.name:<12} {s.race} {s.spec} {s.class_name}"
        guid = assignment.mapping.get(s.index)
        if guid is None:
            rows.append(f"{label}  ->  UNMATCHED")
            continue
        m = by_guid[guid]
        note = (f"  [fallback: {assignment.fallbacks[s.index]}]"
                if s.index in assignment.fallbacks else "")
        rows.append(f"{label}  ->  {m.name} (guid {guid}, level {m.level}, "
                    f"{RACES.get(m.race, '?')} {CLASSES.get(m.class_, '?')}){note}")
    return rows


def unknown_relations(personas, name_to_guid: dict[str, int], *,
                      human: HumanIdentity | None = None, cast=None) -> list[str]:
    """Relationship targets that are neither the human (human_ref) nor on the roster, plus,
    when `cast` (the cast's guids) is given, roster members who are not cast: no persona
    will ever answer for them."""
    toward = human_ref(human or HumanIdentity(0, "", "", "", False), name_to_guid)
    by_name = {n.casefold(): g for n, g in name_to_guid.items()}
    out = []
    for p in personas:
        for r in p.relationships:
            if toward(r.other):
                continue
            g = by_name.get(r.other.casefold())
            if g is None:
                out.append(f"{p.character_name} -> {r.other}")
            elif cast is not None and g not in cast:
                out.append(f"{p.character_name} -> {r.other} (on the roster, not cast)")
    return out


async def seed_cast(store: Store, llm: LLMClient | None, cfg, md_path, *, human_guid: int,
                    dry_run: bool = False, force: bool = False,
                    progress: Callable[[str], None] | None = None) -> SeedReport:
    md = Path(md_path).read_text(encoding="utf-8")
    sketches, unparsed = parse_sketches_checked(md)
    roster = list(store.cast().values())
    by_guid = {m.guid: m for m in roster}
    assignment = assign_characters(sketches, roster, human_guid)
    renames = rename_map(sketches, assignment, roster)
    report = SeedReport(assigned=len(assignment.mapping), unmatched=list(assignment.unmatched),
                        table=_table(sketches, assignment, by_guid), unparsed=unparsed)
    report.not_cast = [f"{m.name} (level {m.level})" for m in roster
                       if m.guid != human_guid and m.level < CAST_MIN_LEVEL]
    names = {m.name: m.guid for m in roster}
    cast = {m.guid for m in roster if m.guid != human_guid and m.level >= CAST_MIN_LEVEL}
    human = human_identity(cfg, store, human_guid)
    for s in sketches:
        g = assignment.mapping.get(s.index)
        problem = g is not None and sketch_real_name_problem(s, by_guid[g], renames, human)
        if problem:
            report.failed[g] = problem
    if dry_run:
        stored = [p for g in assignment.mapping.values() if (p := load_persona(store, g))]
        report.unknown_relations = unknown_relations(stored, names, human=human, cast=cast)
        return report
    say = progress or (lambda _s: None)
    existing = store.persona_versions()
    for s in sketches:
        guid = assignment.mapping.get(s.index)
        if guid is None:
            continue
        if guid in report.failed:
            continue
        if guid in existing and not force:
            report.skipped.append(guid)
            continue
        say(f"converting {s.index}/{len(sketches)}")
        notes: list[str] = []
        try:
            p = await convert_sketch(llm, cfg.models.reflection, s, sketch_context(md, s),
                                     by_guid[guid], renames, human, notes)
        except Exception as e:
            report.failed[guid] = f"{e.__class__.__name__}: {str(e)[:200]}"
            continue
        m = by_guid[guid]
        save_persona(store, p, source="seed", ts=store.clock.now(), race=RACES.get(m.race, ""),
                     class_name=CLASSES.get(m.class_, ""))
        report.created.append(guid)
        if notes:
            report.cut_lines[guid] = int(notes[-1])
    # Every assigned persona gets its relationships, from what is stored, so a run that died
    # between saving and seeding (or a rerun) still ends complete.
    stored = [p for g in assignment.mapping.values() if (p := load_persona(store, g))]
    seed_relationships(store, stored, names, human_guid, store.clock.now(), human=human)
    report.unknown_relations = unknown_relations(stored, names, human=human, cast=cast)
    return report
