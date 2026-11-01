"""Supersession (P1c Task 10): a member's newer belief replaces the older ones it contradicts and
folds into one it duplicates, so the night never deletes a belief and never stores one twice.

Task 11 takes three steps for each accepted belief and self-insight:
1. candidates: the owner's nearest active reflections on the same person (on nobody, for a
   self-insight), found by an exact search over all of them, rows under PROTECTED_KEYS left
   out, in exactly the scope the new memory will be stored with (Task 11's stored scope,
   pre-flight PR7);
2. decide: one JSON call of the reflection model, saying for each candidate whether the new
   belief contradicts it, duplicates it or is unrelated to it;
3. resolve, inside the unit's transaction: a duplicate takes the new evidence and importance
   and nothing new is stored; otherwise the new memory is stored (the caller's `insert`) and
   every row it contradicts is marked superseded by it, never deleted.

It all stays within one scope (controller ruling B2), as P1b's writer merges only within one: a
belief resting on a whisper never merges into or supersedes an open one, and an open one never
touches a private one. candidates matches the scope exactly, and decide keeps only the ids it
offered, so the model cannot reach past them. resolve checks again on its own, with what it can
see: a row it supersedes has exactly the stored scope of the new memory, and a duplicate is at
least as strict as what the claim's evidence derives, so a merge never leaves a memory weaker
than its evidence. resolve cannot see what the prompt showed, so for a duplicate the exact scope
is candidates' to keep. A decision resolve will not act on is logged and skipped.

Keyed rewrites and canon are never touched (K1): a row under PROTECTED_KEYS is rewritten by its
own stage, or is the owner's canon, so supersession neither offers it nor acts on it.

The budget (Task 5, K2): each belief or self-insight with candidates costs one more JSON call.
The count registered here gives the reflect units that may make such calls, and budget prices
each at SUPERSEDE_CALLS until the nights have measured them."""
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

from leeroy_mind.human import HumanIdentity
from leeroy_mind.logs import log
from leeroy_mind.memory.privacy import Scope, derive_scope, stricter
from leeroy_mind.nightly.budget import reflecting, register_counter
from leeroy_mind.nightly.ground import Claim
from leeroy_mind.nightly.llm import schema, system_prompt
from leeroy_mind.personas.screens import redact_real_name
from leeroy_mind.storage.rows import MemoryRow
from leeroy_mind.storage.store import Store
from leeroy_mind.text import strip_wow_markup

if TYPE_CHECKING:
    from leeroy_mind.nightly.runner import NightContext

PROMPT = "supersede"  # the owner's overlay is prompts/nightly_supersede.txt
STAGE = "supersede"  # the budget stage of the decide calls (budget.UNIT_CALLS)
RELATIONS = ("contradicts", "duplicates", "unrelated")
# Keys supersession never touches (K1): keyed rewrites (diary, theory of mind, nicknames, what
# lands, slang, feelings) and the owner's canon.
PROTECTED_KEYS = ("diary:", "tom:", "nick:", "lands:", "slang:", "emotion:", "owner:")
# The same rule in SQL, for the budget's count.
_UNPROTECTED = ("(key IS NULL OR NOT (" + " OR ".join(
    f"substr(key, 1, {len(p)}) = '{p}'" for p in PROTECTED_KEYS) + "))")
# The exact search: every active reflection of the owner in one scope with a vector, within the
# distance, nearest first (ties by id).
_NEAREST = ("SELECT id FROM (SELECT id, vec_distance_cosine(embedding, ?) AS d FROM memory "
            "WHERE owner = ? AND status = 'active' AND kind = 'reflection' AND scope = ? "
            "AND scope_with = ? AND embedding IS NOT NULL) WHERE d <= ? ORDER BY d, id")
_SUPERSEDABLE = ("SELECT DISTINCT owner FROM memory WHERE status = 'active' "
                 "AND kind = 'reflection' AND embedding IS NOT NULL AND " + _UNPROTECTED)

