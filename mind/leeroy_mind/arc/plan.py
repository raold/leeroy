"""The meetup plan a woman on the arc proposes. She floats it; he shapes it later. No text here
names a city, a country or his town: only the event "BlizzCon" is a proper name."""
import math
import random
import re

from leeroy_mind.personas.schema import Persona

PLAN_KINDS = ("halfway", "she_travels", "event", "neutral")
_PARTNER = re.compile(
    r"(?<!not )(?<!never )(?<!business )"
    r"\b(husband|wife|partner|boyfriend|girlfriend|fianc[eé]e?|married)\b", re.IGNORECASE)
# Past or non-partners, removed before the scan ("ex-husband", "divorced", "widowed").
_NOT_PARTNER = re.compile(r"\b(?:ex-\w+|divorced|widowed)\b", re.IGNORECASE)
_COVERS = ("a work trip", "a weekend away with friends", "a course she has to go on",
           "a visit to an old friend")
_WORRIES = ("money", "time off", "the home front")
_THINGS = {"halfway": "meeting halfway", "event": "BlizzCon",
           "neutral": "a quiet weekend somewhere neutral"}


def has_partner(p: Persona) -> bool:
    return _PARTNER.search(_NOT_PARTNER.sub("", p.real_life.household)) is not None


def _kinds(region: str) -> list[str]:
    return [k for k in PLAN_KINDS if not (region == "UK" and k == "halfway")]


def propose_plan(p: Persona, rng: random.Random, *, now: float, exclude: str = "") -> dict:
    """What she floats: a kind (never `exclude`, and never halfway for a UK woman), a target
    10 to 18 days out, what worries her (from her money, job and household) and, when she has
    a partner at home, a cover story."""
    kinds = [k for k in _kinds(p.identity.region) if k != exclude] or _kinds(p.identity.region)
    kind = rng.choice(kinds)
    target = now + rng.randint(10, 18) * 86400
    rl = p.real_life
    pool = [w for w, text in zip(_WORRIES, (rl.money, rl.job, "x" if has_partner(p) else ""))
            if text.strip()]
    worries = rng.sample(pool, min(len(pool), rng.randint(1, 2)))
    cover = rng.choice(_COVERS) if has_partner(p) else ""
    return {"kind": kind, "target": target, "worries": worries, "cover": cover}


def _thing(kind: str, region: str) -> str:
    if kind == "she_travels":
        return "a flight over" if region == "UK" else "a trip out there"
    return _THINGS[kind]


def plan_lines(plan: dict, state: str, *, now: float, forms, region: str) -> list[str]:
    """What the plan means to her tonight, at most two sentences, addressed to her. `forms` are
    his pronoun forms (subject, object, possessive)."""
    thing = _thing(plan["kind"], region)
    cover = plan.get("cover") or ""
    if state == "hoped":
        lines = [f"{thing[0].upper()}{thing[1:]} is a hope you've floated; "
                 f"you leave it with {forms[1]}."]
    elif state == "talked":
        lines = [f"You two have talked about {thing}, but nothing is settled."]
    elif state == "set":
        days = math.ceil((plan["target"] - now) / 86400)
        when = ("any weekend now" if days <= 0
                else f"about {days} day{'' if days == 1 else 's'} off")
        on_mind = ", ".join([*plan.get("worries", []), "nerves", "what you'll wear"])
        second = f"On your mind: {on_mind}"
        second += f"; the cover story is {cover}." if cover else "."
        return [f"It's happening: {thing}, {when}.", second]
    else:
        return []
    if cover:
        lines.append(f"If it comes to it, the cover story is {cover}.")
    return lines


def plan_summary(plan: dict, region: str) -> str:
    """One line for the night prompts."""
    line = f"Plan on the table: {_thing(plan['kind'], region)}."
    return f"{line} Cover story: {plan['cover']}." if plan.get("cover") else line


def plan_planned(plan: dict, region: str) -> str:
    """One line for a plan that has happened (the weekend writer), without its cover story."""
    return f"What they had planned: {_thing(plan['kind'], region)}."
