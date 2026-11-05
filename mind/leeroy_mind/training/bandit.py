"""Pure top-two Thompson sampling with ordered conservative stopping rules.

Larger observations are better. Callers must supply comparable observations of
one known metric/basis; failure observations are negated failures per member-pull.
This module neither selects eligible game records nor applies a tactic.
"""

from collections.abc import Sequence
from dataclasses import dataclass
import math
from statistics import fmean
from typing import Literal


@dataclass(frozen=True)
class Arm:
    id: int
    xs: tuple[float, ...]
    champion: bool


@dataclass(frozen=True)
class Verdict:
    action: Literal["continue", "promote", "retire", "give_up"]
    arm: int | None
    why: str


def _finite(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def _positive(value):
    return _finite(value) and value > 0


def posterior(xs, *, mu0=0.0, kappa0=1.0, alpha0=1.0, beta0=1.0):
    """Normal-Gamma posterior (mean, mean precision, shape, rate)."""
    xs = tuple(xs)
    if not (_finite(mu0) and all(_positive(v) for v in (kappa0, alpha0, beta0))
            and all(_finite(x) for x in xs)):
        raise ValueError("observations and prior must be finite, with positive precision, shape and rate")
    n = len(xs)
    if not n:
        return mu0, kappa0, alpha0, beta0
    try:
        mean = fmean(xs)
        kappa = kappa0 + n
        mu = (kappa0 / kappa) * mu0 + (n / kappa) * mean
        alpha = alpha0 + n / 2
        beta = beta0 + math.fsum((x - mean) ** 2 for x in xs) / 2
        beta += (kappa0 / kappa) * n * (mean - mu0) ** 2 / 2
    except (OverflowError, ZeroDivisionError) as error:
        raise ValueError("posterior exceeds numeric range") from error
    if not (_finite(mu) and all(_positive(v) for v in (kappa, alpha, beta))):
        raise ValueError("posterior exceeds numeric range")
    return mu, kappa, alpha, beta


def draw(post, rng):
    mu, kappa, alpha, beta = post
    if not (_finite(mu) and all(_positive(v) for v in (kappa, alpha, beta))):
        raise ValueError("invalid posterior")
    tau = rng.gammavariate(alpha, 1 / beta)
    precision = kappa * tau
    if not _positive(precision):
        raise ValueError("sample precision exceeds numeric range")
    value = rng.gauss(mu, 1 / math.sqrt(precision))
    if not _finite(value):
        raise ValueError("sample exceeds numeric range")
    return value


def _arms(arms):
    values = tuple(arms)
    if (not values or any(not isinstance(a, Arm) or type(a.id) is not int or type(a.champion) is not bool
                          for a in values) or len({a.id for a in values}) != len(values)):
        raise ValueError("arms must have distinct integer identities and Boolean champion flags")
    # Stable tie handling and random draw order are independent of DB row order.
    values = tuple(sorted(values, key=lambda a: a.id))
    return values, {a.id: posterior(a.xs) for a in values}


def _samples(n):
    if type(n) is not int or n <= 0:
        raise ValueError("sample count must be a positive integer")


def _margin(value):
    if not _finite(value) or value < 0:
        raise ValueError("margin must be finite and nonnegative")


def _leader(posts, rng):
    return max(posts, key=lambda ident: draw(posts[ident], rng))


def p_best(arms, rng, n=2000):
    _samples(n)
    _, posts = _arms(arms)
    counts = dict.fromkeys(posts, 0)
    for _ in range(n):
        counts[_leader(posts, rng)] += 1
    return {ident: count / n for ident, count in counts.items()}


def top_two(arms, rng, beta=0.5):
    if not _finite(beta) or not 0 <= beta <= 1:
        raise ValueError("leader probability must be between zero and one")
    _, posts = _arms(arms)
    leader = _leader(posts, rng)
    if len(posts) == 1 or rng.random() < beta:
        return leader
    for _ in range(50):
        challenger = _leader(posts, rng)
        if challenger != leader:
            return challenger
    return max((ident for ident in posts if ident != leader), key=lambda ident: posts[ident][0])


def below(arm, champ, margin, rng, n=2000):
    _samples(n)
    _margin(margin)
    _, posts = _arms((arm, champ))
    return sum(draw(posts[arm.id], rng) < draw(posts[champ.id], rng) - margin
               for _ in range(n)) / n


def decide(arms, rng, *, margin, wipes: dict[int, int], total_pulls: int,
           fail_arms: Sequence[Arm] = (), fail_margin=0.5):
    values, _ = _arms(arms)
    _margin(margin)
    _margin(fail_margin)
    champions = [a for a in values if a.champion]
    if len(champions) != 1:
        raise ValueError("exactly one champion is required")
    if (type(total_pulls) is not int or total_pulls < 0
            or any(type(key) is not int or type(count) is not int or count < 0 for key, count in wipes.items())):
        raise ValueError("pull and wipe counts must be nonnegative integers")
    champ = champions[0]
    candidates = [a for a in values if not a.champion]
    fail_values = tuple(fail_arms)
    fail_by_id = {}
    if fail_values:
        fail_values, _ = _arms(fail_values)
        fail_by_id = {a.id: a for a in fail_values}
        if ({a.id: a.champion for a in fail_values} != {a.id: a.champion for a in values}):
            raise ValueError("failure observations must identify the same arms and champion")
    # Each safety rule checks ALL candidates before considering any promotion.
    for arm in candidates:
        if wipes.get(arm.id, 0) >= 2:
            return Verdict("retire", arm.id, "two_wipes")
    for arm in candidates:
        if below(arm, champ, margin, rng) >= .9:
            return Verdict("retire", arm.id, "conservative")
    if fail_by_id:
        for arm in candidates:
            if below(fail_by_id[arm.id], fail_by_id[champ.id], fail_margin, rng) >= .9:
                return Verdict("retire", arm.id, "conservative_fail")
    probabilities = p_best(values, rng)
    for arm in candidates:
        if probabilities[arm.id] >= .95 and len(arm.xs) >= 8 and len(champ.xs) >= 8:
            return Verdict("promote", arm.id, "best")
    for arm in candidates:
        if probabilities[arm.id] <= .05 and len(arm.xs) >= 6:
            return Verdict("retire", arm.id, "unlikely_best")
    if total_pulls >= 30:
        return Verdict("give_up", None, "pull_limit")
    return Verdict("continue", None, "insufficient_evidence")
