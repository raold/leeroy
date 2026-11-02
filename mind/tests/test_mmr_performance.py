"""Selection equivalence and work bounds for incremental maximum marginal relevance."""
import math
import random

import pytest

from leeroy_mind.memory.recall import mmr


def previous_mmr(ids, scores, sim, k, lam=0.7):
    """Frozen pre-optimization algorithm: the independent ordering oracle."""
    pool = list(ids)
    top = max((scores[i] for i in pool), default=0.0)
    scale = top if top > 0 else 1.0
    picked = []
    while pool and len(picked) < k:
        def value(i):
            near = max((sim(i, j) for j in picked), default=0.0)
            return lam * scores[i] / scale - (1 - lam) * near
        best = max(pool, key=value)
        picked.append(best)
        pool.remove(best)
    return picked


@pytest.mark.parametrize("seed", range(16))
def test_incremental_matches_previous_for_finite_asymmetric_matrices(seed):
    rng = random.Random(seed)
    size = 1 + seed % 11
    # Repeated ids deliberately remain separate candidates, including self-similarity.
    ids = list(range(size)) + [rng.randrange(size) for _ in range(seed % 5)]
    rng.shuffle(ids)
    matrix = {(a, b): rng.uniform(-1.0, 1.0) for a in range(size) for b in range(size)}
    for scores in ({i: rng.uniform(-3.0, 3.0) for i in range(size)},
                   {i: -float(i + 1) for i in range(size)},
                   {i: 0.0 for i in range(size)},
                   {i: math.nextafter(1.0, 2.0) if i % 2 else 1.0 for i in range(size)}):
        original_ids, original_scores = list(ids), dict(scores)
        for lam in (-0.25, 0.0, 0.7, 0.75, 1.0, 1.25):
            for k in (-3, 0, 1, 2, 5, len(ids), len(ids) + 2):
                sim = lambda a, b: matrix[a, b]
                assert mmr(ids, scores, sim, k, lam) == previous_mmr(ids, scores, sim, k, lam)
        assert ids == original_ids
        assert scores == original_scores


def test_first_negative_similarity_is_not_clamped_to_zero():
    matrix = {(2, 1): -0.2, (3, 1): -0.9, (2, 3): -0.4}
    assert mmr([1, 2, 3], {1: 1, 2: 1, 3: 1}, lambda a, b: matrix[a, b], 3, 0) == [1, 3, 2]


@pytest.mark.parametrize("lam", (0.0, 0.7, 1.0))
def test_ties_and_duplicate_occurrences_stay_in_input_order(lam):
    ids = [30, 10, 30, 20, 10]
    assert mmr(ids, dict.fromkeys(ids, 0.0), lambda a, b: 0.0, 20, lam) == ids


@pytest.mark.parametrize("size,k", ((0, 5), (1, 5), (8, -1), (8, 0), (8, 1)))
def test_no_similarity_work_without_a_second_selection(size, k):
    def forbidden(a, b):
        raise AssertionError("no pair comparison is needed")
    ids = list(range(size))
    assert mmr(ids, dict.fromkeys(ids, 1.0), forbidden, k) == ids[:max(0, min(k, size))]


@pytest.mark.parametrize("size,k", ((50, 5), (100, 5), (270, 5), (8, 8), (8, 20)))
def test_pair_comparisons_grow_once_per_remaining_candidate_and_new_pick(size, k):
    ids = list(range(size))
    scores = {i: 1 / (i + 1) for i in ids}
    calls = {"old": 0, "new": 0}
    def counted(which):
        def sim(a, b):
            calls[which] += 1
            return ((a * 7 + b * 3) % 11 - 5) / 5
        return sim
    assert mmr(ids, scores, counted("new"), k) == previous_mmr(ids, scores, counted("old"), k)
    chosen = min(size, k)
    assert calls["old"] == sum(step * (size - step) for step in range(chosen))
    assert calls["new"] == sum(size - step for step in range(1, chosen))
    assert calls["new"] < calls["old"]
