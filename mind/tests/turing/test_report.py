"""The judge's score on a scale where 0.5 is chance."""
from leeroy_mind.turing.report import judge_rates


def test_an_always_ai_judge_scores_chance_not_its_raw_accuracy():
    # 3 members and him in each of 4 windows; it calls everyone AI
    r = judge_rates(speakers=16, correct=12, his=4, his_correct=0)
    assert r["accuracy"] == 0.75 and r["balanced"] == 0.5
    assert (r["members"], r["members_ai"], r["his_human"]) == (12, 1.0, 0.0)


def test_a_judge_that_is_always_right_or_always_wrong():
    assert judge_rates(10, 10, 3, 3)["balanced"] == 1.0
    assert judge_rates(10, 0, 3, 0)["balanced"] == 0.0


def test_one_side_missing_leaves_no_balanced_score():
    r = judge_rates(speakers=6, correct=5, his=0, his_correct=0)
    assert r["balanced"] is None and r["members_ai"] == 5 / 6 and r["his_human"] is None
    r = judge_rates(speakers=0, correct=0, his=0, his_correct=0)
    assert r == {"members": 0, "members_ai": None, "his_human": None, "balanced": None, "accuracy": None}
