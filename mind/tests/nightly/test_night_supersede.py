"""Supersession (P1c Task 10): a member's newer belief replaces the older ones it contradicts,
which stay in mind.db marked superseded, and folds into one it duplicates instead of being stored
twice. Both happen only within one scope (controller ruling B2; pre-flight PR7): a belief that
rests on a whisper never merges into or supersedes an open one, and an open one never touches a
private one. Fakes only: a FakeLLM answers for the reflection model. Every name and line here is
invented."""
import pytest

from conftest import make_mem, run
from leeroy_mind.nightly import budget
from leeroy_mind.nightly.ground import Claim
from leeroy_mind.nightly.llm import INVARIANTS, ReflectionError, check_schema
from leeroy_mind.nightly.store import run_begin, set_run_plan, stage_record
from leeroy_mind.nightly.supersede import SUPERSEDE_SCHEMA, Decision, candidates, decide, resolve
from leeroy_mind.storage.rows import pack_vec
from leeroy_mind.testing.fake_llm import fake_embed


def test_contradiction_supersedes_and_keeps_old_row(h):
    old = h.mem(101, "Belra is cold.", kind="reflection", source="reflection", actors=(102,), evidence=("memory:1",))
    claim = Claim("belief", 101, "Belra is warmer than I thought.", ["memory:2"], about=102, extra={"importance": 5})
    ctx = h.ctx({"supersede": [{"decisions": [{"id": old, "relation": "contradicts"}]}]})
    ds = run(decide(ctx, claim, h.store.get_memories([old])))
    with h.store.transaction():
        new = resolve(h.store, ds, claim, lambda: h.mem(101, claim.text, kind="reflection", source="reflection", actors=(102,), evidence=("memory:2",)))
    o = h.store.get_memories([old])[0]
    assert (o.status, o.superseded_by) == ("superseded", new)


def test_duplicate_merges_without_insert(h):
    old = h.mem(101, "Belra is solid.", kind="reflection", source="reflection", importance=5, actors=(102,), evidence=("memory:1",))
    claim = Claim("belief", 101, "Belra is reliable.", ["memory:7"], about=102, extra={"importance": 7})
    with h.store.transaction():
        got = resolve(h.store, [Decision(old, "duplicates")], claim, lambda: pytest.fail("inserted"))
    m = h.store.get_memories([old])[0]
    assert got == old and m.evidence == ("memory:1", "memory:7") and m.importance == 7


def test_no_candidates_no_llm_call(h):
    ctx = h.ctx()
    assert run(decide(ctx, Claim("belief", 101, "x", ["memory:1"], about=102), [])) == [] and ctx.reasoner.llm.calls == []


def test_candidates_filter_kind_about_and_diary(h):
    keep = h.mem(101, "Belra heals well.", kind="reflection", source="reflection", actors=(102,))
    h.mem(101, "Belra heals well.", kind="observation", actors=(102,))
    h.mem(101, "Belra heals well.", kind="reflection", source="reflection", actors=(103,))
    h.mem(101, "Belra heals well.", kind="reflection", source="reflection", key="diary:2026-10-28")
    hers = h.mem(101, "Belra heals well.", kind="reflection", source="reflection", actors=(102,), scope="private", scope_with=1)
    vec = pack_vec(fake_embed("Belra heals well."))
    assert [m.id for m in candidates(h.store, 101, 102, vec)] == [keep]                          # open asks for open only
    assert [m.id for m in candidates(h.store, 101, 102, vec, scope=("private", 1))] == [hers]   # never across scopes (B2)


# -- beyond the brief's four: the scope both ways, and what each step keeps to ------------------

def belief(h, owner: int, text: str, **kw) -> int:
    """A stored belief of `owner`, as Task 11 stores one: kind and source reflection."""
    return h.mem(owner, text, kind="reflection", source="reflection", **kw)


def vec(text: str) -> bytes:
    return pack_vec(fake_embed(text))


