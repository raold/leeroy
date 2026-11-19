"""Promotion math and the learn bandit receiving the winner (synthetic scores)."""
import json
from datetime import timezone

import pytest

from leeroy_mind.lab import promote, rules, store
from leeroy_mind.learn import arms, cycle
from leeroy_mind.learn.config import LearnCfg
from leeroy_mind.storage.db import open_db

CLEAN = {c: 0 for c in rules.CODES}


def rows(scores, *, applied=1, flags=None, judged=None):
    out = {}
    for i, s in enumerate(scores):
        key = (i // 2 + 1, i % 2 + 1)
        out[key] = {"pair": key[0], "turn": key[1], "rubric": s, "applied": applied,
                    "rules": dict(CLEAN, **(flags or {}).get(i, {})),
                    "judged_human": None if judged is None else judged[i]}
    return out


def test_a_variant_wins_by_the_threshold_with_no_regression():
    control = rows([1] * 10)
    sheet = promote.compare(control, rows([1] * 8 + [2, 2]))  # mean +0.2
    assert sheet["outcome"] == "won" and sheet["delta"] == pytest.approx(0.2)
    assert promote.compare(control, rows([1] * 9 + [2]))["outcome"] == "lost"  # +0.1
    edge = promote.compare(rows([0] * 20), rows([0] * 17 + [1, 1, 1]))  # exactly +0.15
    assert edge["outcome"] == "won"


def test_a_rule_regression_or_no_application_or_too_few_answers_never_wins():
    control = rows([1] * 10)
    worse = promote.compare(control, rows([3] * 10, flags={0: {"wordy": 1}, 1: {"wordy": 1}}))
    assert worse["outcome"] == "regressed" and worse["regressed"] == ["wordy"]
    # one more flagged answer is noise; with 30 answers the margin is 3
    assert promote.compare(control, rows([3] * 10, flags={0: {"wordy": 1}}))["outcome"] == "won"
    many = {i: {"echo": 1} for i in range(2)}
    assert promote.compare(rows([1] * 30), rows([3] * 30, flags=many))["outcome"] == "won"
    many[2] = {"echo": 1}
    assert promote.compare(rows([1] * 30), rows([3] * 30, flags=many))["regressed"] == ["echo"]
    assert promote.compare(control, rows([3] * 10, applied=0))["outcome"] == "untested"
    assert promote.compare(rows([1] * 6), rows([3] * 6))["outcome"] == "too_few"
    # a flag the control has as often is no regression
    both = {0: {"echo": 1}}
    assert promote.compare(rows([1] * 10, flags=both), rows([3] * 10, flags=both))["outcome"] == "won"


def test_the_sheet_counts_only_turns_both_arms_have_and_reads_the_judge():
    control = rows([1] * 10, judged=[0, 0, 1, 1, 0, 0, 0, 0, 0, 0])
    variant = rows([2] * 12, judged=[1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 1, 1])
    sheet = promote.compare(control, variant)
    assert sheet["lines"] == 10 and sheet["graded"] == 10
    assert sheet["judge"] == {"pairs": 5, "control_human": 0.2, "variant_human": 0.4}


def seed(conn, *, now=1000.0):
    rid = store.create_run(conn, night="n", source="night", piece="reply_human", seed="s",
                           plan={"pairs": []}, now=now)
    a = store.add_variant(conn, rid, "reply_human", "A text", "diff", now)
    b = store.add_variant(conn, rid, "reply_human", "B text", "diff", now)
    c = store.add_variant(conn, rid, "reply_human", "C text", "diff", now)

    def put(arm, scores):
        store.put_scores(conn, rid, arm, [
            {"pair": i // 2 + 1, "turn": i % 2 + 1, "guid": 101, "kind": "flirt", "channel": "dm",
             "answered": 1, "applied": int(arm != "control"), "rules": dict(CLEAN), "words": 3,
             "his_words": 3} for i in range(len(scores))])
        for i, s in enumerate(scores):
            conn.execute("UPDATE lab_score SET rubric=?, rubric_code='generic' WHERE run=? AND arm=? "
                         "AND pair=? AND turn=?", (s, rid, arm, i // 2 + 1, i % 2 + 1))
    put("control", [1] * 10)
    put(f"v{a}", [2] * 10)    # +1.0: the best
    put(f"v{b}", [1] * 5 + [2] * 5)  # +0.5: beaten
    put(f"v{c}", [1] * 10)    # lost
    return rid, a, b, c


def test_the_best_winner_becomes_an_active_arm_and_the_rest_retire(tmp_path):
    conn = open_db(tmp_path / "mind.db")
    try:
        rid, a, b, c = seed(conn)
        result = promote.promote(conn, rid, settings=LearnCfg(), now=2000.0, allow=True)
        conn.commit()
        assert result["promoted"] == f"lab.reply_human.v{a}"
        assert result["outcomes"] == {a: "won", b: "beaten", c: "lost"}
        found = {v["id"]: v for v in store.variants(conn, rid)}
        assert found[a]["status"] == "promoted" and found[a]["arm_id"] == f"lab.reply_human.v{a}"
        assert found[b]["status"] == "retired" and found[c]["outcome"] == "lost"
        assert json.loads(found[a]["scores"])["delta"] == pytest.approx(1.0)
        registry = {x.id: x for x in arms.read(conn)}
        arm = registry[f"lab.reply_human.v{a}"]
        assert (arm.family, arm.status, arm.params) == ("prompt.reply_human", "active",
                                                        {"piece": "reply_human", "variant": a})
        assert arms.valid(arm.family, arm.params)
        assert registry["prompt.reply_human.control.v1"].status == "control"
        # the weekly cycle reads the new family without error (its control is known)
        cycle.evaluate(conn, week="2026-W47", cfg=LearnCfg(enabled=True, bandit=True, cycle=True),
                       now=3000.0, tz=timezone.utc, owner=1)
    finally:
        conn.close()


@pytest.mark.parametrize("cfg", [LearnCfg(freeze=True), LearnCfg(frozen_families=("prompt.reply_human",))])
def test_a_freeze_promotes_nothing(tmp_path, cfg):
    conn = open_db(tmp_path / "mind.db")
    try:
        rid, a, b, c = seed(conn)
        result = promote.promote(conn, rid, settings=cfg, now=2000.0, allow=True)
        assert result["promoted"] is None and result["outcomes"][a] == "frozen"
        assert not [x for x in arms.read(conn) if x.id.startswith("lab.")]
    finally:
        conn.close()


def test_a_budget_cut_settles_unfinished_arms_as_budget(tmp_path):
    conn = open_db(tmp_path / "mind.db")
    try:
        rid = store.create_run(conn, night="n", source="night", piece="reply_human", seed="s",
                               plan={"pairs": []}, now=1.0)
        v = store.add_variant(conn, rid, "reply_human", "t", "d", 1.0)
        result = promote.promote(conn, rid, settings=LearnCfg(), now=2.0, budget_cut=True,
                                 units_failed=True)
        assert result["outcomes"] == {v: "budget"}   # the budget's cut names it
    finally:
        conn.close()


def test_failed_units_settle_too_few_arms_as_failed(tmp_path):
    conn = open_db(tmp_path / "mind.db")
    try:
        rid = store.create_run(conn, night="n", source="night", piece="reply_human", seed="s",
                               plan={"pairs": []}, now=1.0)
        v = store.add_variant(conn, rid, "reply_human", "t", "d", 1.0)
        result = promote.promote(conn, rid, settings=LearnCfg(), now=2.0, units_failed=True)
        assert result["outcomes"] == {v: "failed"}
        assert store.variants(conn, rid)[0]["outcome"] == "failed"
    finally:
        conn.close()


def test_prompt_families_are_valid_frozen_and_never_in_the_plain_controls():
    assert "prompt.reply_human" in arms.all_families() and "prompt.reply_human" not in arms.FAMILIES
    assert set(arms.controls()) == set(arms.FAMILIES)
    assert arms.all_controls()["prompt.beat_ask"] == "prompt.beat_ask.control.v1"
    assert arms.valid("prompt.reply_human", {"piece": "reply_human", "variant": 3})
    assert not arms.valid("prompt.reply_human", {"piece": "continue", "variant": 3})
    assert not arms.valid("prompt.reply_human", {"piece": "reply_human", "variant": "3"})
    assert not arms.valid("prompt.nope", {"piece": "nope", "variant": 3})
    from leeroy_mind.learn.config import load_learn_config
    from pathlib import Path
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "mind.toml"
        p.write_bytes(b'[learn]\nenabled = true\nfrozen_families = ["prompt.reply_human"]\n')
        assert load_learn_config(p).frozen_families == ("prompt.reply_human",)


def test_the_judges_score_is_compared_with_its_own_margin():
    control = {k: dict(v, score=3.0) for k, v in rows([1] * 10).items()}
    ahead = {k: dict(v, score=3.0 + promote.THRESHOLDS["score"]) for k, v in rows([1] * 10).items()}
    sheet = promote.compare(control, ahead, key="score")
    assert sheet["outcome"] == "won" and sheet["grade"] == "score"
    assert sheet["delta"] == pytest.approx(promote.THRESHOLDS["score"])
    near = {k: dict(v, score=3.05) for k, v in rows([1] * 10).items()}
    assert promote.compare(control, near, key="score")["outcome"] == "lost"
    # the rubric column is not read for it, and a row without a score is not graded
    assert promote.compare(control, rows([3] * 10), key="score")["outcome"] == "too_few"
    assert promote.compare(rows([1] * 10), rows([3] * 10))["grade"] == "rubric"


def test_measure_only_records_the_winner_and_proposes_nothing(tmp_path):
    conn = open_db(tmp_path / "mind.db")
    try:
        rid, a, b, c = seed(conn)
        result = promote.promote(conn, rid, settings=LearnCfg(), now=2000.0)   # allow: false by default
        conn.commit()
        assert result["promoted"] is None
        assert result["outcomes"] == {a: "measure_only", b: "beaten", c: "lost"}
        found = {v["id"]: v for v in store.variants(conn, rid)}
        assert found[a]["status"] == "retired" and found[a]["arm_id"] is None
        assert json.loads(found[a]["scores"])["delta"] == pytest.approx(1.0)   # the sheet is kept
        assert not arms.read(conn)   # no arm, no control row: the weekly gate sees nothing
    finally:
        conn.close()


def test_the_summary_and_the_proposers_feedback_read_the_judges_score(tmp_path):
    conn = open_db(tmp_path / "mind.db")
    try:
        rid, a, b, c = seed(conn)
        assert store.has_score(conn)
        store.set_score(conn, rid, "control", 1, [(3.2, 4), (2.8, 4)])
        summary = store.arm_summary(store.scores(conn, rid)["control"])
        assert summary["scored"] == 2 and summary["score"] == pytest.approx(3.0)
        assert summary["score_calls"] == 8 and summary["kinds"] == {"flirt": 3.0}
        assert summary["graded"] == 10 and summary["rubric"] == 1.0   # the rubric's own columns stay
        feedback = store.feedback(conn, "reply_human")
        assert feedback["control"]["scale"] == "1-5" and feedback["control"]["rubric"] == pytest.approx(3.0)
        from leeroy_mind.lab import propose
        text = propose.prompt("reply_human", k=2, feedback=feedback)
        assert "1-5, 5 reads as a real person texting" in text and "most common grades" not in text
    finally:
        conn.close()
