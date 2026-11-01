"""Grounding what the night's model writes (P1c Task 6): each claim must rest on evidence it
cites, through twelve deterministic rules (check) and then, for most kinds, the judge (a JSON
pass of the reflection model). Nothing here writes, except log_verdicts, which a stage calls
inside its unit's commit. Text that reaches a log is screened of the owner's real name first.

The rules, in order; the first that fails is the reason:
 1. the evidence is not empty and every ref is in it ("unknown evidence");
 2. 0 < length <= MAX_LEN[kind] ("length");
 3. no meta (P1b's has_meta) ("meta");
 4. not his real name, by P1b's matcher (its Title and UPPER forms; never the lower-case word,
    which may be an ordinary one: ruling I4) ("real name");
 5. every name it uses is the owner, the human or someone the cited items involve
    ("name not in evidence: <name>");
 6. `about` is someone the cited items involve, and not the owner ("about not in evidence");
 7. no romance aimed at a member, by P1b's screen with the cast's names in any case (PR6;
    rivalry over him is allowed) ("romance between members");
 8. nobody leaves the guild: talk of quitting the game is fine ("leaves the guild");
 9. outside FICTION, every number occurs in a cited item; a rumour's may move within
    NUMBER_BAND of one ("number not in evidence", "number outside the band");
10. outside FICTION, every boss or item it names is in a cited item ("boss/item not in evidence");
11. a rel_delta moves a REL_DELTA_DIMS dimension by at most 10, and never attraction, which only
    the owner's curve moves (Task 12) ("bad delta", "attraction moves only by acts");
12. a tom claim is about someone who really does not know something it cites
    ("tom not supported")."""
import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import TYPE_CHECKING

from leeroy_mind.human import HumanIdentity
from leeroy_mind.nightly.evidence import Evidence, Facts
from leeroy_mind.nightly.llm import schema, system_prompt
from leeroy_mind.nightly.store import log_grounding
from leeroy_mind.personas.screens import check_romance, has_word, names_real_name, redact_real_name
from leeroy_mind.storage.store import Store
from leeroy_mind.text import has_meta

if TYPE_CHECKING:
    from leeroy_mind.nightly.runner import NightContext


class _MaxLen(dict):
    def __missing__(self, kind: str) -> int:
        return 200  # memory text is capped at 200 by P1b's schema


MAX_LEN = _MaxLen({"story": 400, "recap": 400, "chronicle": 400, "notes": 400, "profile": 400,
                   "scene": 450, "fact": 160, "bystander": 120, "emotion": 80, "nickname": 24})
FICTION = frozenset({"life", "break", "scene", "wowlife", "bystander"})
DISTORTS = frozenset({"gossip"})  # a rumour may fuzz a number within the band, and invent nothing
NUMBER_BAND = 0.5  # from half to one and a half times a number in the evidence
PARTLY_OK = frozenset({"diary", "plan", "scene"})
INVARIANT_ONLY = frozenset({"life", "break", "wowlife"})
CHECK_ONLY = frozenset({"gossip", "emotion", "joke", "nickname", "candidate"})  # never judged
REL_DELTA_DIMS = ("affinity", "trust", "respect", "familiarity")
MAX_DELTA = 10
LEAVES_RE = re.compile(
    r"\b(g ?quit|left the guild|leav(e|es|ing) the guild|quit(s|ting)? the guild|kicked from the "
    r"guild|(joined|joining|join) (another|a new|a different|their) guild|transferr?(ed|ing) "
    r"(servers|realms|off the server))\b", re.I)
NUMBER_RE = re.compile(r"\b\d+\b")
JUDGE_BATCH = 10
JUDGE_SYSTEM = ("You check whether each claim is supported by its evidence and keeps the rules "
                "below.")
JUDGE_ASK = (
    "For each ITEM, say how far its EVIDENCE supports it: \"yes\" when the evidence supports all "
    "of it, \"partly\" when it supports some of it, \"no\" when it does not. Set invariants_ok to "
    "false when the item breaks any of the rules. Give why in a few words.")