def chats(ctx):
    """The ctx's chat calls (the embedder's calls have no messages)."""
    return [c for c in ctx.reasoner.llm.calls if "messages" in c]


def put_belief(ctx, claim: Claim, *, shown=()):
    """resolve's `insert` as Task 11 passes it: the new belief through ctx.writer.put, with what
    its prompt showed when that counts for its scope (the backstop, PR7)."""
    def insert() -> int:
        row = make_mem(owner=claim.owner, kind="reflection", source="reflection", text=claim.text,
                       actors=() if claim.about is None else (claim.about,),
                       importance=claim.extra["importance"], evidence=tuple(claim.evidence),
                       embedding=vec(claim.text))
        return ctx.writer.put(row, shown=shown)
    return insert


def test_schema_takes_an_id_and_one_of_three_relations():
    assert SUPERSEDE_SCHEMA["title"] == "supersede"
    check_schema(SUPERSEDE_SCHEMA, {"decisions": [{"id": 3, "relation": "contradicts"}, {"id": 4, "relation": "duplicates"},
                                                  {"id": 5, "relation": "unrelated"}]})
    for bad in ({"decisions": [{"id": 3, "relation": "replaces"}]}, {"decisions": [{"id": "3", "relation": "duplicates"}]},
                {"decisions": [{"id": 3}]}, {}):
        with pytest.raises(ValueError):
            check_schema(SUPERSEDE_SCHEMA, bad)


def test_candidates_are_close_active_and_nearest_first(h):
    farther = belief(h, 101, "Belra heals really well in raids.", actors=(102,))   # similarity 0.71
    near = belief(h, 101, "Belra heals well tonight.", actors=(102,))              # 0.87
    exact = belief(h, 101, "Belra heals well.", actors=(102,))
    belief(h, 101, "Belra tanks badly in dungeons.", actors=(102,))                # 0.26: too far
    belief(h, 101, "Belra heals well.", actors=(102,), status="superseded")
    belief(h, 102, "Belra heals well.", actors=(102,))                             # Belra's own, not Arkon's
    v = vec("Belra heals well.")
    assert [m.id for m in candidates(h.store, 101, 102, v)] == [exact, near, farther]
    assert [m.id for m in candidates(h.store, 101, 102, v, k=2)] == [exact, near]
    assert [m.id for m in candidates(h.store, 101, 102, v, min_sim=0.8)] == [exact, near]


def test_candidates_pin_the_diary_and_the_actor_rules(h):
    """A diary entry is never a candidate, whoever it involves. With no `about` (a self-insight)
    only memories that involve nobody are; with one, those that involve that person."""
    alone = belief(h, 101, "I heal well under pressure.")
    belra = belief(h, 101, "I heal well under pressure.", actors=(102,))
    both = belief(h, 101, "I heal well under pressure.", actors=(102, 103))
    belief(h, 101, "I heal well under pressure.", key="diary:2026-10-28")
    belief(h, 101, "I heal well under pressure.", actors=(102,), key="diary:2026-10-27")
    v = vec("I heal well under pressure.")
    assert [m.id for m in candidates(h.store, 101, None, v)] == [alone]
    assert [m.id for m in candidates(h.store, 101, 102, v)] == [belra, both]
    assert [m.id for m in candidates(h.store, 101, 103, v)] == [both]


def test_candidates_match_the_scope_exactly(h):
    """The class and the partner both: open, private with Testy, private with Coros and secret
    with Testy are four scopes, and each asks for its own; private with nobody finds none (B2)."""
    scopes = [("open", 0), ("private", 1), ("private", 103), ("secret", 1)]
    ids = {s: belief(h, 101, "Belra heals well.", actors=(102,), scope=s[0], scope_with=s[1]) for s in scopes}
    v = vec("Belra heals well.")
    for s in scopes:
        assert [m.id for m in candidates(h.store, 101, 102, v, scope=s)] == [ids[s]]
    assert candidates(h.store, 101, 102, v, scope=("private", 0)) == []


