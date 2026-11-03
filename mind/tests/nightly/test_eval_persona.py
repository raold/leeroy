"""Synthetic weekly persona evaluation, retry, privacy and denominator checks."""
import json
import random
import re
from dataclasses import replace
from types import SimpleNamespace

import pytest

from conftest import run
from nightly.conftest import no_real_name
from leeroy_mind.eval import interview as interviews, persona
from leeroy_mind.nightly import growth
from leeroy_mind.storage.rows import pack_vec


def sent(h, guid, n=4, *, channel="guild"):
    return [h.say(h.T + guid + i, guid, f"voice {guid} line {i}", channel=channel,
                  to=h.human if channel == "whisper" else 0) for i in range(n)]


def scores(prompt):
    return {"scores": [{"i": int(i), "score": 5 if int(g) == 101 else 2}
                       for i, g in re.findall(r"(?m)^(\d+) \| speaker (\d+):", prompt)]}


def test_consistency_scores_are_written_once_with_complete_member_coverage(h):
    sent(h, 101)
    sent(h, 102)
    ctx = h.ctx({"consistency": [scores]})
    values = run(persona.consistency(ctx, ctx.window, random.Random(7), "2026-W44"))
    assert values == {101: 1.0, 102: 0.4}
    assert h.count("eval_score") == 2
    assert persona.consistency_coverage(h.store, "2026-W44") == {
        "sampled_lines": 8, "scored_lines": 8, "sampled_members": 2, "scored_members": 2}
    retry = h.ctx(night="2026-10-30")
    assert run(persona.consistency(retry, ctx.window, random.Random(99), "2026-W44")) == values
    assert retry.reasoner.llm.calls == [] and h.count("eval_score") == 2


@pytest.mark.parametrize("reply", [
    {"scores": []}, {"scores": [{"i": 0, "score": 5}] * 4},
    {"scores": [{"i": i, "score": 5} for i in (0, 1, 2, 9)]},
])
def test_consistency_requires_exact_index_coverage(h, reply):
    sent(h, 101)
    ctx = h.ctx({"consistency": [reply]})
    assert run(persona.consistency(ctx, ctx.window, random.Random(1), "2026-W44")) == {}
    assert ctx.failures["eval"] == 1 and h.count("eval_score") == 0
    assert persona.consistency_coverage(h.store, "2026-W44")["scored_lines"] == 0


@pytest.mark.parametrize("change", ["drop", "persona"])
def test_consistency_rechecks_current_sources_and_uncached_persona(h, change):
    sent(h, 101)

    def changing(prompt):
        if change == "drop":
            h.store.conn.execute("UPDATE utterance SET dropped='policy' WHERE guid=101")
        else:
            h.persona(101, player_gender="female")
        return scores(prompt)

    ctx = h.ctx({"consistency": [changing]})
    ctx.card(101)
    assert run(persona.consistency(ctx, ctx.window, random.Random(1), "2026-W44")) == {}
    assert ctx.failures["eval"] == 1 and h.count("eval_score") == 0


def test_consistency_bounds_total_and_per_member_and_excludes_quiet_members(h):
    sent(h, 101, 18)
    sent(h, 102, 18)
    sent(h, 103, 2)
    h.utter(h.T, h.human, "The owner is not a simulated persona.")
    ctx = h.ctx({"consistency": [scores, scores]})
    run(persona.consistency(ctx, ctx.window, random.Random(3), "2026-W44", lines=13))
    plan = json.loads(h.store.get_meta("eval:consistency-plan:2026-W44"))["members"]
    assert sum(map(len, plan.values())) <= 13 and max(map(len, plan.values())) <= 10
    assert "103" not in plan and str(h.human) not in plan