JUDGE_SCHEMA = schema("judge", {"verdicts": {"type": "array", "items": {
    "type": "object",
    "properties": {"i": {"type": "integer", "minimum": 0},
                   "support": {"type": "string", "enum": ["yes", "partly", "no"]},
                   "invariants_ok": {"type": "boolean"},
                   "why": {"type": "string", "maxLength": 120}},
    "required": ["i", "support", "invariants_ok", "why"], "additionalProperties": False}}})


@dataclass
class Claim:
    """Something a model said, as a stage will store it: `evidence` is refs (aliases already
    mapped through Evidence.ref), `about` whom it is about, `dim` and `delta` a rel_delta's."""
    kind: str
    owner: int
    text: str
    evidence: list[str]
    about: int | None = None
    dim: str | None = None
    delta: float | None = None
    valence: float = 0.0
    extra: dict = field(default_factory=dict)


@dataclass
class Verdict:
    """`check` is "deterministic" or "judge". For a judged claim `label` is the judge's support
    (yes, partly or no), which Task 11 reads to scope what was accepted only as partly."""
    ok: bool
    check: str
    reason: str
    label: str | None = None


def screen_text(text: str, ident: HumanIdentity) -> str:
    """Text for a log, the review queue or the ledger: his real name, in the forms P1b's matcher
    takes, as "[name]"."""
    return redact_real_name(text, ident, "[name]")


@lru_cache(maxsize=16)
def _names_re(names: tuple[str, ...]) -> re.Pattern | None:
    alts = sorted((n for n in names if n), key=len, reverse=True)
    return re.compile(r"(?<!\w)(?:" + "|".join(map(re.escape, alts)) + r")(?!\w)",
                      re.I) if alts else None


def _named(text: str, names: dict[str, int]) -> list[str]:
    """The keys of `names` the text uses, in the order it first uses them."""
    pat = _names_re(tuple(sorted(names)))
    found = [] if pat is None else [m.group(0).lower() for m in pat.finditer(text)]
    return [n for n in dict.fromkeys(found) if n in names]


def _bare(name: str) -> str:
    """A boss or item name without its article: "the Lich King" is also "Lich King"."""
    return name[4:] if name.startswith("the ") and len(name) > 4 else name


@lru_cache(maxsize=8)
def _terms_re(terms: frozenset[str]) -> re.Pattern | None:
    """One pattern for every boss and item name, with and without its article (the item list
    grows with every loot, so it is not searched name by name)."""
    forms = sorted({f for t in terms for f in (t, _bare(t))}, key=len, reverse=True)
    return re.compile(r"(?<!\w)(?:" + "|".join(map(re.escape, forms)) + r")(?!\w)",
                      re.I) if forms else None


def check(claim: Claim, ev: Evidence, facts: Facts) -> Verdict:
    """The deterministic rules (the module docstring), check "deterministic"."""
    def no(reason: str) -> Verdict:
        return Verdict(False, "deterministic", reason)
    text, kind = claim.text, claim.kind
    if not claim.evidence or any(ref not in ev for ref in claim.evidence):
        return no("unknown evidence")
    if not text.strip() or len(text) > MAX_LEN[kind]:
        return no("length")
    if has_meta(text):
        return no("meta")
    if names_real_name(text, facts.ident):
        return no("real name")
    cited = [ev[ref] for ref in claim.evidence]
    actors = frozenset().union(*(item.actors for item in cited))
    for name in _named(text, facts.names):
        if facts.names[name] not in (claim.owner, facts.human) and facts.names[name] not in actors:
            return no(f"name not in evidence: {name}")
    if claim.about is not None and (claim.about not in actors or claim.about == claim.owner):
        return no("about not in evidence")
    try:
        check_romance({"sample_lines": [text]}, facts.ident, (), characters=facts.member_names)
    except ValueError:
        return no("romance between members")
    if LEAVES_RE.search(text):
        return no("leaves the guild")
    if kind not in FICTION:
        numbers = {int(n) for item in cited for n in NUMBER_RE.findall(item.text)}
        for n in (int(x) for x in NUMBER_RE.findall(text)):
            if not numbers or (kind not in DISTORTS and n not in numbers):
                return no("number not in evidence")  # a rumour's needs a number to lean on (M8)
            if kind in DISTORTS and not any(m * (1 - NUMBER_BAND) <= n <= m * (1 + NUMBER_BAND)
                                            for m in numbers):
                return no("number outside the band")
        terms = _terms_re(facts.terms)
        for m in terms.finditer(text) if terms is not None else ():
            if not any(has_word(item.text, _bare(m.group(0).lower())) for item in cited):
                return no("boss/item not in evidence")
    if kind == "rel_delta":
        if claim.dim == "attraction":
            return no("attraction moves only by acts")
        if (claim.dim not in REL_DELTA_DIMS or claim.delta is None
                or abs(claim.delta) > MAX_DELTA):
            return no("bad delta")
    if kind == "tom" and (claim.about is None
                          or all(claim.about in item.knowers for item in cited)):
        return no("tom not supported")
    return Verdict(True, "deterministic", "")


