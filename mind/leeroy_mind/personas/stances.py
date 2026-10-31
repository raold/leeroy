"""The women's romance attitudes toward the human (owner, 2026-10-30): 20% lust, 30% drawn,
40% fond and 10% none, counted over every woman in the cast, seeds included.

`slot_stances` fills new slots toward that mix and `plan_rebalance` moves stored personas to
it. Lust is chosen across every woman, seeds included: only women whose style or traits are
bold, theatrical, competitive or outgoing, never the shy or guarded and never a grandmother
type (none_fit), ranked by how far along she already is (drawn, then fond, then none), then a
bold, theatrical or competitive style, then extraversion. Drawn, fond and none are then
refilled with the fewest changes, and a grandmother type leaves none only when nobody else
can. The owner's own choices (`fixed`, the CLI's --set) come first and never move. Men are
never rebalanced."""
import random
import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import NamedTuple

from ..human import HumanIdentity
from ..storage.rows import Relationship
from .schema import Persona
from .screens import has_word
from .seed import human_seed, kind_of, seed_values

STANCE_SHARES = {"lust": 0.20, "drawn": 0.30, "fond": 0.40, "none": 0.10}
FILL_ORDER = ("lust", "drawn", "fond", "none")
REFILL_ORDER = ("drawn", "fond", "none")
LUST_RANK = {"lust": 0, "drawn": 1, "fond": 2, "none": 3}  # how far along she already is
LUST_STYLES = frozenset({"bold", "theatrical", "competitive"})
NOT_LUST_STYLES = frozenset({"shy", "guarded"})
OUTGOING = 6  # extraversion; generation makes outgoing people 6 or more
NONE_FIT_AGE = 50
# Grandchildren only: a young woman who lives with her nan or inherited her grandmother's shop
# is a granddaughter, not a grandmother. Age catches the rest.
_GRANDCHILD = re.compile(
    r"\bgrand[- ]?(?:kids?|child(?:ren)?|sons?|daughters?|bab(?:y|ies))\b", re.IGNORECASE)
_WRONG = {"he": ("she", "her", "hers", "herself"), "she": ("he", "him", "his", "himself")}


def stance_targets(n: int) -> dict[str, int]:
    """How many of n women hold each stance: floors of the shares, then the remainder to the
    largest fractions (ties in FILL_ORDER), so the counts always add up to n."""
    exact = {s: STANCE_SHARES[s] * n for s in FILL_ORDER}
    out = {s: int(exact[s]) for s in FILL_ORDER}
    for s in sorted(FILL_ORDER, key=lambda s: (-(exact[s] - out[s]), FILL_ORDER.index(s))
                    )[:n - sum(out.values())]:
        out[s] += 1
    return out


def slot_stances(existing: Sequence[str], n_slots: int) -> list[str]:
    """Stances for n_slots new women, given the stances the cast's women already hold, so the
    whole cast lands on the mix. A stance the seeds already overfill gets no slots, and the
    shortfall that leaves is shared in proportion to the shares."""
    targets = stance_targets(len(existing) + n_slots)
    have = Counter(existing)
    out = []
    for _ in range(n_slots):
        s = max(FILL_ORDER, key=lambda s: ((targets[s] - have[s]) / STANCE_SHARES[s],
                                           -FILL_ORDER.index(s)))
        have[s] += 1
        out.append(s)
    return out


def is_woman(p: Persona) -> bool:
    return p.identity.player_gender == "female"


def lust_fit(p: Persona) -> bool:
    style = p.romance.style
    if style in NOT_LUST_STYLES:
        return False
    return style in LUST_STYLES or p.big_five.extraversion.score >= OUTGOING


def none_fit_reason(p: Persona) -> str | None:
    """Why she is the grandmother type ("age 52", "grandchildren" or both), or None."""
    life = " ".join((p.real_life.household, p.real_life.job, p.real_life.life_thread))
    why = ([f"age {p.identity.age}"] if p.identity.age >= NONE_FIT_AGE else []) + \
        (["grandchildren"] if _GRANDCHILD.search(life) else [])
    return ", ".join(why) or None


def none_fit(p: Persona) -> bool:
    """The grandmother type: 50 or older, or grandchildren in her life."""
    return none_fit_reason(p) is not None


