import asyncio
import json
import random
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from conftest import run
from leeroy_mind.eval.quiz import (CATEGORIES, QuizItem, ask_all, build_quiz, judge_all,
                                    score)
from leeroy_mind.nightly.settings import week_bounds

UTC = timezone.utc
WEEK = "2026-W44"
BOUNDS = week_bounds(WEEK, UTC)
MON, TUE = (datetime(2026, 10, day, 21, tzinfo=UTC).timestamp() for day in (26, 27))


def seed(h):
    h.boss(MON, 631, 0, "killed", (101, 102))
    for i in range(3):
        h.boss(TUE + i, 631, 11, "wiped", (101, 102))
    h.boss(TUE + 9, 631, 11, "killed", (101, 102))
    h.loot(MON + 5, 101, "Bryntroll")
    h.loot(TUE + 5, 101, "Deathbringer's Will")


def quiz(h, **kw):
    return build_quiz(h.store, BOUNDS, UTC, random.Random(1), **kw)


def by_category(items):
    return {c: [i for i in items if i.category == c] for c in CATEGORIES}


def test_build_quiz_from_ground_truth(h):
    seed(h)
    by = by_category(quiz(h))
    assert all(by.values())
    assert by["single_session"][0].truth == "3"
    assert "Sindragosa on Tuesday night?" in by["single_session"][0].question
    assert by["temporal"][0].truth == "Lord Marrowgar"
    assert by["knowledge_update"][0].truth == "Deathbringer's Will"
    assert all(i.truth is None and "Sindragosa" not in i.question and "Lord Marrowgar" not in i.question
               for i in by["abstention"])


def test_quiz_is_deterministic_and_caps_categories_and_distinct_members(h):
    seed(h)
    assert quiz(h) == quiz(h)
    items = quiz(h, members=2, per_category=1)
    assert len({i.guid for i in items}) <= 2
    assert all(len(v) <= 1 for v in by_category(items).values())
    assert quiz(h, members=0) == []
    assert quiz(h, per_category=0) == []


def test_presences_are_specific_to_count_and_first_kills(h):
    h.boss(MON, 631, 0, "killed", (101,))
    h.boss(MON + 1, 631, 0, "killed", (102,))
    h.boss(TUE, 631, 11, "wiped", (101, 102))
    h.boss(TUE + 1, 631, 11, "wiped", (101,))
    h.boss(TUE + 2, 631, 11, "killed", (101, 102))
    by = by_category(quiz(h, per_category=30))
    assert {i.guid for i in by["single_session"]} == {101}
    assert {i.guid for i in by["temporal"]} == {101}
    assert {i.guid for i in by["multi_session"]} == {101, 102}


def test_tied_wipes_and_simultaneous_kills_have_no_ambiguous_truth(h):
    for when, pair in ((MON, (631, 0)), (TUE, (631, 11))):
        h.boss(when, *pair, "wiped", (101,))
        h.boss(TUE + 20, *pair, "killed", (101,))
    by = by_category(quiz(h))
    assert not by["multi_session"]
    assert not by["temporal"]


def test_bosses_filter_non_fights_unknowns_and_outside_week(h):
    h.boss(BOUNDS[0] - 1, 631, 0, "killed", (101,))
    h.boss(MON, 631, 13, "wiped", (101,))
    h.boss(TUE, 631, 9876, "wiped", (101,))
    h.boss(BOUNDS[1], 631, 0, "killed", (101,))
    by = by_category(quiz(h, per_category=2000))
    assert not by["single_session"]
    assert not by["temporal"]
    assert any("Lord Marrowgar" in i.question for i in by["abstention"])


def test_after_midnight_uses_previous_logical_night(h):
    at = datetime(2026, 10, 28, 2, tzinfo=UTC).timestamp()
    h.boss(at, 631, 11, "wiped", (101,))
    assert "Tuesday night" in by_category(quiz(h))["single_session"][0].question


def test_loot_uses_latest_before_week_end_and_false_premise_respects_ever_won(h):
    h.loot(BOUNDS[0] - 100, 101, "Old Blade", item_id=1)
    h.loot(MON, 101, "New Blade", item_id=2)
    h.loot(TUE, 102, "OLD BLADE", item_id=3)
    h.loot(TUE + 1, 102, "Other Ring", item_id=4)
    h.loot(BOUNDS[1] + 1, 101, "Future Sword", item_id=5)
    by = by_category(quiz(h, per_category=2000))
    assert next(i.truth for i in by["knowledge_update"] if i.guid == 101) == "New Blade"
    false_items = [i.question for i in by["abstention"] if i.guid == 101 and i.qkey.startswith("no-item:")]
    assert false_items == ["When did you win Other Ring?"]