def _reason(head: str, why: str) -> str:
    return f"{head}: {why}" if why else head


def _policy(claim: Claim, v: dict | None) -> Verdict:
    """The judge's verdict on one claim. A missing verdict, or one that breaks the invariants,
    rejects; an INVARIANT_ONLY kind needs nothing more; otherwise yes accepts, partly only for
    PARTLY_OK kinds, and no rejects."""
    if v is None:
        return Verdict(False, "judge", "no verdict")
    support, why = v["support"], v["why"]
    if not v["invariants_ok"]:
        return Verdict(False, "judge", _reason("invariants", why), support)
    if claim.kind in INVARIANT_ONLY or support == "yes" or (
            support == "partly" and claim.kind in PARTLY_OK):
        return Verdict(True, "judge", why, support)
    return Verdict(False, "judge", _reason("unsupported" if support == "no" else "partly", why),
                   support)


async def judge(ctx: "NightContext", claims: list[Claim], ev: Evidence,
                facts: Facts) -> list[Verdict]:
    """The judge's verdict on each claim, in order, asked in batches of JUDGE_BATCH numbered
    from 0. Each claim is shown as "ITEM {i}: [{kind}] {text}", then EVIDENCE: and its cited
    lines. A failed request is the unit's failure (a ReflectionError)."""
    system = system_prompt("judge", JUDGE_SYSTEM, ctx.settings.data_dir, human=ctx.ident)
    out: list[Verdict] = []
    for start in range(0, len(claims), JUDGE_BATCH):
        batch = claims[start:start + JUDGE_BATCH]
        user = JUDGE_ASK + "".join(
            f"\n\nITEM {i}: [{c.kind}] {c.text}\nEVIDENCE:\n"
            + "\n".join(ev.line(ref, ctx.tz) for ref in dict.fromkeys(c.evidence))
            for i, c in enumerate(batch))
        reply = await ctx.reasoner.json(system, user, JUDGE_SCHEMA)
        by_i: dict[int, dict] = {}
        for v in reply["verdicts"]:
            by_i.setdefault(v["i"], v)  # the first verdict for an item counts
        out += [_policy(c, by_i.get(i)) for i, c in enumerate(batch)]
    return out


async def ground(ctx: "NightContext", claims: list[Claim], ev: Evidence,
                 facts: Facts) -> list[tuple[Claim, Verdict]]:
    """Each claim with its verdict, in the original order: `check` first, then the judge for
    the survivors whose kind is not CHECK_ONLY. Writes nothing."""
    verdicts = [check(c, ev, facts) for c in claims]
    judged = [i for i, (c, v) in enumerate(zip(claims, verdicts))
              if v.ok and c.kind not in CHECK_ONLY]
    if judged:
        for i, v in zip(judged, await judge(ctx, [claims[i] for i in judged], ev, facts)):
            verdicts[i] = v
    return list(zip(claims, verdicts))


def log_verdicts(store: Store, night: str, stage: str, pairs: list[tuple[Claim, Verdict]],
                 now: float, ident: HumanIdentity) -> None:
    """One grounding_log row per claim, its text and reason screened (screen_text). Sync, for
    the unit's commit."""
    for claim, v in pairs:
        log_grounding(store, night=night, stage=stage, owner=claim.owner, kind=claim.kind,
                      text=screen_text(claim.text, ident),
                      verdict="accepted" if v.ok else "rejected", check=v.check,
                      reason=screen_text(v.reason, ident), ts=now)