def test_decide_lists_the_candidates_and_keeps_only_what_it_offered(h):
    """One JSON call that names each candidate by its id, never by his real name. A decision for
    an id it did not offer (a private memory, or none at all) is dropped, the first one for an
    id counts, and they come back in the candidates' order, nearest first."""
    near = belief(h, 101, "Belra heals well.", actors=(102,))
    named = belief(h, 101, "Quillon says Belra heals well tonight.", actors=(102,))
    hers = belief(h, 101, "Belra heals well.", actors=(102,), scope="private", scope_with=1)
    claim = Claim("belief", 101, "Belra heals better than anyone.", [f"memory:{near}"], about=102, extra={"importance": 5})
    ctx = h.ctx({"supersede": [{"decisions": [
        {"id": named, "relation": "contradicts"}, {"id": hers, "relation": "duplicates"}, {"id": 999, "relation": "duplicates"},
        {"id": near, "relation": "duplicates"}, {"id": named, "relation": "unrelated"}]}]})
    assert run(decide(ctx, claim, h.store.get_memories([near, named]))) == [Decision(near, "duplicates"), Decision(named, "contradicts")]
    (call,) = chats(ctx)
    system, user = call["messages"][0]["content"], call["messages"][-1]["content"]
    assert claim.text in user and f"[{near}] Belra heals well." in user and f"[{hers}]" not in user
    assert f"[{named}] Testy says Belra heals well tonight." in user and "quillon" not in (system + user).casefold()
    assert system.endswith(INVARIANTS.format(human="Testy"))


def test_decide_fails_the_unit_on_a_bad_reply_twice(h):
    """Like the judge's: two replies that miss the schema are the unit's failure, never a silent
    'no decisions'."""
    old = belief(h, 101, "Belra is cold.", actors=(102,))
    ctx = h.ctx({"supersede": [{"decisions": [{"id": old, "relation": "maybe"}]}] * 2})
    with pytest.raises(ReflectionError) as e:
        run(decide(ctx, Claim("belief", 101, "Belra is warm.", ["memory:1"], about=102), h.store.get_memories([old])))
    assert not e.value.fatal and len(chats(ctx)) == 2


def test_resolve_supersedes_every_contradicted_row_or_merges_into_the_first_duplicate(h):
    """Every contradicted row is superseded by the new one and kept; an unrelated one is left
    alone. With a duplicate nothing new is stored: the first duplicate takes the evidence and
    nothing is superseded."""
    cold = belief(h, 101, "Belra is cold.", actors=(102,), evidence=("memory:1",))
    aloof = belief(h, 101, "Belra keeps to herself.", actors=(102,), evidence=("memory:1",))
    druid = belief(h, 101, "Belra plays a druid.", actors=(102,), evidence=("memory:1",))
    claim = Claim("belief", 101, "Belra is warm once you know her.", ["memory:2"], about=102, extra={"importance": 6})
    with h.store.transaction():
        new = resolve(h.store, [Decision(cold, "contradicts"), Decision(druid, "unrelated"), Decision(aloof, "contradicts")],
                      claim, lambda: belief(h, 101, claim.text, actors=(102,), evidence=("memory:2",)))
    assert {m.id: (m.status, m.superseded_by) for m in h.rows(101)} == {
        cold: ("superseded", new), aloof: ("superseded", new), druid: ("active", None), new: ("active", None)}
    first = belief(h, 101, "Belra is warm.", actors=(102,), evidence=("memory:3",))
    second = belief(h, 101, "Belra is friendly.", actors=(102,), evidence=("memory:3",))
    with h.store.transaction():
        got = resolve(h.store, [Decision(new, "contradicts"), Decision(first, "duplicates"), Decision(second, "duplicates")],
                      claim, lambda: pytest.fail("inserted"))
    rows = {m.id: m for m in h.rows(101)}
    assert got == first and (rows[first].evidence, rows[first].merged_n) == (("memory:3", "memory:2"), 2)
    assert rows[second].merged_n == 1 and rows[new].status == "active"