def test_latest_loot_tie_is_skipped(h):
    h.loot(MON, 101, "A")
    h.loot(MON, 101, "B")
    assert not by_category(quiz(h))["knowledge_update"]


def test_malformed_history_never_supplies_negative_ground_truth(h):
    with h.store.transaction():
        h.store.conn.execute("INSERT INTO game_event(ts,type,payload) VALUES (?,'boss','{')", (MON,))
        h.store.conn.execute("INSERT INTO game_event(ts,type,payload) VALUES (?,'loot_won','{')", (MON,))
    assert quiz(h) == []


def test_no_human_owner_zero_or_missing_persona_is_quizzed(h):
    seed(h)
    with h.store.transaction():
        h.store.conn.execute("DELETE FROM persona_core WHERE guid=101")
    assert all(i.guid not in (0, 1, 101) for i in quiz(h, per_category=2000))


def item(key="one", *, guid=101, truth="3", category="single_session"):
    return QuizItem(category, guid, key, "How many times did we wipe?", truth)


class Asker:
    def __init__(self, ident, script=None):
        self.ident, self.script, self.calls = ident, script, []

    async def ask(self, guid, question):
        self.calls.append((guid, question))
        result = self.script(guid, question) if self.script else "three"
        if isinstance(result, BaseException):
            raise result
        return SimpleNamespace(text=result, memory_ids=[7, 7])


def test_asking_is_screened_frozen_and_idempotent_without_live_side_effects(h):
    before = h.count("chat_line"), h.count("memory")
    asker = Asker(h.ident, lambda g, q: "|TQuillon|t Quillon says three")
    assert run(ask_all(h.store, asker, [item()], WEEK))["asked"] == 1
    row = dict(h.store.conn.execute("SELECT * FROM eval_answer").fetchone())
    assert "Quillon" not in row["answer"] and "three" in row["answer"]
    assert json.loads(row["memory_ids"]) == [7]
    assert run(ask_all(h.store, asker, [item("new")], WEEK))["skipped"] == 1
    assert len(asker.calls) == 1 and h.count("eval_answer") == 1
    assert before == (h.count("chat_line"), h.count("memory"))


def test_partial_failure_retries_only_pending_and_keeps_denominator(h):
    asker = Asker(h.ident, lambda g, q: RuntimeError("unavailable") if g == 101 else "three")
    counts = run(ask_all(h.store, asker, [item(), item(guid=102)], WEEK))
    assert counts == {"asked": 1, "failed": 1, "skipped": 0}
    result = score(h.store, WEEK)
    assert (result["n"], result["answered_n"], result["pending_n"]) == (2, 1, 2)
    asker.script = None
    counts = run(ask_all(h.store, asker, [], WEEK))
    assert counts == {"asked": 1, "failed": 0, "skipped": 1}
    assert len(asker.calls) == 3


