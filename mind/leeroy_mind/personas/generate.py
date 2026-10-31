"""Generating the rest of the cast: plan one slot per cast member that has no persona so the
whole cast hits the owner's mix targets, then write each person in two passes (a free-form
profile, then the structured record). Slot fields are forced after the model answers, and
over-long fields are never truncated: the reply is validated and asked again, condensed. A
real name that is still taken after one informed retry is swapped for a free one from a short
list of candidate names."""
import logging
import random
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from ..config import CastCfg
from ..human import HumanIdentity, human_identity
from ..llm.client import LLMClient, LLMError
from ..llm.presets import REASON, STRUCTURE, JSONJobFailed, chat_json
from ..prompts import load_prompt
from ..protocol import Member
from ..storage.store import Store
from ..text import estimate_tokens, truncate_words
from ..wow import CAST_MIN_LEVEL, CLASSES, RACES, class_roles
from .io import load_persona, save_persona
from .schema import (JEALOUSY_MODES, MIN_AGE, TYPING_RULE, Persona, persona_from_dict,
                     persona_json_schema, persona_to_dict, validate_persona)
from .screens import (NAME_FIELDS, check_no_real_name, check_romance, human_aliases,
                      name_keys, refers_to_human, touches_real_name)
from .seed import seed_relationships
from .stances import LUST_STYLES, NONE_FIT_AGE, slot_stances
from .typing_fix import repair_typing

log = logging.getLogger("leeroy_mind.generate")

UK_SHARE = 1 / 3
RAIDER_SHARE = 0.7
DRINKER_SHARE = 0.45
JEALOUSY_CYCLE = JEALOUSY_MODES
THINKS_AS = ("theorycrafter", "lore", "social", "collector", "competitor")
AGE_BANDS = ((21, 25), (26, 32), (33, 40), (41, 55))
AGE_WEIGHTS = (2, 4, 3, 1)
TANK_SHARE = 0.10
HEALER_SHARE = 0.24
ROLES = ("tank", "healer", "damage")
# class -> role -> [(main spec, raid role)]; chosen by guid so the choice is stable
SPECS = {
    "Warrior": {"tank": [("Protection", "tank")], "damage": [("Arms", "melee"), ("Fury", "melee")]},
    "Paladin": {"tank": [("Protection", "tank")], "healer": [("Holy", "healer")],
                "damage": [("Retribution", "melee")]},
    "Death Knight": {"tank": [("Blood", "tank")],
                     "damage": [("Frost", "melee"), ("Unholy", "melee")]},
    "Druid": {"tank": [("Feral", "tank")], "healer": [("Restoration", "healer")],
              "damage": [("Balance", "ranged"), ("Feral", "melee")]},
    "Priest": {"healer": [("Discipline", "healer"), ("Holy", "healer")],
               "damage": [("Shadow", "ranged")]},
    "Shaman": {"healer": [("Restoration", "healer")],
               "damage": [("Elemental", "ranged"), ("Enhancement", "melee")]},
    "Mage": {"damage": [("Arcane", "ranged"), ("Fire", "ranged"), ("Frost", "ranged")]},
    "Warlock": {"damage": [("Affliction", "ranged"), ("Demonology", "ranged"),
                           ("Destruction", "ranged")]},
    "Rogue": {"damage": [("Assassination", "melee"), ("Combat", "melee"), ("Subtlety", "melee")]},
    "Hunter": {"damage": [("Beast Mastery", "ranged"), ("Marksmanship", "ranged"),
                          ("Survival", "ranged")]},
}
STYLES = ("bold", "shy", "earnest", "theatrical", "guarded", "competitive")
MAX_SAMPLE_LINES = 12
MIN_BIT_GAP_DAYS = 7.0
MIN_YEARS_PLAYED = 3
CAST_SUMMARY_TOKEN_CAP = 900
OVERLAY_TOKEN_CAP = 1200
TAKEN_LIST_TOKEN_CAP = 300
RENAME_CANDIDATES = 8
RENAME_SCHEMA = {
    "type": "object",
    "properties": {"names": {"type": "array", "items": {"type": "string"}, "minItems": 1,
                             "maxItems": RENAME_CANDIDATES}},
    "required": ["names"], "additionalProperties": False}
_ACCENT = {"UK": "en-gb", "US": "en-us"}

