"""Fitting a prompt to the text encoders' window: the mind sends phrases with a priority
(0 required, 9 first to go) and the worker keeps as many as the token limit allows."""
from collections.abc import Callable, Sequence


class PromptTooLong(Exception):
    """The required (priority 0) phrases alone are over the limit."""


def fit(phrases: Sequence[tuple[str, int]], count: Callable[[str], int],
        limit: int = 75) -> tuple[str, tuple[str, ...]]:
    """(the kept phrases joined by ", " in their original order, the dropped phrases).
    Every priority-0 phrase stays; then priority by priority from 1, phrase by phrase in
    position order, a phrase stays if the joined text still counts at most `limit`."""
    keep = {i for i, (_, p) in enumerate(phrases) if p == 0}

    def joined(idx) -> str:
        return ", ".join(phrases[i][0] for i in sorted(idx))

    if count(joined(keep)) > limit:
        raise PromptTooLong(f"the required phrases count more than {limit} tokens")
    for priority in sorted({p for _, p in phrases if p > 0}):
        for i, (_, p) in enumerate(phrases):
            if p == priority and count(joined(keep | {i})) <= limit:
                keep.add(i)
    dropped = tuple(t for i, (t, _) in enumerate(phrases) if i not in keep)
    return joined(keep), dropped