def test_distinctiveness_measures_correct_guesses_and_cosines_with_real_denominators(h, monkeypatch):
    for guid in (101, 102, 103):
        sent(h, guid, 3)
    ctx = h.ctx()
    calls = []

    async def choose(_system, user, _schema, **_):
        calls.append(user)
        text = json.loads(user)
        row = h.store.conn.execute("SELECT id,guid FROM utterance WHERE text=?", (text,)).fetchone()
        plan = json.loads(h.store.get_meta("eval:distinctiveness-plan:2026-W44"))
        item = next(t for t in plan["trials"] if t["id"] == row["id"])
        return {"pick": item["options"].index(row["guid"]) + 1}

    async def embed(texts):
        return [pack_vec({101: [1, 0], 102: [0, 1], 103: [-1, 0]}[int(re.search(r"voice (\d+)", t)[1])]) for t in texts]

    monkeypatch.setattr(ctx.reasoner, "json", choose)
    monkeypatch.setattr(ctx, "embed", embed)
    result = run(persona.distinctiveness(ctx, ctx.window, random.Random(9)))
    assert result["who_said_accuracy"] == 1
    assert (result["trials_requested"], result["trials_sampled"], result["trials_scored"]) == (60, 9, 9)
    assert result["within_member_cosine"] == pytest.approx(1)
    assert result["cross_member_cosine"] == pytest.approx(-1 / 3)
    assert result["within_member_pairs"] == 9 and result["cross_member_pairs"] == 27
    assert run(persona.distinctiveness(ctx, ctx.window, random.Random(55))) == result
    assert len(calls) == 9


def test_empty_evaluation_reports_unavailable_not_perfect_scores(h):
    ctx = h.ctx()
    assert run(persona.consistency(ctx, ctx.window, random.Random(1), "2026-W44")) == {}
    result = run(persona.distinctiveness(ctx, ctx.window, random.Random(1)))
    assert result["trials_scored"] == 0
    assert all(result[k] is None for k in ("who_said_accuracy", "cross_member_cosine", "within_member_cosine"))
    assert ctx.reasoner.llm.calls == []


def test_private_sent_dialogue_stays_in_owner_evaluation(h):
    sent(h, 101, channel="whisper")
    ctx = h.ctx({"consistency": [scores]})
    assert run(persona.consistency(ctx, ctx.window, random.Random(1), "2026-W44")) == {101: 1.0}
    assert h.count("memory") == h.count("review_queue") == h.count("persona_ledger") == 0


def seed_drift(h):
    for week, values in {"2026-W43": {101: 0.9, 102: 0.9}, "2026-W44": {101: 0.7, 102: 0.88}}.items():
        for guid, value in values.items():
            h.store.conn.execute("INSERT INTO eval_score VALUES (?,?,'consistency',?)", (week, guid, value))
    h.store.put_state(replace(h.store.get_state(101), self_notes=(
        {"text": "An older note.", "evidence": [], "added": h.T - 20 * 86400},
        {"text": "A new note.", "evidence": [], "added": h.T - 2 * 86400})))


def test_drift_rollback_is_atomic_undoable_and_not_repeated(h):
    seed_drift(h)
    result = persona.drift_check(h.store, "2026-W44", "2026-W43", since=h.T - 7 * 86400, now=h.T)
    assert result == [101] and h.count("review_queue") == 1
    assert [n["text"] for n in h.store.get_state(101).self_notes] == ["An older note."]
    rolled = h.store.conn.execute("SELECT id FROM persona_ledger WHERE kind='note_rolled_back'").fetchone()[0]
    assert growth.undo(h.store, rolled, now=h.T + 1)
    assert len(h.store.get_state(101).self_notes) == 2
    assert persona.drift_check(h.store, "2026-W44", "2026-W43", since=h.T - 7 * 86400, now=h.T + 2) == [101]
    assert len(h.store.get_state(101).self_notes) == 2 and h.count("review_queue") == 1


def test_failed_drift_rolls_back_state_reviews_and_receipt(h, monkeypatch):
    seed_drift(h)
    rollback = persona.rollback_week

    def fail(*args, **kwargs):
        rollback(*args, **kwargs)
        raise RuntimeError("synthetic rollback failure")

    monkeypatch.setattr(persona, "rollback_week", fail)
    with pytest.raises(RuntimeError):
        persona.drift_check(h.store, "2026-W44", "2026-W43", since=h.T - 7 * 86400, now=h.T)
    assert len(h.store.get_state(101).self_notes) == 2
    assert h.count("review_queue") == h.count("persona_ledger") == 0
    assert h.store.get_meta("eval:drift:2026-W44") is None