GENERATION_RULES = (
    "You write persona records for the members of a World of Warcraft guild on a private "
    "server. Each member is a real adult at a keyboard (18 or over) who has played WoW for "
    "three or more years and raids at endgame. It is 2009 and they write as people did then.\n"
    "- Write traits, not identities: what they do, how they type, what annoys them. A quirk is "
    "at most a rare bit, never a whole personality.\n"
    "- No meta or AI persona, never an assistant's voice, and no newbies.\n"
    "- Negative traits are allowed and make people real: pettiness, laziness, a short temper.\n"
    "- Drinking and smoking are written as adults do them, without fuss.\n"
    "- Nothing non-consensual, ever, and nothing under 18.\n"
    "- Any romance is aimed only at the guild master's character, never between members.\n"
    "- Keep every field short and concrete; respect each list limit.")


@dataclass(frozen=True)
class GenSlot:
    guid: int
    player_gender: str
    introvert: bool
    guild_role: str
    region: str
    drinker: bool
    romance_stance: str
    jealousy_mode: str | None
    thinks_as: str
    age_band: tuple[int, int]
    combat_role: str = "damage"


@dataclass(frozen=True)
class MixReport:
    total: int
    female_share: float
    introvert_share: float
    roles: dict = field(default_factory=dict)            # role -> count
    female_by_role: dict = field(default_factory=dict)   # role -> share of women


@dataclass
class GenReport:
    planned: int = 0
    created: list[int] = field(default_factory=list)
    failed: dict[int, str] = field(default_factory=dict)
    slots: list[GenSlot] = field(default_factory=list)
    collisions: list[str] = field(default_factory=list)
    dropped_targets: dict[int, list[str]] = field(default_factory=dict)


@dataclass
class GenNotes:
    collisions: list[str] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)
    renames: list[tuple[str, str]] = field(default_factory=list)  # (old real name, new one)


class NameCollision(ValueError):
    pass


def spec_for(class_id: int, role: str, guid: int) -> tuple[str, str] | None:
    """(main spec, raid role) for a class in a combat role, or None for an unknown class."""
    options = SPECS.get(CLASSES.get(class_id, ""), {}).get(role)
    return options[guid % len(options)] if options else None


def _is_introvert(p: Persona) -> bool:
    return p.big_five.extraversion.score <= 4


def role_of(p: Persona) -> str:
    return p.play.raid_role if p.play.raid_role in ("tank", "healer") else "damage"


def mix_report(personas: Sequence[Persona]) -> MixReport:
    n = len(personas)
    if n == 0:
        return MixReport(0, 0.0, 0.0)
    roles = {r: sum(role_of(p) == r for p in personas) for r in ROLES}
    women = {r: sum(role_of(p) == r and p.identity.player_gender == "female" for p in personas)
             for r in ROLES}
    return MixReport(n, sum(p.identity.player_gender == "female" for p in personas) / n,
                     sum(_is_introvert(p) for p in personas) / n, roles,
                     {r: (women[r] / roles[r] if roles[r] else 0.0) for r in ROLES})


def cast_summary(personas: Sequence[Persona], max_tokens: int = CAST_SUMMARY_TOKEN_CAP,
                 human: HumanIdentity | None = None) -> str:
    """One short line per existing person, so a new one avoids duplicating them. Over the
    token cap the first lines (the lowest guids) are dropped."""
    lines = [f"- {p.character_name}: {p.identity.real_name}, {p.identity.age}, "
             f"{p.identity.player_gender[0]}, {p.real_life.job}, {p.play.guild_role}"
             for p in personas
             if not (human and touches_real_name(p.identity.real_name, human))]
    kept, used = [], 0
    for line in reversed(lines):
        used += estimate_tokens(line) + 1
        if used > max_tokens:
            break
        kept.append(line)
    if len(kept) < len(lines):
        log.info("cast summary cut to the newest %d of %d lines", len(kept), len(lines))
    return "\n".join(reversed(kept))


def _flags(n: int, k: int, rng: random.Random) -> list[bool]:
    out = [True] * k + [False] * (n - k)
    rng.shuffle(out)
    return out


def _give_first(values: list, to: Sequence[int], good) -> None:
    """Swap values so the slots in `to` hold ones that satisfy `good`, taking them from the
    other slots in order. The multiset of values is unchanged, so every share still holds."""
    chosen = set(to)
    spare = [j for j in range(len(values)) if j not in chosen and good(values[j])]
    for i in to:
        if not good(values[i]) and spare:
            j = spare.pop(0)
            values[i], values[j] = values[j], values[i]


