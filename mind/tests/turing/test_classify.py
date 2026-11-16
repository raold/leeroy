"""The plain-Python classifier on synthetic data: indistinguishable classes score about 0.5,
separable ones near 1, and the interval brackets the estimate."""
import random

import pytest

from leeroy_mind.turing import classify


def test_auc_matches_hand_counts_with_ties_and_weights():
    assert classify.auc([0.1, 0.4, 0.35, 0.8], [0, 0, 1, 1]) == 0.75
    assert classify.auc([1, 1, 1, 1], [0, 1, 0, 1]) == 0.5
    assert classify.auc([0, 1], [1, 0]) == 0.0
    assert classify.auc([0, 1], [1, 1]) is None
    # a weight of 2 counts an item twice
    assert classify.auc([0.1, 0.5, 0.3], [0, 0, 1], [1, 1, 1]) == 0.5
    assert classify.auc([0.1, 0.5, 0.3], [0, 0, 1], [2, 1, 1]) == pytest.approx(2 / 3)


def synthetic(n, shift, *, seed, groups=60):
    rng = random.Random(seed)
    cols = [[], [], []]
    labels, group = [], []
    for i in range(2 * n):
        y = int(i >= n)
        cols[0].append(rng.gauss(shift * y, 1))
        cols[1].append(float(rng.random() < (0.3 + 0.4 * y * (shift > 0))))
        cols[2].append(rng.gauss(0, 1))  # noise
        labels.append(y)
        group.append(f"g{i % groups}")
    return cols, labels, group


def test_identical_distributions_score_about_one_half():
    cols, labels, groups = synthetic(400, 0.0, seed=1)
    r = classify.cross_validate(cols, labels, groups, seed="same")
    assert abs(r.auc - 0.5) < 0.08
    assert r.low <= 0.5 <= r.high


def test_separable_classes_score_near_one():
    cols, labels, groups = synthetic(300, 4.0, seed=2)
    r = classify.cross_validate(cols, labels, groups, seed="apart")
    assert r.auc > 0.97
    assert r.low <= r.auc <= r.high and r.low > 0.9


def test_cross_validation_is_deterministic_for_a_seed():
    cols, labels, groups = synthetic(150, 1.0, seed=3)
    a = classify.cross_validate(cols, labels, groups, seed="s")
    b = classify.cross_validate(cols, labels, groups, seed="s")
    assert (a.auc, a.low, a.high) == (b.auc, b.low, b.high)


def test_a_group_never_sits_in_two_folds():
    groups = [f"g{i // 7}" for i in range(100)]
    for labels in (None, [i % 2 for i in range(100)]):
        parts = classify.folds(groups, 5, random.Random(0), labels)
        owner = {}
        for k, rows in enumerate(parts):
            for i in rows:
                assert owner.setdefault(groups[i], k) == k
        assert sorted(i for rows in parts for i in rows) == list(range(100))


def test_folds_spread_each_class_evenly():
    # groups that hold one class each: a fold by size alone could end up all of one class
    labels = [0] * 60 + [1] * 60
    groups = [f"h{i // 6}" if i < 60 else f"m{i // 6}" for i in range(120)]
    parts = classify.folds(groups, 5, random.Random(1), labels)
    for rows in parts:
        ones = sum(labels[i] for i in rows)
        assert 9 <= ones <= 15 and 9 <= len(rows) - ones <= 15


def test_no_signal_is_not_pulled_under_one_half():
    # with no signal, the folds' averaged AUC centres on one half (no cross-validation drag)
    aucs = []
    for seed in range(8):
        cols, labels, groups = synthetic(120, 0.0, seed=10 + seed, groups=40)
        aucs.append(classify.cross_validate(cols, labels, groups, seed=f"n{seed}").auc)
    assert abs(sum(aucs) / len(aucs) - 0.5) < 0.04


def test_fit_learns_the_direction_of_a_feature():
    cols, labels, _ = synthetic(200, 2.0, seed=4)
    model = classify.fit(cols, labels)
    assert model.coef[0] > 1.0
    assert abs(model.coef[2]) < abs(model.coef[0]) / 4


def test_perfect_separation_stays_finite():
    cols = [[0.0] * 50 + [1.0] * 50]
    labels = [0] * 50 + [1] * 50
    model = classify.fit(cols, labels)
    assert all(abs(c) < 100 for c in model.coef)
    assert classify.auc(model.scores(cols, list(range(100))), labels) == 1.0


def test_too_few_groups_give_no_estimate():
    cols, labels, _ = synthetic(20, 1.0, seed=5)
    r = classify.cross_validate(cols, labels, ["one"] * 40, seed="x")
    assert r.auc is None and r.groups == 1 and r.folds == 0