def test_cancellation_keeps_all_planned_items_pending(h):
    asker = Asker(h.ident, lambda g, q: asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        run(ask_all(h.store, asker, [item(), item(guid=102)], WEEK))
    assert score(h.store, WEEK)["n"] == 2
    assert score(h.store, WEEK)["answered_n"] == 0


def test_asking_requires_identity_before_any_persistence(h):
    with pytest.raises(ValueError, match="HumanIdentity"):
        run(ask_all(h.store, Asker(None), [item()], WEEK))
    assert h.count("eval_answer") == 0
    assert h.store.get_meta(f"eval:quiz-plan:{WEEK}") is None


def test_answer_is_not_written_if_question_changes_while_awaiting(h):
    def mutate(guid, question):
        with h.store.transaction():
            h.store.conn.execute("UPDATE eval_answer SET truth='99'")
        return "three"
    counts = run(ask_all(h.store, Asker(h.ident, mutate), [item()], WEEK))
    assert counts["failed"] == 1
    assert h.store.conn.execute("SELECT answer FROM eval_answer").fetchone()[0] is None


def test_completed_answer_cannot_bypass_frozen_truth_check_on_retry(h):
    asker = Asker(h.ident)
    run(ask_all(h.store, asker, [item()], WEEK))
    with h.store.transaction():
        h.store.conn.execute("UPDATE eval_answer SET truth='99'")
    assert run(ask_all(h.store, asker, [], WEEK)) == {"asked": 0, "failed": 1, "skipped": 0}
    assert len(asker.calls) == 1
    ctx = h.ctx()
    assert run(judge_all(ctx, WEEK)) == {"judged": 0, "failed": 1}
    assert ctx.reasoner.calls == 0
    with h.store.transaction():
        h.store.conn.execute("UPDATE eval_answer SET verdict='correct'")
    result = score(h.store, WEEK)
    assert result["invalid_n"] == result["pending_n"] == 1
    assert result["judged_n"] == 0


def test_blank_output_is_pending_not_an_abstention(h):
    assert run(ask_all(h.store, Asker(h.ident, lambda g, q: " "), [item()], WEEK))["failed"] == 1
    assert score(h.store, WEEK)["judged_n"] == 0


def test_judging_is_screened_idempotent_and_does_not_judge_pending(h):
    run(ask_all(h.store, Asker(h.ident), [item(), item(guid=102)], WEEK))
    with h.store.transaction():
        h.store.conn.execute("UPDATE eval_answer SET answer='|TQuillon|t Quillon says 3' WHERE guid=101")
        h.store.conn.execute("UPDATE eval_answer SET answer=NULL WHERE guid=102")
    ctx = h.ctx({"quiz_judge": [{"verdict": "correct"}]})
    assert run(judge_all(ctx, WEEK)) == {"judged": 1, "failed": 0}
    assert run(judge_all(ctx, WEEK)) == {"judged": 0, "failed": 0}
    calls = [c for c in ctx.reasoner.llm.calls if "messages" in c]
    assert len(calls) == 1
    assert "Quillon" not in json.dumps(calls)
    result = score(h.store, WEEK)
    assert result["accuracy"]["single_session"] == 1.0
    assert result["accuracy_n"]["single_session"] == 1
    assert result["pending_n"] == 1


def test_judge_failure_and_postawait_change_remain_retryable(h):
    run(ask_all(h.store, Asker(h.ident), [item(), item(guid=102)], WEEK))
    def mutate(prompt):
        with h.store.transaction():
            h.store.conn.execute("UPDATE eval_answer SET answer='changed' WHERE guid=101")
        return {"verdict": "correct"}
    ctx = h.ctx({"quiz_judge": [mutate, {"verdict": "incorrect"}]})
    assert run(judge_all(ctx, WEEK)) == {"judged": 1, "failed": 1}
    assert h.store.conn.execute("SELECT verdict FROM eval_answer WHERE guid=101").fetchone()[0] is None
    ctx = h.ctx({"quiz_judge": [{"verdict": "correct"}]})
    assert run(judge_all(ctx, WEEK))["judged"] == 1


def test_false_premise_cannot_be_marked_correct(h):
    run(ask_all(h.store, Asker(h.ident), [item(truth=None, category="abstention")], WEEK))
    ctx = h.ctx({"quiz_judge": [{"verdict": "correct"}]})
    assert run(judge_all(ctx, WEEK)) == {"judged": 0, "failed": 1}
    assert score(h.store, WEEK)["abstention_rate"] is None


def test_scoring_has_honest_denominators_and_no_data_rates(h):
    with h.store.transaction():
        for i in range(12):
            abstention = i < 10
            verdict = ("abstained" if i < 9 else "incorrect") if abstention else ("correct" if i == 10 else "incorrect")
            h.store.conn.execute("INSERT INTO eval_answer(week,kind,guid,qkey,category,truth,answer,verdict) "
                "VALUES (?,'quiz',101,?,?,?,?,?)", (WEEK, str(i), "abstention" if abstention else "single_session",
                None if abstention else "3", "answer", verdict))
    result = score(h.store, WEEK)
    assert result["abstention_rate"] == .9
    assert result["false_memory_rate"] == .1
    assert result["accuracy"]["single_session"] == .5
    assert result["accuracy"]["temporal"] is None
    assert result["n"] == result["judged_n"] == 12
    assert result["abstention_n"] == 10
    assert score(h.store, "2026-W43")["abstention_rate"] is None