def plan_slots(roster: Sequence[Member], existing: Sequence[Persona], cfg: CastCfg,
               human_guid: int, rng: random.Random) -> list[GenSlot]:
    """One slot per cast member (level >= CAST_MIN_LEVEL, not the human) without a persona.
    The whole cast, existing personas included, should have round(share * N) women and
    introverts; the slots supply what is missing, never below zero. The women's romance
    stances follow stances.STANCE_SHARES over every woman in the cast: existing personas
    keep theirs and the slots make up the rest. Lust slots take the outgoing flags first and
    have their age band capped below NONE_FIT_AGE (41-55 becomes 41-49), so a generated lust
    woman is one the rebalance would pick for lust too."""
    cast = sorted((m for m in roster if m.guid != human_guid and m.level >= CAST_MIN_LEVEL),
                  key=lambda m: m.guid)
    cast_guids = {m.guid for m in cast}
    have = [p for p in existing if p.guid in cast_guids]
    have_guids = {p.guid for p in have}
    open_ = [m for m in cast if m.guid not in have_guids]
    n, m = len(cast), len(open_)

    def need(share: float, already: int) -> int:
        return max(0, min(m, round(share * n) - already))

    n_intro = need(cfg.introvert_share, sum(_is_introvert(p) for p in have))
    # Roles first: tanks and healers are chosen from the classes that can fill them.
    tank_ex = [x for x in open_ if class_roles(x.class_) >= {"tank"}
               and "healer" not in class_roles(x.class_)]
    heal_ex = [x for x in open_ if class_roles(x.class_) >= {"healer"}
               and "tank" not in class_roles(x.class_)]
    flex = [x for x in open_ if class_roles(x.class_) >= {"tank", "healer"}]
    for pool in (tank_ex, heal_ex, flex):
        rng.shuffle(pool)
    have_tank = sum(role_of(p) == "tank" for p in have)
    have_heal = sum(role_of(p) == "healer" for p in have)
    tank_pool = tank_ex + flex
    tanks = tank_pool[:max(0, min(len(tank_pool), round(TANK_SHARE * n) - have_tank))]
    tank_guids = {x.guid for x in tanks}
    heal_pool = heal_ex + [x for x in flex if x.guid not in tank_guids]
    healers = heal_pool[:max(0, min(len(heal_pool), round(HEALER_SHARE * n) - have_heal))]
    role_of_guid = {**{x.guid: "tank" for x in tanks}, **{x.guid: "healer" for x in healers}}
    damage = [x for x in open_ if x.guid not in role_of_guid]
    have_f = sum(p.identity.player_gender == "female" for p in have)
    # healers are women and tanks are men; the damage dealers make up the rest of the share
    n_dmg_women = max(0, min(len(damage),
                             round(cfg.female_share * n) - have_f - len(healers)))
    dmg_female = dict(zip((x.guid for x in damage), _flags(len(damage), n_dmg_women, rng)))
    female = [role_of_guid.get(x.guid) == "healer" or dmg_female.get(x.guid, False)
              for x in open_]
    share = (have_f + sum(female)) / n if n else 0.0
    tank_n, heal_n = have_tank + len(tanks), have_heal + len(healers)
    tank_t, heal_t = round(TANK_SHARE * n), round(HEALER_SHARE * n)
    got = (tank_n, heal_n, n - tank_n - heal_n)
    want = (tank_t, heal_t, n - tank_t - heal_t)
    # a warning reaches the CLI (no logging is set up there); on target it stays at info
    off = n > 0 and (abs(share - cfg.female_share) > 0.01
                     or any(g < w for g, w in zip(got, want)))
    log.log(logging.WARNING if off else logging.INFO,
            "cast plan: female share %.3f (target %.2f) of %d, tanks %d/%d, healers %d/%d, "
            "damage %d/%d%s", share, cfg.female_share, n, got[0], want[0], got[1], want[1],
            got[2], want[2], "; off target" if off else "")
    intro = _flags(m, n_intro, rng)
    uk = _flags(m, round(UK_SHARE * m), rng)
    raider = _flags(m, round(RAIDER_SHARE * m), rng)
    drinker = _flags(m, round(DRINKER_SHARE * m), rng)
    stances = slot_stances([p.romance.stance for p in have
                            if p.identity.player_gender == "female"], sum(female))
    rng.shuffle(stances)
    jealous_at = rng.randrange(len(JEALOUSY_CYCLE))
    thinks_at = rng.randrange(len(THINKS_AS))
    bands = rng.choices(AGE_BANDS, weights=AGE_WEIGHTS, k=m)
    it = iter(stances)
    slot_stance = [next(it) if female[i] else "none" for i in range(m)]
    lust = [i for i in range(m) if slot_stance[i] == "lust"]
    _give_first(intro, lust, lambda introvert: not introvert)
    for i in lust:  # under the grandmother age; _build clamps the age into the band
        lo, hi = bands[i]
        bands[i] = (min(lo, NONE_FIT_AGE - 1), min(hi, NONE_FIT_AGE - 1))
    slots, men = [], 0
    for i, member in enumerate(open_):
        stance, jealousy = slot_stance[i], None
        if not female[i]:
            jealousy = JEALOUSY_CYCLE[(jealous_at + men) % len(JEALOUSY_CYCLE)]
            men += 1
        slots.append(GenSlot(
            guid=member.guid, player_gender="female" if female[i] else "male",
            introvert=intro[i], guild_role="raider" if raider[i] else "social",
            region="UK" if uk[i] else "US", drinker=drinker[i], romance_stance=stance,
            jealousy_mode=jealousy, thinks_as=THINKS_AS[(thinks_at + i) % len(THINKS_AS)],
            age_band=bands[i], combat_role=role_of_guid.get(member.guid, "damage")))
    return slots