def _fits(p: Persona, stance: str) -> bool:
    if stance == "none":
        return none_fit(p)
    if stance == "lust":
        return lust_fit(p)
    if stance == "drawn":
        return p.romance.style is not None and not none_fit(p)
    return True


def _pref(p: Persona, stance: str) -> tuple:
    ext = p.big_five.extraversion.score
    if stance == "none":
        return (not none_fit(p), ext)
    return (none_fit(p), -ext)


def misgenders(text: str, human: HumanIdentity) -> bool:
    """A line that names the human (nickname, character name or GM, in any case) together
    with a pronoun that is not theirs. Unset pronouns read as they, so nothing misgenders."""
    wrong = _WRONG.get(human.forms[0], ())
    if not wrong:
        return False
    named = any(has_word(text, n) for n in (human.nickname, human.character_name, "GM") if n)
    return named and any(has_word(text, w) for w in wrong)


def parse_overrides(values: Iterable[str], personas: Sequence[Persona]) -> dict[int, str]:
    """The CLI's --set GUID=STANCE values, checked: each guid a woman in `personas`, each
    stance a real one, each guid once. Raises ValueError naming the bad value."""
    women = {p.guid for p in personas if is_woman(p)}
    out: dict[int, str] = {}
    for v in values:
        guid, sep, stance = (x.strip() for x in v.partition("="))
        if not sep or not guid.isdigit() or not stance:
            raise ValueError(f"--set {v}: expected GUID=STANCE")
        if stance not in STANCE_SHARES:
            raise ValueError(f"--set {v}: stance must be one of {', '.join(FILL_ORDER)}")
        if int(guid) not in women:
            raise ValueError(f"--set {v}: {guid} is not a woman in the cast")
        if int(guid) in out:
            raise ValueError(f"--set {v}: {guid} is set twice")
        out[int(guid)] = stance
    return out


@dataclass
class RebalancePlan:
    changes: list[tuple[int, str, str]] = field(default_factory=list)  # (guid, old, new)
    lust: list[int] = field(default_factory=list)  # who ends lust: the owner's, then by rank
    fixed: dict[int, str] = field(default_factory=dict)  # the owner's own choices
    shortfall: dict[str, int] = field(default_factory=dict)
    dropped: dict[int, tuple[str, ...]] = field(default_factory=dict)  # guid -> lines dropped
    _keep: dict[int, tuple] = field(default_factory=dict, repr=False)

    def updated(self, personas: Sequence[Persona]) -> list[Persona]:
        """The personas this plan changes, with only romance.stance and sample_lines new."""
        new = {g: s for g, _, s in self.changes}
        out = []
        for p in personas:
            if p.guid not in new and p.guid not in self._keep:
                continue
            q = p
            if p.guid in new:
                q = replace(q, romance=replace(q.romance, stance=new[p.guid]))
            if p.guid in self._keep:
                q = replace(q, sample_lines=self._keep[p.guid])
            out.append(q)
        return out