def test_resolve_never_supersedes_the_row_it_stored(h):
    """A keyed `insert` may land on a row it was told to supersede (under a key supersession may
    touch, none of K1's): that row is the new memory, rewritten, and it stays active."""
    hours = belief(h, 101, "Testy played all night again.", actors=(1,), key="hours:2026-10-29")
    claim = Claim("belief", 101, "Testy took a night off.", ["event:1"], about=1, extra={"importance": 5})
    ctx = h.ctx()
    row = make_mem(owner=101, kind="reflection", source="reflection", text=claim.text, actors=(1,), key="hours:2026-10-29")
    with h.store.transaction():
        new = resolve(h.store, [Decision(hours, "contradicts")], claim, lambda: ctx.writer.put(row))
    m = h.store.get_memories([hours])[0]
    assert new == hours and (m.status, m.superseded_by, m.text) == ("active", None, claim.text)


# -- fix round 1: protected keys (K1), the exact search (concern 3), the budget (K2) -------------

PROTECTED = ("diary:", "tom:", "nick:", "lands:", "slang:", "emotion:", "owner:")   # K1, the controller's list


@pytest.mark.parametrize("prefix", PROTECTED)
def test_a_protected_key_is_never_offered_superseded_or_merged_into(h, prefix):
    """K1: keyed rewrites and canon stay out of supersession. candidates never offers such a
    row, and resolve, handed a duplicate and a contradiction about it anyway, leaves it alone."""
    kept = belief(h, 101, "Belra heals well.", actors=(102,), key=f"{prefix}102")
    plain = belief(h, 101, "Belra heals well.", actors=(102,))
    assert [m.id for m in candidates(h.store, 101, 102, vec("Belra heals well."))] == [plain]
    claim = Claim("belief", 101, "Belra heals well in raids.", ["memory:9"], about=102, extra={"importance": 9})
    with h.store.transaction():
        new = resolve(h.store, [Decision(kept, "duplicates"), Decision(kept, "contradicts")], claim,
                      lambda: belief(h, 101, claim.text, actors=(102,)))
    k = h.store.get_memories([kept])[0]
    assert new not in (kept, plain)
    assert (k.status, k.superseded_by, k.merged_n, k.evidence, k.importance) == ("active", None, 1, ("event:1",), 5)


def test_candidates_find_a_real_one_behind_25_closer_rows_that_cannot_be(h):
    """Concern 3: the search is exact over the owner's active reflections, so 25 superseded
    twins nearer the query than the real candidate no longer hide it, as they did from the
    owner's 20 nearest."""
    v = vec("Belra heals well.")
    for _ in range(25):
        belief(h, 101, "Belra heals well.", actors=(102,), status="superseded")
    real = belief(h, 101, "Belra heals well tonight.", actors=(102,))                  # similarity 0.87
    assert real not in [i for i, _ in h.store.knn(101, v, k=20)]                     # where the old pre-filter stopped
    assert [m.id for m in candidates(h.store, 101, 102, v)] == [real]


def test_the_exact_search_stays_inside_the_owner_and_the_active_status(h):
    """Only the owner's active reflections with a vector are read: another owner's twin, the
    owner's archived, rejected and superseded twins and one with no vector are never offered."""
    mine = belief(h, 101, "Belra heals well.", actors=(102,))
    hers = belief(h, 102, "Belra heals well.", actors=(102,))
    belief(h, 103, "Belra heals well.", actors=(102,))
    for status in ("archived", "rejected", "superseded"):
        belief(h, 101, "Belra heals well.", actors=(102,), status=status)
    belief(h, 101, "Belra heals well.", actors=(102,), embedding=None)
    v = vec("Belra heals well.")
    assert [m.id for m in candidates(h.store, 101, 102, v)] == [mine]
    assert [m.id for m in candidates(h.store, 102, 102, v)] == [hers]