# -- one persona ------------------------------------------------------------------------------

def _spec_name(character: Member, slot: GenSlot) -> str:
    chosen = spec_for(character.class_, slot.combat_role, character.guid)
    return chosen[0] if chosen else "their own spec"


def _slot_text(slot: GenSlot, character: Member, human: HumanIdentity) -> str:
    lo, hi = slot.age_band
    _, them, _ = human.forms
    romance = {
        "lust": f"openly wants {human.called}, the owner's character, and is the most forward "
                f"woman in the guild with {them} (the record itself stays non-explicit)",
        "drawn": f"drawn to {human.called}, the owner's character: clearly into {them} and "
                 "flirty (the record itself stays non-explicit)",
        "fond": f"fond of {human.called}: warm and attentive",
        "none": f"no romantic interest in {human.called}"}[slot.romance_stance]
    jealous = (f"; when others get {human.called}'s attention they react as: "
               f"{slot.jealousy_mode.replace('_', ' ')}") if slot.jealousy_mode else ""
    return (
        f"Character: {character.name}, a {RACES.get(character.race, '')} "
        f"{CLASSES.get(character.class_, '')}.\n"
        f"Plays {_spec_name(character, slot)} as {slot.combat_role}.\n"
        f"Player: {slot.player_gender}, aged {lo}-{hi}, from the {slot.region}, "
        f"{'introverted' if slot.introvert else 'outgoing'}, guild role {slot.guild_role}, "
        f"{'drinks (and may smoke)' if slot.drinker else 'does not drink or smoke'}, "
        f"thinks about the game as a {slot.thinks_as}.\n"
        f"Romance: {romance}{jealous}.\n"
        f"The owner plays {human.describe(include_real_name=False)}.")


def _profile_prompt(slot, character, summary, human) -> str:
    return (
        f"{_slot_text(slot, character, human)}\n\n"
        "Write a profile of about 250 words of the real person behind this character: job, "
        "home life, how they play, how they talk, what they like and dislike, one flaw, and one "
        "private secret that is not romantic and does not involve another guildmate. Make them different from the people already in the guild:\n"
        f"{summary or '(nobody yet)'}")


async def _write_profile(llm, model, rules, user, character_name) -> str:
    """The thinking model now and then spends its whole reply thinking and returns no text, so
    an empty profile is asked for once more before the slot fails."""
    messages = [{"role": "system", "content": rules}, {"role": "user", "content": user}]
    for attempt in range(2):
        r = await llm.chat(model, messages, options=dict(REASON.options), think=REASON.think,
                           format=None, timeout=REASON.timeout)
        if r.text.strip():
            return r.text.strip()
        if attempt == 0:
            log.warning("profile_empty_retry: %s", character_name)
    raise ValueError("the profile pass returned nothing")