SUPERSEDE_SCHEMA = schema("supersede", {"decisions": {"type": "array", "items": {
    "type": "object",
    "properties": {"id": {"type": "integer"},
                   "relation": {"type": "string", "enum": list(RELATIONS)}},
    "required": ["id", "relation"], "additionalProperties": False}}})
SUPERSEDE_PROMPT = ("You keep {name}'s beliefs in order. When {name} comes to a new belief, you "
                    "say how each older belief of theirs relates to it.")
SUPERSEDE_ASK = (
    "{name}'s new belief: {text}\n\n"
    "{name}'s older beliefs, each with its id:\n{older}\n\n"
    "For each older belief, give its id and how it relates to the new one: \"contradicts\" when "
    "the new belief replaces it (both cannot be true now), \"duplicates\" when it says the same "
    "thing, \"unrelated\" otherwise.")


@dataclass
class Decision:
    """What the model said of one candidate: `relation` is one of RELATIONS."""
    memory_id: int
    relation: str


def _protected(key: str | None) -> bool:
    """A key supersession never touches (PROTECTED_KEYS, K1)."""
    return key is not None and key.startswith(PROTECTED_KEYS)


def _fits(m: MemoryRow, about: int | None, scope: tuple) -> bool:
    return (m.status == "active" and m.kind == "reflection" and not _protected(m.key)
            and (m.scope, m.scope_with) == scope
            and (about in m.actors if about is not None else not m.actors))


def candidates(store: Store, owner: int, about: int | None, vec: bytes, *, k: int = 5,
               min_sim: float = 0.6, scope: tuple[str, int] = ("open", 0)) -> list[MemoryRow]:
    """The owner's memories a new belief may contradict or duplicate, nearest first (ties by
    id), at most `k`. An exact search (sqlite-vec's vec_distance_cosine over memory.embedding)
    reads every active reflection of the owner in exactly `scope` (scope, scope_with) within
    cosine distance 1 - min_sim, so no superseded twin, other kind or other scope can crowd a
    real one out; `scope` is the one the new memory will be stored with, which Task 11 takes
    from ctx.writer.scope_of. A row under PROTECTED_KEYS is left out. With `about`, a row must
    involve that person; with None, nobody."""
    want = tuple(scope)
    with store.transaction():
        near = [r[0] for r in store.conn.execute(_NEAREST, (vec, owner, *want, 1 - min_sim))]
    rows = {m.id: m for m in store.get_memories(near)}
    return [rows[i] for i in near if i in rows and _fits(rows[i], about, want)][:k]


def _shown(text: str, ident: HumanIdentity) -> str:
    """Text for the prompt as the night's evidence shows it: no markup, and his real name as
    what the guild calls him."""
    return redact_real_name(strip_wow_markup(text), ident, ident.called)


async def decide(ctx: "NightContext", claim: Claim, cands: list[MemoryRow]) -> list[Decision]:
    """How the new belief relates to each candidate, from one JSON call that lists them by id;
    no call when there are none. A decision for an id that was not offered is dropped (logged as
    nightly_supersede_unoffered), the first decision for an id counts, and they come back in the
    candidates' order, so the first duplicate is the nearest. A failed request is the unit's
    failure (a ReflectionError), as with the judge."""
    if not cands:
        return []
    name = ctx.name(claim.owner)
    system = system_prompt(PROMPT, SUPERSEDE_PROMPT, ctx.settings.data_dir, human=ctx.ident,
                           name=name)
    older = "\n".join(f"- [{m.id}] {_shown(m.text, ctx.ident)}" for m in cands)
    user = SUPERSEDE_ASK.format(name=name, text=_shown(claim.text, ctx.ident), older=older)
    reply = await ctx.reasoner.json(system, user, SUPERSEDE_SCHEMA)
    offered = [m.id for m in cands]
    said: dict[int, str] = {}
    unoffered: list[int] = []
    for d in reply["decisions"]:
        if d["id"] in offered:
            said.setdefault(d["id"], d["relation"])
        else:
            unoffered.append(d["id"])
    if unoffered:
        log("nightly_supersede_unoffered", night=ctx.night, owner=claim.owner,
            ids=sorted(set(unoffered)))
    return [Decision(i, said[i]) for i in dict.fromkeys(offered) if i in said]