def test_interview_panel_keeps_members_and_replaces_only_departure(h):
    ctx = h.ctx()
    ctx.settings = replace(ctx.settings, interview_members=3)
    first = interviews.interview_panel(ctx)
    assert len(first) == len(set(first)) == 3
    assert interviews.interview_panel(ctx) == first
    h.store.conn.execute("INSERT INTO away(guid,reason,left_at,returns_at) VALUES (?,'rest',?,?)", (first[0], h.T, h.T + 86400))
    second = interviews.interview_panel(ctx)
    assert set(second) & set(first) == set(first[1:])
    assert len(second) == 3 and interviews.interview_panel(ctx) == second


class Asker:
    def __init__(self, h, *, changed=False, fail=None):
        self.ident, self.calls, self.changed, self.fail = h.ident, [], changed, fail
        self.memory = h.mem(101, "A remembered raid.", evidence=())

    async def ask(self, guid, question):
        self.calls.append((guid, question))
        n = next(i for i, (_, q) in enumerate(interviews.QUESTIONS) if q == question)
        if n == self.fail:
            raise RuntimeError("synthetic missing answer")
        return SimpleNamespace(text="stable trait" if n < 5 else "new people" if self.changed else "old people",
                               memory_ids=[] if n < 5 else [self.memory])


def test_interview_and_stability_are_resumable_and_grounded(h, monkeypatch):
    old = Asker(h)
    current = Asker(h, changed=True)
    assert run(interviews.interview(h.store, old, "2026-W43", [101]))["answered"] == 10
    assert run(interviews.interview(h.store, current, "2026-W44", [101]))["answered"] == 10
    again = run(interviews.interview(h.store, current, "2026-W44", [101]))
    assert again["asked"] == 0 and again["skipped"] == 10 and len(current.calls) == 10
    ctx = h.ctx()

    async def embed(texts):
        return [pack_vec([0, 1] if text == "new people" else [1, 0]) for text in texts]

    monkeypatch.setattr(ctx, "embed", embed)
    result = run(interviews.stability(ctx, "2026-W44", "2026-W43"))
    assert result["trait_similarity"] == 1 and result["people_changed"] == 1
    assert result["people_grounded"] == 1 and result["trait_pairs"] == result["people_pairs"] == 5
    assert no_real_name(h.store) == []


def test_failed_interview_answers_keep_honest_denominator_and_retry_only_missing(h):
    asker = Asker(h, fail=2)
    first = run(interviews.interview(h.store, asker, "2026-W44", [101]))
    assert (first["expected"], first["answered"], first["pending"], first["failed"]) == (10, 9, 1, 1)
    accepted = [tuple(r) for r in h.store.conn.execute("SELECT * FROM eval_answer WHERE answer IS NOT NULL ORDER BY id")]
    asker.fail = None
    second = run(interviews.interview(h.store, asker, "2026-W44", [101]))
    assert second["asked"] == 1 and second["answered"] == 10 and len(asker.calls) == 11
    now = [tuple(r) for r in h.store.conn.execute("SELECT * FROM eval_answer ORDER BY id")]
    assert all(row in now for row in accepted)


def test_stability_without_previous_week_has_no_invented_similarity(h):
    asker = Asker(h)
    run(interviews.interview(h.store, asker, "2026-W44", [101]))
    result = run(interviews.stability(h.ctx(), "2026-W44", "2026-W43"))
    assert result["trait_similarity"] is result["people_changed"] is None
    assert result["trait_pairs"] == result["people_pairs"] == 0
    assert result["people_grounded"] == 1


def test_interview_requires_identity_before_side_effects(h):
    with pytest.raises(ValueError):
        run(interviews.interview(h.store, SimpleNamespace(), "2026-W44", [101]))
    assert h.count("eval_answer") == 0