def _printable_first_names(taken, human: HumanIdentity,
                           character_names=frozenset()) -> list[str]:
    """De-duplicated first names for the retry prompt, capped by tokens. The owner's real name
    is never printed (it still blocks collisions through the keys)."""
    hidden = name_keys(human.real_name) if human.real_name else set()
    seen: dict[str, str] = {}
    # persona real names first, character names last, so the cap cuts character names first
    for n in sorted(taken, key=lambda x: (x in character_names, x.casefold())):
        first = n.split()[0] if n.split() else ""
        k = first.casefold()
        if first and k not in seen and k not in hidden:
            seen[k] = first
    out, used = [], 0
    for first in seen.values():
        used += estimate_tokens(first) + 1
        if used > TAKEN_LIST_TOKEN_CAP:
            log.info("taken first names cut to %d of %d", len(out), len(seen))
            break
        out.append(first)
    return out


def _record_prompt(slot, character, profile, human, taken) -> str:
    out = (
        f"{_slot_text(slot, character, human)}\n\n--- profile ---\n{profile}\n--- end ---\n\n"
        "Turn the profile into the record: personality scores with a behaviour each, typing "
        "habits, real life, habits, romance, opinions, speech habits, a few sample lines in "
        'their voice and at most one bit. Use "@human" as `other` in a relationship that '
        f"points at {human.called}; name any other member by their character name, exactly. "
        + f"{TYPING_RULE} "
        + ("Include at least one alcohol or smoking habit. " if slot.drinker else "")
        + "Do not invent anything explicit.")
    if taken:
        out += ("\n\nThese first names are already taken, so choose a real name with a "
                "different first name: " + ", ".join(taken) + ".")
    return out


def _rename_prompt(p: Persona, taken) -> str:
    who = p.identity
    out = (f"Player: {who.player_gender}, aged {who.age}, from the {who.region}\n"
           f"Job: {p.real_life.job}\nHome: {p.real_life.household}\n\n"
           f"Suggest {RENAME_CANDIDATES} different real names for this player: a first name, "
           "optionally followed by a surname, of a kind that was common for their age and "
           "region. No nicknames and no fantasy names.")
    if taken:
        out += ("\n\nThese first names are already taken, so every suggestion needs a different "
                "first name: " + ", ".join(taken) + ".")
    return out


_TITLES = frozenset({"dr", "mr", "mrs", "ms", "miss", "mx", "sir", "lady", "lord", "dame",
                     "prof"})
_NAME_WORD = re.compile(r"[^\W\d_]+(?:['-][^\W\d_]+)*")  # letters, inner hyphens or apostrophes


def _looks_like_a_name(name: str) -> bool:
    """One to three words of letters (O'Neil and Jo-Ann pass), not starting with a title: a
    candidate that does not would become a mangled first name in the record."""
    words = name.split()
    return (1 <= len(words) <= 3 and all(_NAME_WORD.fullmatch(w) for w in words)
            and words[0].casefold() not in _TITLES)


def _candidate_names(d: dict) -> list[str]:
    names = d.get("names")
    if not isinstance(names, list):
        raise ValueError("names: must be a list of real names")
    out = [n for n in (" ".join(x.split()) for x in names if isinstance(x, str))
           if n and _looks_like_a_name(n)]
    if not out:
        raise ValueError("names: give at least one real name (a first name and maybe a "
                         "surname: letters only, no titles)")
    return out[:RENAME_CANDIDATES]