def test_a_reflect_unit_is_priced_with_its_decide_calls():
    """K2: a reflect unit without candidates costs the stage's own 4 calls; one with candidates
    adds supersession's decide calls (budget stage "supersede", run and measured inside
    "reflect"): SUPERSEDE_CALLS of them at planning, the measured count once there is one."""
    assert budget.unit_calls("reflect") == 4 and budget.unit_calls("supersede") == budget.SUPERSEDE_CALLS == 2
    assert budget.MEASURED_AS["supersede"] == "reflect"
    assert budget.estimate({"reflect": 1}, measured={}) == {"reflect": 150}                    # without candidates
    assert budget.estimate({"reflect": 1, "supersede": 1}, measured={}) == {"reflect": 150, "supersede": 40}
    measured = {"reflect": 10.0, "supersede": 10.0}                                          # 10 s a call
    assert budget.estimate({"reflect": 1, "supersede": 1}, measured=measured) == {"reflect": 40, "supersede": 20}
    assert budget.estimate({"reflect": 1, "supersede": 1}, measured=measured,
                           calls={"supersede": 3.5}) == {"reflect": 40, "supersede": 35}     # the measured count
    assert budget.estimate({"supersede": 1}, measured={}, calls={"supersede": 3.5}) == {"supersede": 70}


def test_the_budget_counts_the_reflect_units_with_candidates(h):
    """The count supersede.py registers: the members who reflect tonight (3 new memories or more)
    holding at least one memory supersession may touch. Protected keys only, a reflection with no
    vector or one already superseded make no decide call; a member who does not reflect tonight
    is no unit at all."""
    old = h.T - 3 * 86400                                                                  # not new tonight
    for g in (101, 102, 104, 105):
        for i in range(3):
            h.mem(g, f"Tonight's raid, take {i}.")
    for i in range(2):
        h.mem(103, f"Tonight's raid, take {i}.")                                          # 103 does not reflect
    belief(h, 101, "Belra heals well.", actors=(102,), t=old)
    for p in PROTECTED:
        belief(h, 102, "Belra heals well.", actors=(103,), key=f"{p}x", t=old)
    belief(h, 103, "Belra heals well.", actors=(102,), t=old)
    belief(h, 104, "Belra heals well.", actors=(102,), embedding=None, t=old)
    belief(h, 105, "Belra heals well.", actors=(102,), status="superseded", t=old)
    assert budget.COUNTERS["supersede"](h.ctx()) == 1                                      # 101 alone


def test_the_plan_keeps_its_units_and_measures_the_decide_calls(h, monkeypatch):
    """The plan records the units it counted; after three nights with them, a unit with
    candidates is priced at the measured count: the reflect stage's calls less its units' own
    (4 each, 3 without the questions), over the units with candidates. Nights planned without
    units still give the price of a call, and leave the count at SUPERSEDE_CALLS."""
    monkeypatch.setattr(budget, "COUNTERS", {"reflect": lambda ctx: 5, "supersede": lambda ctx: 4})
    plan = run(budget.plan_night(h.ctx()))
    assert plan["units"] == {"reflect": 5, "supersede": 4}
    assert plan["estimate_s"] == pytest.approx(5 * 150 + 4 * 40)                         # planning costs
    for night in ("2026-10-20", "2026-10-21", "2026-10-22"):                              # planned before units
        run_begin(h.store, night, 0, 1, now=0)
        stage_record(h.store, night, "reflect", started=0, finished=1, calls=20, call_s=100.0)   # 5 s a call
    assert budget.measured_supersede_calls(h.store) is None
    assert run(budget.plan_night(h.ctx()))["estimate_s"] == pytest.approx(5 * 4 * 5 + 4 * 2 * 5)
    for night, decides in (("2026-10-25", 2), ("2026-10-26", 5), ("2026-10-27", 3)):
        run_begin(h.store, night, 0, 1, now=0)
        set_run_plan(h.store, night, {"units": {"reflect": 5, "reflect_small": 1, "supersede": 4}})
        calls = 5 * 4 + 1 * 3 + 4 * decides
        stage_record(h.store, night, "reflect", started=0, finished=1, calls=calls, call_s=5.0 * calls)
    assert budget.measured_supersede_calls(h.store) == 3                                  # the median of 2, 5 and 3
    assert run(budget.plan_night(h.ctx()))["estimate_s"] == pytest.approx(5 * 4 * 5 + 4 * 3 * 5)


