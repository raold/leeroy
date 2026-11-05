"""Pure retrieval arithmetic, compatibility imports, and input ownership."""

import math

import pytest

from leeroy_mind.memory.recall import mmr as recall_mmr
from leeroy_mind.retrieval import cosine, mmr, vector_or_word_similarity
from leeroy_mind.storage.rows import cosine as rows_cosine
from leeroy_mind.text import word_jaccard


def test_compatibility_imports_and_cosine_arithmetic():
    assert recall_mmr is mmr
    assert rows_cosine is cosine
    assert cosine([], [1.0]) == 0.0
    assert cosine([0.0, 0.0], [1.0, 2.0]) == 0.0
    assert cosine([1.0, 0.0], [-1.0, 0.0]) == -1.0
    # The dot uses zip while each norm uses its entire vector.
    assert cosine([3.0, 4.0], [3.0]) == 9.0 / (5.0 * 3.0)
    a, b = [0.1, 0.2, -0.3], [0.4, -0.5, 0.6]
    expected = sum(x * y for x, y in zip(a, b)) / (
        math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))
    assert cosine(a, b) == expected


def test_similarity_owns_vectors_and_texts_and_uses_existing_fallback():
    vectors = {1: [1.0, 0.0], 2: [-1.0, 0.0], 3: [0.0, 0.0]}
    texts = {1: "Raid RAID! boss", 2: "raid boss", 3: "", 4: "Boss down", 5: ""}
    sim = vector_or_word_similarity(vectors, texts)
    vectors[1][0] = -1.0
    vectors[2] = [1.0, 0.0]
    vectors[4] = [1.0, 0.0]
    texts[1] = "changed"
    texts.clear()
    assert sim(1, 2) == -1.0
    assert sim(1, 3) == 0.0
    vectors[1][1] = 77.0
    assert sim(1, 2) == -1.0  # cached norms and dot still use the original snapshot
    assert sim(1, 4) == word_jaccard("Raid RAID! boss", "Boss down") == 1 / 3
    assert sim(3, 5) == 0.0
    with pytest.raises(KeyError):
        sim(1, 999)


def test_mmr_keeps_ties_duplicates_negative_similarity_and_empty_edges():
    ids = [1, 2, 1, 3]
    assert mmr(ids, {1: 0.0, 2: 0.0, 3: 0.0}, lambda a, b: 0.0, 9) == ids
    assert mmr([], {}, lambda a, b: 0.0, 3) == []
    assert mmr(ids, {1: 0.0, 2: 0.0, 3: 0.0}, lambda a, b: 0.0, 0) == []
    matrix = {(2, 1): -0.2, (3, 1): -0.9, (2, 3): -0.4}
    assert mmr([1, 2, 3], {1: 1, 2: 1, 3: 1}, lambda a, b: matrix[a, b], 3, 0) == [1, 3, 2]


def test_cached_similarity_matches_cosine_exactly_across_repeated_pairs():
    vectors = {1: [0.1, -0.2, 0.3], 2: [-0.4, 0.5],
               3: [0.0, 0.0, 0.0], 4: [], 5: [2.0, -3.0, 7.0, 0.01]}
    sim = vector_or_word_similarity(vectors, {})
    for _ in range(2):
        for a in vectors:
            for b in vectors:
                assert sim(a, b) == cosine(vectors[a], vectors[b])
    ids = [1, 2, 5, 3, 1]
    scores = {1: 0.9, 2: 0.8, 3: 0.4, 5: 0.7}
    for k in (0, 1, 3, 9):
        assert mmr(ids, scores, sim, k) == mmr(
            ids, scores, lambda a, b: cosine(vectors[a], vectors[b]), k)


def test_norms_are_lazy_request_local_and_dot_is_computed_first(monkeypatch):
    import leeroy_mind.retrieval as retrieval

    calls = []
    real_sqrt = math.sqrt

    def counted_sqrt(value):
        calls.append(value)
        return real_sqrt(value)

    monkeypatch.setattr(retrieval.math, "sqrt", counted_sqrt)
    vectors = {1: [3.0, 4.0], 2: [0.0, 0.0], 3: [object()]}
    sim = vector_or_word_similarity(vectors, {1: "one", 4: "one"})
    assert mmr([1, 3], {1: 1.0, 3: 0.5}, sim, 0) == []
    assert mmr([1, 3], {1: 1.0, 3: 0.5}, sim, 1) == [1]
    assert sim(1, 4) == 1.0
    assert calls == []
    with pytest.raises(TypeError):
        sim(1, 3)  # dot fails before either norm is evaluated
    assert calls == []
    assert sim(1, 2) == sim(2, 1) == 0.0
    assert calls == [25.0, 0.0]  # zero norms are cached too
    other = vector_or_word_similarity(vectors, {})
    assert other(1, 2) == 0.0
    assert calls == [25.0, 0.0, 25.0, 0.0]


def test_zero_first_norm_does_not_skip_invalid_second_norm(monkeypatch):
    import leeroy_mind.retrieval as retrieval

    seen = []
    real_sqrt = math.sqrt

    def counted_sqrt(value):
        seen.append(value)
        return real_sqrt(value)

    monkeypatch.setattr(retrieval.math, "sqrt", counted_sqrt)
    sim = vector_or_word_similarity({1: [], 2: [object()]}, {})
    with pytest.raises(TypeError):
        sim(1, 2)  # empty zip permits dot; second norm is still evaluated
    assert seen == [0]


def test_cached_similarity_is_bit_exact_at_the_real_dimension():
    """The norm cache at VEC_DIM, the size of every stored memory and library vector, over a
    candidate set like a broad search's: every value and every MMR order is cosine's."""
    import random

    from leeroy_mind.storage.db import VEC_DIM

    rng = random.Random(512)
    vectors = {i: [rng.uniform(-1.0, 1.0) for _ in range(VEC_DIM)] for i in range(60)}
    vectors[60] = [0.0] * VEC_DIM
    sim = vector_or_word_similarity(vectors, {})
    for a in vectors:
        for b in (0, 7, 59, 60, a):
            assert sim(a, b).hex() == cosine(vectors[a], vectors[b]).hex()
    ids = list(vectors)
    scores = {i: rng.random() for i in ids}
    for k in (0, 1, 5, 12):
        assert mmr(ids, scores, vector_or_word_similarity(vectors, {}), k) == mmr(
            ids, scores, lambda a, b: cosine(vectors[a], vectors[b]), k)