def _obj(d: dict, key: str) -> dict:
    v = d.get(key)
    if not isinstance(v, dict):
        raise ValueError(f"{key}: must be an object")
    return v


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _build(data: dict, slot: GenSlot, character: Member, ctx: "_Ctx") -> Persona:
    lo, hi = slot.age_band
    data["schema"] = 1
    data["guid"] = character.guid
    data["character_name"] = character.name
    data["core"] = False
    data["mock_interview"] = ""
    ident = _obj(data, "identity")
    age = ident.get("age")
    low, high = max(lo, MIN_AGE), max(hi, MIN_AGE)
    ident["age"] = (min(max(age, low), high) if isinstance(age, int) and not isinstance(age, bool)
                    else (low + high) // 2)
    ident.update(player_gender=slot.player_gender, region=slot.region,
                 accent=_ACCENT[slot.region])
    trait = _obj(_obj(data, "big_five"), "extraversion")
    score = trait.get("score")
    score = score if isinstance(score, int) and not isinstance(score, bool) else 5
    trait["score"] = min(score, 4) if slot.introvert else max(score, 6)
    game = _obj(data, "game")
    years = game.get("years_played")
    game["years_played"] = (max(years, MIN_YEARS_PLAYED)
                            if isinstance(years, int) and not isinstance(years, bool)
                            else MIN_YEARS_PLAYED)
    game["thinks_as"] = slot.thinks_as
    play = _obj(data, "play")
    play["guild_role"] = slot.guild_role
    chosen = spec_for(character.class_, slot.combat_role, character.guid)
    if chosen is not None:
        play["main_spec"], play["raid_role"] = chosen
        if play.get("off_spec") == chosen[0]:
            play["off_spec"] = ""
    play["officer_title"] = ""
    romance = _obj(data, "romance")
    romance.pop("dial", None)  # his to set by hand, never the model's
    romance["stance"] = slot.romance_stance
    romance["jealousy_mode"] = slot.jealousy_mode
    style = romance.get("style")
    if slot.romance_stance == "lust":  # bold, theatrical or competitive, never shy or guarded
        romance["style"] = style if style in LUST_STYLES else "bold"
    elif slot.romance_stance == "drawn":
        romance["style"] = style if style in STYLES else "earnest"
    else:
        romance["style"] = None
    if not slot.drinker or not isinstance(data.get("habits"), list):
        data["habits"] = []
    bits = data.get("bits")
    kept = []
    for b in (bits[:1] if isinstance(bits, list) else []):
        if isinstance(b, dict):
            gap = b.get("min_days_between")
            b["min_days_between"] = max(float(gap), MIN_BIT_GAP_DAYS) if _is_num(gap) \
                else MIN_BIT_GAP_DAYS
            kept.append(b)
    data["bits"] = kept
    lines = data.get("sample_lines")
    data["sample_lines"] = [{"text": x["text"], "approved": False}
                            for x in (lines if isinstance(lines, list) else [])
                            if isinstance(x, dict) and isinstance(x.get("text"), str)
                            ][:MAX_SAMPLE_LINES]
    ctx.dropped.clear()
    rels = data.get("relationships")
    members = {m.casefold(): m for m in ctx.members if m.casefold() != character.name.casefold()}
    kept_rels = []
    for r in (rels if isinstance(rels, list) else []):
        if not isinstance(r, dict):
            continue
        other = r.get("other")
        if isinstance(other, str) and refers_to_human(other, ctx.human):
            r["other"] = "@human"
        elif isinstance(other, str) and other.casefold() in members:
            r["other"] = members[other.casefold()]  # as the roster spells it
        else:
            ctx.dropped.append(str(other))
            continue
        kept_rels.append(r)
    data["relationships"] = kept_rels
    repair_typing(data.get("typing"))  # a backwards map is turned round, emoji dropped
    check_no_real_name(data, ctx.human)
    check_romance(data, ctx.human, ctx.screen_names, characters=ctx.members)
    p = persona_from_dict(data)
    errs = validate_persona(p)
    if errs:
        raise ValueError("; ".join(errs[:25]))
    if slot.drinker and not any(h.substance in ("alcohol", "tobacco") for h in p.habits):
        raise ValueError("habits: this player drinks or smokes, so include an alcohol or "
                         "tobacco habit")
    return p


@dataclass
class _Ctx:
    human: HumanIdentity
    members: frozenset[str]       # character names a relationship may name
    screen_names: frozenset[str]  # every name the romance screen watches for
    dropped: list[str] = field(default_factory=list)


async def _structured(llm, model, rules, user, slot, character, ctx) -> Persona:
    schema = persona_json_schema()
    last: Exception | None = None
    for attempt in range(2):
        messages = [{"role": "system", "content": rules},
                    {"role": "user", "content": user + (
                        "" if attempt == 0 else
                        "\n\nEarlier attempts were refused (" + str(last) + "). Keep every "
                        "list at or under its maximum and condense the wording instead of "
                        "listing more.")}]
        try:
            return await chat_json(llm, model, messages, schema, STRUCTURE,
                                   lambda d: _build(d, slot, character, ctx))
        except JSONJobFailed as e:
            last = e
    raise last


# a name field keeps its value when the real name changes: it is not text about the person
_NAME_PATHS = re.compile(rf"character_name|{NAME_FIELDS}")


def _shown(name: str, human: HumanIdentity) -> str:
    """A name as logs, exceptions and reports may print it: "<redacted>" when it shares a word
    with the owner's real name (screens.touches_real_name)."""
    return "<redacted>" if touches_real_name(name, human) else name


def _with_real_name(p: Persona, new: str, ctx: _Ctx) -> tuple[Persona, int]:
    """p under a new real name, and how many free-text fields still hold the old first name in
    another case. In its free text the old full name becomes the new full name and the old
    first name the new first name (whole words, spelled as the record has them: a lower-case
    "will" may be the ordinary word, so it is left, and counted). A note about a member whose
    name is the old name is about that member and stays as it is. The result goes through the
    screens and limits again: ValueError when it fails one."""
    old = p.identity.real_name.split()
    first = new.split()[0]
    pat = re.compile(r"(?<!\w)(?:" + r"\s+".join(map(re.escape, old)) + "|" + re.escape(old[0])
                     + r")(?!\w)")
    leftover = re.compile(rf"(?<!\w){re.escape(old[0])}(?!\w)", re.IGNORECASE)
    olds = {" ".join(old).casefold(), old[0].casefold()}
    theirs = {f"relationships[{i}].note" for i, r in enumerate(p.relationships)
              if r.other.casefold() in olds}
    left = 0

    def swap(v, path=""):
        nonlocal left
        if isinstance(v, str):
            if _NAME_PATHS.fullmatch(path) or path in theirs:
                return v
            out = pat.sub(lambda m: new if len(m.group(0).split()) > 1 else first, v)
            left += leftover.search(out) is not None
            return out
        if isinstance(v, dict):
            return {k: swap(x, f"{path}.{k}" if path else k) for k, x in v.items()}
        if isinstance(v, list):
            return [swap(x, f"{path}[{i}]") for i, x in enumerate(v)]
        return v

    data = swap(persona_to_dict(p))
    data["identity"]["real_name"] = new
    check_no_real_name(data, ctx.human)
    check_romance(data, ctx.human, ctx.screen_names, characters=ctx.members)
    renamed = persona_from_dict(data)
    errs = validate_persona(renamed)
    if errs:
        raise ValueError("; ".join(errs[:25]))
    return renamed, left


async def _rename(llm, model, rules, p: Persona, taken, is_taken: Callable[[str], bool],
                  ctx: _Ctx) -> Persona | None:
    """One cheap call (no thinking, low temperature) for candidate real names; p takes the
    first that is not taken and leaves the record passing the screens and limits. None when no
    candidate does. `taken`: the first names the prompt lists (the owner's is never among them).
    STRUCTURE rather than EXTRACT: the same settings with the persona model's num_ctx, since
    another num_ctx makes Ollama reload that model."""
    messages = [{"role": "system", "content": rules},
                {"role": "user", "content": _rename_prompt(p, taken)}]
    for name in await chat_json(llm, model, messages, RENAME_SCHEMA, STRUCTURE, _candidate_names):
        if is_taken(name):
            continue
        try:
            renamed, left = _with_real_name(p, name, ctx)
        except ValueError:
            continue
        if left:
            log.info("renamed %s: the old first name is still in %d field%s, in another case",
                     p.character_name, left, "" if left == 1 else "s")
        return renamed
    return None


async def generate_persona(llm: LLMClient, model: str, slot: GenSlot, character: Member,
                           cast_summary: str, rules: str, *, human: HumanIdentity | None = None,
                           taken_names: frozenset[str] = frozenset(),
                           member_names: frozenset[str] = frozenset(),
                           notes: GenNotes | None = None) -> Persona:
    """taken_names: every name a real name must not repeat (character names, real names, the
    owner's names), compared by full name and by first name. member_names: the character
    names a relationship may point at. A record that still collides after one informed retry
    is renamed (one call for candidate names, the first free one that passes the screens);
    NameCollision only when no candidate does."""
    if slot.age_band[0] < MIN_AGE:
        raise ValueError(f"age band {slot.age_band} includes someone under {MIN_AGE}")
    human = human or HumanIdentity(0, "", "", "", False)
    notes = notes if notes is not None else GenNotes()
    profile = await _write_profile(llm, model, rules,
                                   _profile_prompt(slot, character, cast_summary, human),
                                   character.name)
    known = frozenset(taken_names)
    aliases = human_aliases(human)
    watched = set(member_names)
    for n in known:
        watched.add(n)
        watched.update(n.split()[:1])  # the first name as written: the screen is case-sensitive
    ctx = _Ctx(human, frozenset(member_names) - {character.name},
               frozenset(n for n in watched if n.casefold() not in aliases
                         and n != character.name))
    taken_keys: set[str] = set()
    for n in known:
        taken_keys |= name_keys(n)

    def is_taken(name: str) -> bool:
        return bool(name_keys(name) & taken_keys)

    p = await _structured(llm, model, rules,
                          _record_prompt(slot, character, profile, human, ()),
                          slot, character, ctx)
    notes.dropped = list(ctx.dropped)
    if not is_taken(p.identity.real_name):
        return p
    notes.collisions.append(p.identity.real_name)
    first_names = _printable_first_names(known, human, member_names)
    # LLMError and a screen failure on the retry surface as themselves.
    again = await _structured(llm, model, rules,
                              _record_prompt(slot, character, profile, human, first_names),
                              slot, character, ctx)
    notes.dropped = list(ctx.dropped)
    old = again.identity.real_name
    if not is_taken(old):
        return again
    renamed = await _rename(llm, model, rules, again, first_names, is_taken, ctx)
    if renamed is None:
        log.warning("name collision for %s: %s; no rename candidate fit", character.name,
                    _shown(old, human))
        raise NameCollision(f"real name {_shown(old, human)!r} collides with a taken name")
    new = renamed.identity.real_name
    notes.renames.append((old, new))  # raw, like notes.collisions: reports redact
    log.warning("renamed %s: %s -> %s", character.name, _shown(old, human), _shown(new, human))
    return renamed


# -- the whole cast ---------------------------------------------------------------------------

def _redact(names: list[str], human: HumanIdentity) -> list[str]:
    return [_shown(n, human) for n in names]


async def generate_cast(store: Store, llm: LLMClient | None, cfg, *, human_guid: int,
                        data_dir: Path, limit: int | None = None, dry_run: bool = False,
                        rng: random.Random, progress: Callable[[str], None] | None = None
                        ) -> GenReport:
    """Plan from what is stored (seeds and earlier runs count), then make up to `limit`
    personas one at a time, saving each so a rerun resumes."""
    roster = list(store.cast().values())
    by_guid = {m.guid: m for m in roster}
    existing = [p for g in sorted(store.persona_versions()) if (p := load_persona(store, g))]
    slots = plan_slots(roster, existing, cfg.cast, human_guid, rng)
    report = GenReport(planned=len(slots), slots=slots)
    if dry_run:
        return report
    human = human_identity(cfg, store, human_guid)
    rules = GENERATION_RULES
    extra = load_prompt("persona_rules", data_dir, "")
    if estimate_tokens(extra) > OVERLAY_TOKEN_CAP:
        log.warning("persona_rules overlay cut to %d tokens", OVERLAY_TOKEN_CAP)
        extra = truncate_words(extra, OVERLAY_TOKEN_CAP * 4)
    if extra:
        rules = f"{rules}\n\n{extra}"
    taken = {m.name for m in roster}
    taken.update(p.identity.real_name for p in existing)
    taken.update(n for n in (human.nickname, human.real_name, human.character_name) if n)
    names = {m.name: m.guid for m in roster}
    members = frozenset(m.name for m in roster if m.guid != human_guid)
    say = progress or (lambda _s: None)
    todo = slots if limit is None else slots[:max(limit, 0)]
    for i, slot in enumerate(todo, 1):
        member = by_guid[slot.guid]
        say(f"generating {i}/{len(todo)}: {member.name}")
        notes = GenNotes()
        try:
            p = await generate_persona(llm, cfg.models.reflection, slot, member,
                                       cast_summary(existing, human=human), rules, human=human,
                                       taken_names=frozenset(taken), member_names=members,
                                       notes=notes)
            with store.transaction():
                save_persona(store, p, source="generated", ts=store.clock.now(),
                             race=RACES.get(member.race, ""),
                             class_name=CLASSES.get(member.class_, ""))
                seed_relationships(store, [p], names, human_guid, store.clock.now(),
                                   human=human)
        except Exception as e:
            report.failed[slot.guid] = f"{e.__class__.__name__}: {str(e)[:200]}"
            report.collisions.extend(_redact(notes.collisions, human))
            continue
        report.collisions.extend(_redact(notes.collisions, human))
        if notes.dropped:
            report.dropped_targets[slot.guid] = _redact(notes.dropped, human)
        existing.append(p)
        taken.add(p.identity.real_name)
        report.created.append(slot.guid)
    return report