def test_a_belief_resting_on_a_whisper_never_touches_an_open_one(h):
    """PR7, one way. Task 11 stores a backstopped belief with what its prompt showed, so it is
    private with Testy though it cites only an open memory, and asks for candidates of that
    scope. The open belief on the same subject is none of them: the model cannot name it, and
    the private one it contradicts is superseded. resolve keeps to the scope on its own too: a
    contradiction across scopes supersedes nothing, and a duplicate citing the whisper is not
    folded into the open row."""
    said = h.mem(101, "Testy talked about the bakery in guild chat.", actors=(1,))
    told = h.mem(101, "Testy whispered me he is quitting the bakery.", actors=(1,), scope="private", scope_with=1)
    old = belief(h, 101, "Testy is leaving the bakery soon.", actors=(1,), evidence=(f"memory:{said}",))
    staying = belief(h, 101, "Testy is staying at the bakery.", actors=(1,), evidence=(f"memory:{told}",), scope="private", scope_with=1)
    claim = Claim("belief", 101, "Testy is quitting the bakery soon.", [f"memory:{said}"], about=1, extra={"importance": 6})
    shown = [f"memory:{said}", f"memory:{told}"]
    ctx = h.ctx({"supersede": [{"decisions": [{"id": old, "relation": "duplicates"}, {"id": staying, "relation": "contradicts"}]}]})
    stored = ctx.writer.scope_of(101, claim.evidence, shown=shown)
    assert stored == ("private", 1)
    assert [m.id for m in candidates(h.store, 101, 1, vec(claim.text))] == [old]     # what the citation's scope would offer (N3)
    cands = candidates(h.store, 101, 1, vec(claim.text), scope=stored)
    assert [m.id for m in cands] == [staying]
    ds = run(decide(ctx, claim, cands))
    assert ds == [Decision(staying, "contradicts")]
    with h.store.transaction():
        new = resolve(h.store, ds, claim, put_belief(ctx, claim, shown=shown))
    whispered = Claim("belief", 101, claim.text, [f"memory:{told}"], about=1, extra={"importance": 6})
    with h.store.transaction():
        crossed = resolve(h.store, [Decision(old, "contradicts")], claim, put_belief(ctx, claim, shown=shown))
        folded = resolve(h.store, [Decision(old, "duplicates")], whispered, put_belief(ctx, whispered))
    rows = {m.id: m for m in h.rows(101)}
    assert (rows[staying].status, rows[staying].superseded_by) == ("superseded", new)
    assert [(rows[i].scope, rows[i].scope_with) for i in (new, crossed, folded)] == [("private", 1)] * 3
    o = rows[old]
    assert (o.status, o.superseded_by, o.evidence, o.merged_n, o.importance) == ("active", None, (f"memory:{said}",), 1, 5)