def _refused(m: MemoryRow | None, claim: Claim) -> str | None:
    """Why resolve may not touch `m` at all, or None."""
    if m is None:
        return "no such memory"
    if m.owner != claim.owner:
        return "another owner's"
    if m.status != "active":
        return "not active"
    if _protected(m.key):
        return "protected key"
    return None


def _holds(m: MemoryRow, cited: Scope) -> bool:
    """Whether `m` stays as strict as its evidence once it takes evidence that derives `cited`."""
    mine = (m.scope, m.scope_with)
    return stricter(mine, (cited.scope, cited.scope_with)) == mine


def _skipped(claim: Claim, d: Decision, why: str) -> None:
    log("nightly_supersede_skipped", owner=claim.owner, memory=d.memory_id, relation=d.relation,
        reason=why)


def resolve(store: Store, decisions: list[Decision], claim: Claim,
            insert: Callable[[], int]) -> int:
    """Act on the decisions; returns the id the belief now lives under. Sync, inside the unit's
    transaction (it joins it). With a duplicate the first one that may take the claim is merged
    (merge_memory with the claim's evidence and extra["importance"]) and `insert` is never
    called. Otherwise `insert()` stores the new memory, and each contradicted row with exactly
    its stored scope is marked superseded by it. Only the claim owner's active rows under no
    protected key are touched, and a duplicate only when it is at least as strict as what the
    claim's evidence derives (_holds), so a merge never leaves it weaker than its evidence; any
    other decision is skipped and logged (nightly_supersede_skipped)."""
    with store.transaction():
        rows = {m.id: m for m in store.get_memories({d.memory_id for d in decisions})}
        dups = [d for d in decisions if d.relation == "duplicates"]
        cited = derive_scope(store.conn, claim.owner, claim.evidence) if dups else None
        for d in dups:
            m = rows.get(d.memory_id)
            why = _refused(m, claim)
            if why is None and not _holds(m, cited):
                why = "scope"
            if why is None:
                store.merge_memory(m.id, claim.evidence, claim.extra["importance"])
                return m.id
            _skipped(claim, d, why)
        new = insert()
        made = store.get_memories([new])
        if not made:
            raise KeyError(f"no memory {new}")
        scope = (made[0].scope, made[0].scope_with)
        for d in decisions:
            if d.relation != "contradicts":
                continue
            m = rows.get(d.memory_id)
            why = _refused(m, claim)
            if why is None and m.id == new:
                why = "the new memory"
            if why is None and (m.scope, m.scope_with) != scope:
                why = "scope"
            if why is not None:
                _skipped(claim, d, why)
                continue
            store.conn.execute("UPDATE memory SET status = 'superseded', superseded_by = ? "
                               "WHERE id = ? AND status = 'active'", (new, m.id))
        return new


def _count(ctx: "NightContext") -> int:
    """The budget's count (K2): tonight's reflect units with candidates, the members who reflect
    tonight (budget.reflecting) holding a memory supersession may touch, active, a reflection,
    with a vector and under no protected key. Each such unit may make a decide call per belief
    and self-insight; budget prices them at SUPERSEDE_CALLS until they are measured."""
    members = reflecting(ctx)
    if not members:
        return 0
    with ctx.store.transaction():
        owners = {r[0] for r in ctx.store.conn.execute(_SUPERSEDABLE)}
    return len(owners & members.keys())


register_counter(STAGE, _count)
