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