def test_an_open_belief_never_touches_a_private_one(h):
    """PR7, the other way. A belief from guild chat is stored open, and the private belief on
    the same subject is no candidate for it: the model cannot name it, and resolve, handed the
    contradiction anyway, leaves it as it is. The open belief it contradicts is superseded."""
    said = h.mem(101, "Testy talked about the bakery in guild chat.", actors=(1,))
    told = h.mem(101, "Testy whispered me he is quitting the bakery.", actors=(1,), scope="private", scope_with=1)
    theirs = belief(h, 101, "Testy is leaving the bakery soon.", actors=(1,), evidence=(f"memory:{told}",), scope="private", scope_with=1)
    staying = belief(h, 101, "Testy is staying at the bakery.", actors=(1,), evidence=(f"memory:{said}",))
    claim = Claim("belief", 101, "Testy is quitting the bakery soon.", [f"memory:{said}"], about=1, extra={"importance": 6})
    ctx = h.ctx({"supersede": [{"decisions": [{"id": theirs, "relation": "duplicates"}, {"id": staying, "relation": "contradicts"}]}]})
    stored = ctx.writer.scope_of(101, claim.evidence)
    assert stored == ("open", 0)
    assert [m.id for m in candidates(h.store, 101, 1, vec(claim.text), scope=("private", 1))] == [theirs]   # there, in its own scope
    cands = candidates(h.store, 101, 1, vec(claim.text), scope=stored)
    assert [m.id for m in cands] == [staying]
    ds = run(decide(ctx, claim, cands))
    assert ds == [Decision(staying, "contradicts")]
    with h.store.transaction():
        new = resolve(h.store, ds, claim, put_belief(ctx, claim))
        crossed = resolve(h.store, [Decision(theirs, "contradicts")], claim, put_belief(ctx, claim))
    rows = {m.id: m for m in h.rows(101)}
    assert (rows[staying].status, rows[staying].superseded_by) == ("superseded", new)
    assert [(rows[i].scope, rows[i].scope_with) for i in (new, crossed)] == [("open", 0)] * 2
    t = rows[theirs]
    assert (t.status, t.superseded_by, t.evidence, t.merged_n) == ("active", None, (f"memory:{told}",), 1)


def test_resolve_acts_only_on_the_owners_active_rows_that_can_hold_the_evidence(h):
    """Decisions resolve should never have been handed change nothing: another member's memory
    and one already superseded, though both are private with Testy like the claim, and a
    duplicate whose scope could not hold the claim's evidence (private with Coros does not hold
    what is private with Testy). A stricter row can: a secret with Testy takes what was private
    with him."""
    told = h.mem(101, "Testy whispered me about the bakery.", actors=(1,), scope="private", scope_with=1)
    hers = belief(h, 102, "Testy works at the bakery.", actors=(1,), scope="private", scope_with=1)
    gone = belief(h, 101, "Testy works at the bakery.", actors=(1,), status="superseded", scope="private", scope_with=1)
    coros = belief(h, 101, "Testy works at the bakery.", actors=(1,), scope="private", scope_with=103)
    kept = belief(h, 101, "Testy works at the bakery.", actors=(1,), scope="secret", scope_with=1)
    claim = Claim("belief", 101, "Testy bakes at night.", [f"memory:{told}"], about=1, extra={"importance": 6})
    ctx = h.ctx()
    with h.store.transaction():
        new = resolve(h.store, [Decision(hers, "duplicates"), Decision(gone, "duplicates"), Decision(coros, "duplicates"),
                                Decision(hers, "contradicts"), Decision(gone, "contradicts")], claim, put_belief(ctx, claim))
    rows = {m.id: m for m in h.store.get_memories([hers, gone, coros, new])}
    assert (rows[new].scope, rows[new].scope_with) == ("private", 1)
    assert [(rows[i].status, rows[i].superseded_by, rows[i].merged_n) for i in (hers, gone, coros)] == [
        ("active", None, 1), ("superseded", None, 1), ("active", None, 1)]
    with h.store.transaction():
        got = resolve(h.store, [Decision(kept, "duplicates")], claim, lambda: pytest.fail("inserted"))
    k = h.store.get_memories([kept])[0]
    assert got == kept and (k.scope, k.scope_with, k.evidence) == ("secret", 1, ("event:1", f"memory:{told}"))
