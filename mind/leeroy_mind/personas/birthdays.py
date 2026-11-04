"""Birthdays are drawn in code: a model asked for one keeps picking the same few dates."""
from __future__ import annotations

import random
from collections import Counter
from collections.abc import Iterable
from datetime import date, timedelta

# 2025 is not a leap year, so there is no 02-29.
DATES = tuple((date(2025, 1, 1) + timedelta(days=i)).strftime("%m-%d") for i in range(365))


def draw_birthday(taken: Iterable[str], rng: random.Random) -> str:
    """A date (MM-DD, never 02-29) drawn uniformly from those the fewest `taken` birthdays have."""
    counts = Counter(taken)
    fewest = min(counts[d] for d in DATES)
    return rng.choice([d for d in DATES if counts[d] == fewest])
