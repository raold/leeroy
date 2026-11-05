"""Pure similarity and ranking operations over retrieval candidate data."""

import math
from collections.abc import Callable, Mapping, Sequence
from types import MappingProxyType

from leeroy_mind.text import word_jaccard


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def vector_or_word_similarity(
    vectors: Mapping[int, Sequence[float]], texts: Mapping[int, str]
) -> Callable[[int, int], float]:
    """Snapshot candidate data and return its vector-or-word similarity callable."""
    vecs = MappingProxyType({i: tuple(v) for i, v in vectors.items()})
    words = MappingProxyType(dict(texts))
    norms: dict[int, float] = {}

    def norm(i: int) -> float:
        if i not in norms:
            norms[i] = math.sqrt(sum(x * x for x in vecs[i]))
        return norms[i]

    def sim(a: int, b: int) -> float:
        if a in vecs and b in vecs:
            dot = sum(x * y for x, y in zip(vecs[a], vecs[b]))
            na = norm(a)
            nb = norm(b)
            if na == 0 or nb == 0:
                return 0.0
            return dot / (na * nb)
        return word_jaccard(words[a], words[b])

    return sim


def mmr(ids: Sequence[int], scores: dict[int, float], sim: Callable[[int, int], float], k: int,
        lam: float = 0.7) -> list[int]:
    """Pick k ids, each maximising lam * score - (1 - lam) * (closest similarity to a pick).
    Scores are scaled by the largest so the penalty is comparable at any score range.
    Retain each candidate's maximum similarity: only the newest pick adds information."""
    pool = list(ids)
    nearest = [0.0] * len(pool)  # per occurrence, so repeated ids retain their original order
    top = max((scores[i] for i in pool), default=0.0)
    scale = top if top > 0 else 1.0
    picked: list[int] = []
    while pool and len(picked) < k:
        def value(index: int) -> float:
            return lam * scores[pool[index]] / scale - (1 - lam) * nearest[index]
        best_index = max(range(len(pool)), key=value)
        best = pool.pop(best_index)
        nearest.pop(best_index)
        picked.append(best)
        if len(picked) < k:
            for index, candidate in enumerate(pool):
                similarity = sim(candidate, best)
                # Zero is the empty-set default, not a floor for negative similarities.
                nearest[index] = (similarity if len(picked) == 1
                                  else max(nearest[index], similarity))
    return picked
