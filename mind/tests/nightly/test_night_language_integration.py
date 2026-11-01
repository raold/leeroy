"""Exercise real unit ordering, source boundaries and candidate budgeting together."""
from dataclasses import replace

from conftest import run
from leeroy_mind.nightly import budget
from leeroy_mind.nightly.language import stage_language


def none(prompt):
    return {"kind": "none", "term": "", "for": None, "evidence": [1]}


def moment(h, offset=0):
    line = h.say(h.T + offset, 102, "Belra saved the pull")
    h.say(h.T + offset + 2, 101, "haha")
    for owner in (101, 102, 103):
        h.mem(owner, "Belra saved the pull.", t=h.T + offset, importance=8,
              actors=(102,), evidence=(f"chat:{line}",))
    return line


def test_language_real_units_replay_without_recounting_or_human_memories(h):
    h.say(h.T, 102, "pulling with my face again")
    h.say(h.T + 5, 101, "lmao")
    h.say(h.T + 8, 1, "haha")
    ctx = h.ctx(default=none)
    run(stage_language(ctx))
    assert not ctx.failures
    terms = [tuple(r) for r in h.store.conn.execute("SELECT * FROM lexicon ORDER BY id")]
    adopters = [tuple(r) for r in h.store.conn.execute("SELECT * FROM lexicon_adopter ORDER BY term,guid")]
    assert h.store.conn.execute("SELECT count(*) FROM lexicon WHERE kind='joke'").fetchone()[0] == 1
    assert h.rows(0) == h.rows(1) == []
    assert all(ctx.done("language", u) for u in ("vocab", "slang", "jokes", "candidates", "spread", "fade"))
    run(stage_language(h.ctx(default=none)))
    assert terms == [tuple(r) for r in h.store.conn.execute("SELECT * FROM lexicon ORDER BY id")]
    assert adopters == [tuple(r) for r in h.store.conn.execute("SELECT * FROM lexicon_adopter ORDER BY term,guid")]


def test_candidate_budget_counts_actual_one_call_units_and_saved_cap(h):
    for offset in (0, 100, 200):
        moment(h, offset)
    ctx = h.ctx(default=none)
    ctx.settings = replace(ctx.settings, lexicon_candidates=2)
    before = h.store.conn.total_changes
    assert budget.COUNTERS["lexicon"](ctx) == 2
    assert h.store.conn.total_changes == before
    assert budget.estimate({"lexicon": 2}, measured={}) == {"lexicon": 40}
    run(stage_language(ctx))
    assert not ctx.failures
    assert len([c for c in ctx.reasoner.llm.calls if "messages" in c]) == 2
    assert budget.COUNTERS["lexicon"](ctx) == 0
    retry = h.ctx(default=none)
    retry.settings = replace(retry.settings, lexicon_candidates=3)
    run(stage_language(retry))
    assert not retry.reasoner.llm.calls  # larger settings do not expand a frozen nightly plan


def test_no_candidate_calls_without_laughs_or_when_degraded_to_zero(h):
    line = h.say(h.T, 102, "Belra saved the pull")
    for owner in (101, 102, 103):
        h.mem(owner, "Belra saved the pull.", importance=8, actors=(102,),
              evidence=(f"chat:{line}",))
    assert budget.COUNTERS["lexicon"](h.ctx()) == 0
    h.say(h.T + 2, 101, "haha")
    ctx = h.ctx(default=none)
    ctx.settings = replace(ctx.settings, lexicon_candidates=0)
    assert budget.COUNTERS["lexicon"](ctx) == 0
    run(stage_language(ctx))
    assert not ctx.reasoner.llm.calls and not ctx.failures


def test_dropped_and_whispered_chat_never_seed_language(h):
    for n, owner in enumerate((101, 102, 103)):
        h.say(h.T + n, owner, "zug", channel="whisper", to=1)
    line = h.say(h.T + 10, 102, "pulling with my face again")
    with h.store.transaction():
        h.store.conn.execute("UPDATE utterance SET dropped=? WHERE guid=? AND ts=?",
                             ("not sent", 102, h.T + 10))
    h.say(h.T + 12, 101, "lmao")
    h.say(h.T + 14, 1, "haha")
    ctx = h.ctx(default=none)
    run(stage_language(ctx))
    assert not ctx.failures
    assert not h.store.conn.execute("SELECT 1 FROM vocab WHERE word IN ('zug','pulling','face')").fetchone()
    assert not h.store.conn.execute("SELECT 1 FROM lexicon WHERE origin_ref=?", (f"chat:{line}",)).fetchone()
