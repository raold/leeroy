"""Reproducible counts and latency, with false-positive rates only for labeled input."""
from collections import Counter
from dataclasses import dataclass
import math
import statistics
import time

from .review import review
from .types import Draft


@dataclass(frozen=True)
class Case:
    draft: Draft
    # Human label: True means the original line was acceptable. None is unlabeled.
    acceptable: bool | None = None


def measure(stores, cases, *, timer=time.perf_counter):
    actions, codes, times = Counter(), Counter(), []
    clean = false_positives = labeled = missing = relevance_checked = 0
    for case in cases:
        before = timer()
        verdict = review(stores, case.draft)
        times.append((timer()-before)*1000)
        actions[verdict.action] += 1
        codes.update(verdict.codes)
        missing += any(score.code == 'missing_sources' for score in verdict.scores)
        relevance_checked += any(score.code == 'parent_relevance' for score in verdict.scores)
        labeled += case.acceptable is not None
        clean += case.acceptable is True
        false_positives += case.acceptable is True and verdict.action != 'send'
    n = len(times)
    return dict(samples=n, actions=dict(sorted(actions.items())), codes=dict(sorted(codes.items())),
        flagged_per_100=round(100*(n-actions['send'])/n,6) if n else 0.0,
        labeled=labeled, acceptable=clean, false_positives=false_positives,
        lines_with_missing_sources=missing, relevance_checked=relevance_checked,
        false_positive_rate=round(false_positives/clean,6) if clean else None,
        median_ms=round(statistics.median(times),6) if times else None,
        p95_ms=round(sorted(times)[max(0,math.ceil(.95*n)-1)],6) if times else None)