def plan_rebalance(personas: Sequence[Persona], rng: random.Random, *,
                   human: HumanIdentity | None = None,
                   fixed: Mapping[int, str] | None = None) -> RebalancePlan:
    """Move the women to the mix.

    1. `fixed` (the owner's own choices) is applied first, and those women never move.
    2. Lust takes the best-ranked candidates across all women: lust_fit, not none_fit, not
       fixed, ranked by LUST_RANK of her current stance, then a LUST_STYLES style, then
       extraversion, then the seeded tie-break. What it still lacks is the shortfall. A woman
       already lust who is not chosen must move.
    3. Drawn, fond and none, in that order, are refilled with the fewest changes: only from a
       stance over its target (those who must move first), a grandmother type in none last,
       then women who don't fit where they are before those who do, then the most outgoing
       (for none: the grandmother type, then the least outgoing).

    With `human`, every persona also loses the sample lines that misgender them, except the
    owner's own approved lines."""
    fixed = dict(fixed or {})
    for guid, stance in fixed.items():
        if stance not in STANCE_SHARES:
            raise ValueError(f"stance must be one of {', '.join(FILL_ORDER)}, not {stance!r}")
        if guid not in {p.guid for p in personas if is_woman(p)}:
            raise ValueError(f"{guid} is not a woman in the cast")
    plan = RebalancePlan(fixed=fixed)
    women = sorted((p for p in personas if is_woman(p)), key=lambda p: p.guid)
    tie = {p.guid: rng.random() for p in women}
    targets = stance_targets(len(women))
    cur = {p.guid: p.romance.stance for p in women}
    cur.update(fixed)

    owner_lust = [g for g in sorted(fixed) if fixed[g] == "lust"]
    ranked = sorted((p for p in women if p.guid not in fixed and lust_fit(p) and not none_fit(p)),
                    key=lambda p: (LUST_RANK[p.romance.stance], p.romance.style not in LUST_STYLES,
                                   -p.big_five.extraversion.score, tie[p.guid]))
    need = max(0, targets["lust"] - len(owner_lust))
    picks = [p.guid for p in ranked[:need]]
    if len(picks) < need:
        plan.shortfall["lust"] = need - len(picks)
    plan.lust = owner_lust + picks
    settled = set(fixed) | set(picks)
    for g in picks:
        cur[g] = "lust"
    must_move = {p.guid for p in women if p.guid not in settled and cur[p.guid] == "lust"}

    def count(s: str) -> int:
        return sum(v == s for v in cur.values())

    def surplus(g: int) -> bool:
        return g in must_move or count(cur[g]) > targets[cur[g]]

    movable = [p for p in women if p.guid not in settled]
    for stance in REFILL_ORDER:
        while count(stance) < targets[stance]:
            pool = [p for p in movable if cur[p.guid] != stance and surplus(p.guid)]
            if not pool:
                plan.shortfall[stance] = targets[stance] - count(stance)
                break
            best = min(pool, key=lambda p: (p.guid not in must_move,
                                            cur[p.guid] == "none" and none_fit(p),
                                            _fits(p, cur[p.guid]), *_pref(p, stance),
                                            tie[p.guid]))
            cur[best.guid] = stance
            must_move.discard(best.guid)
    for g in sorted(must_move):  # nobody is short any more: she goes where she is least over
        cur[g] = max(REFILL_ORDER, key=lambda s: ((targets[s] - count(s)) / STANCE_SHARES[s],
                                                  -REFILL_ORDER.index(s)))
    plan.changes = [(p.guid, p.romance.stance, cur[p.guid]) for p in women
                    if cur[p.guid] != p.romance.stance]
    if human is not None:
        for p in sorted(personas, key=lambda p: p.guid):
            gone = tuple(s.text for s in p.sample_lines
                         if not s.approved and misgenders(s.text, human))
            if gone:
                plan.dropped[p.guid] = gone
                plan._keep[p.guid] = tuple(s for s in p.sample_lines
                                           if s.approved or not misgenders(s.text, human))
    return plan


class Reseed(NamedTuple):
    guid: int
    old: str
    new: str
    row: Relationship  # what the row becomes


def _reseed(store, guid: int, old: str, new: str, human_guid: int) -> Reseed | None:
    row = store.get_relationship(guid, human_guid)
    kind = kind_of(row)  # a declared friend or friction keeps that part, and its note
    if row.interactions != 0 or seed_values(row) != seed_values(
            human_seed(guid, human_guid, old, kind, row.notes)):
        return None
    return Reseed(guid, old, new, replace(human_seed(guid, human_guid, new, kind, row.notes),
                                          last_seen=row.last_seen))


def plan_reseeds(store, changes: Iterable[tuple[int, str, str]], human_guid: int
                 ) -> list[Reseed]:
    """For each stance change, the woman's row toward the human when it was never lived in
    (interactions 0) and still holds exactly the old stance's seed (with what she declares
    toward him: seed.human_seed): it becomes the new stance's seed with the same
    declaration, keeping last_seen. A lived, nudged or missing row is left alone."""
    return [r for guid, old, new in changes
            if (r := _reseed(store, guid, old, new, human_guid)) is not None]


def apply_reseeds(store, planned: Iterable[Reseed], human_guid: int
                  ) -> tuple[list[Reseed], list[Reseed]]:
    """Write the planned re-seeds in one transaction (joining the caller's), re-reading and
    re-checking each row first: a row that was lived in or changed since the plan is skipped.
    Returns (written, skipped)."""
    written, skipped = [], []
    with store.transaction():
        for r in planned:
            fresh = _reseed(store, r.guid, r.old, r.new, human_guid)
            if fresh is None:
                skipped.append(r)
                continue
            store.put_relationship(fresh.row)
            written.append(fresh)
    return written, skipped
