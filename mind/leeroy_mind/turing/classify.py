"""A small, dependency-free classifier: L2-regularised logistic regression by Newton's method on
standardised columns, scored by grouped k-fold cross-validation (a conversation never sits in
two folds), repeated over fresh fold draws, with a cluster bootstrap over those groups for the
AUC's interval. The mind's venv has no numpy; columns are plain lists and the sums run through
math.sumprod, so a family of a few thousand lines takes about a second.

Labels: 1 for a member's line, 0 for his. An AUC of 0.5 means the classifier cannot tell them
apart; 1.0 means it always can.

The AUC is the folds' own AUCs averaged (weighted by each fold's member-by-his pairs), not the
AUC of every fold's scores pooled: a fold's intercept leans against its own class balance, so
pooling would pull a no-signal AUC under 0.5. Folds also keep each class evenly spread. The
interval resamples whole groups over the fitted folds; it does not refit, so it shows the
sampling spread of the held-out lines, not the spread from refitting."""
import math
import random
from dataclasses import dataclass
from operator import mul
from typing import Sequence

L2 = 1.0          # ridge on the standardised coefficients (never the intercept)
ITERATIONS = 30
TOLERANCE = 1e-7
FOLDS = 5
REPEATS = 3       # fold draws averaged
BOOT = 200        # cluster bootstrap resamples for the interval
LEVEL = 0.95


def _ordered(order, scores, labels, weights) -> tuple[float, float] | None:
    """(AUC, its pair weight) over items already sorted by score: P(a member's score > his),
    ties counting half (Mann-Whitney), each item counted `weights[i]` times."""
    pos = neg = total = below = 0.0
    i = 0
    while i < len(order):
        j, tp, tn = i, 0.0, 0.0
        while j < len(order) and scores[order[j]] == scores[order[i]]:
            w = weights[order[j]]
            if labels[order[j]]:
                tp += w
            else:
                tn += w
            j += 1
        total += tp * (below + tn / 2)
        below += tn
        pos += tp
        neg += tn
        i = j
    if pos <= 0 or neg <= 0:
        return None
    return total / (pos * neg), pos * neg


def auc(scores: Sequence[float], labels: Sequence[int],
        weights: Sequence[float] | None = None) -> float | None:
    """P(a member's score > his), with optional weights per item (a bootstrap's repeat
    counts). None without both classes."""
    weights = [1.0] * len(scores) if weights is None else weights
    got = _ordered(sorted(range(len(scores)), key=scores.__getitem__), scores, labels, weights)
    return None if got is None else got[0]


def _standardise(columns, rows):
    """Each column's mean and spread over `rows`; a constant column scales 1."""
    stats = []
    for col in columns:
        values = [col[i] for i in rows]
        n = len(values)
        mean = math.fsum(values) / n if n else 0.0
        var = math.fsum((v - mean) ** 2 for v in values) / n if n else 0.0
        stats.append((mean, math.sqrt(var) if var > 1e-12 else 1.0))
    return stats


def _solve(a: list[list[float]], b: list[float]) -> list[float]:
    """Gaussian elimination with partial pivoting (the ridge keeps it non-singular)."""
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(m[r][c]))
        m[c], m[p] = m[p], m[c]
        piv = m[c][c] if abs(m[c][c]) > 1e-12 else 1e-12
        for r in range(c + 1, n):
            f = m[r][c] / piv
            if f:
                row, top = m[r], m[c]
                for k in range(c, n + 1):
                    row[k] -= f * top[k]
    x = [0.0] * n
    for r in range(n - 1, -1, -1):
        piv = m[r][r] if abs(m[r][r]) > 1e-12 else 1e-12
        x[r] = (m[r][n] - math.fsum(m[r][k] * x[k] for k in range(r + 1, n))) / piv
    return x


def _sigmoid(z: float) -> float:
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-min(z, 50.0)))
    e = math.exp(max(z, -50.0))
    return e / (1.0 + e)


@dataclass(frozen=True)
class Model:
    intercept: float
    coef: tuple[float, ...]  # per standardised column
    stats: tuple[tuple[float, float], ...]

    def scores(self, columns, rows) -> list[float]:
        z = [self.intercept] * len(rows)
        for col, (m, s), b in zip(columns, self.stats, self.coef):
            if b:
                z = [zi + b * (col[i] - m) / s for zi, i in zip(z, rows)]
        return z


def fit(columns: Sequence[Sequence[float]], labels: Sequence[int], rows: Sequence[int] | None = None,
        *, l2: float = L2) -> Model:
    """Logistic regression on `rows` (all by default) of the column lists."""
    rows = list(range(len(labels))) if rows is None else list(rows)
    stats = _standardise(columns, rows)
    x = [[(col[i] - m) / s for i in rows] for col, (m, s) in zip(columns, stats)]
    y = [float(labels[i]) for i in rows]
    d = len(x)
    beta = [0.0] * (d + 1)  # intercept first
    for _ in range(ITERATIONS):
        z = [beta[0]] * len(rows)
        for j in range(d):
            if beta[j + 1]:
                bj = beta[j + 1]
                z = [zi + bj * xi for zi, xi in zip(z, x[j])]
        p = [_sigmoid(v) for v in z]
        r = [yi - pi for yi, pi in zip(y, p)]
        w = [pi * (1 - pi) for pi in p]
        grad = [math.fsum(r)] + [math.sumprod(r, x[j]) - l2 * beta[j + 1] for j in range(d)]
        wx = [list(map(mul, w, x[j])) for j in range(d)]
        h = [[0.0] * (d + 1) for _ in range(d + 1)]
        h[0][0] = math.fsum(w) + 1e-9
        for j in range(d):
            h[0][j + 1] = h[j + 1][0] = math.fsum(wx[j])
            for k in range(j, d):
                h[j + 1][k + 1] = h[k + 1][j + 1] = math.sumprod(wx[j], x[k])
            h[j + 1][j + 1] += l2
        step = _solve(h, grad)
        beta = [b + s for b, s in zip(beta, step)]
        if max(abs(s) for s in step) < TOLERANCE:
            break
    return Model(beta[0], tuple(beta[1:]), tuple(stats))


def folds(groups: Sequence[str], k: int, rng: random.Random,
          labels: Sequence[int] | None = None) -> list[list[int]]:
    """Rows by fold: whole groups, shuffled, largest first, each to the fold where both classes
    stay most evenly spread (with `labels`), else to the fold with the fewest rows."""
    by_group: dict[str, list[int]] = {}
    for i, g in enumerate(groups):
        by_group.setdefault(g, []).append(i)
    keys = sorted(by_group)
    rng.shuffle(keys)
    k = max(1, min(k, len(keys)))
    out: list[list[int]] = [[] for _ in range(k)]
    order = sorted(keys, key=lambda g: -len(by_group[g]))  # sorted keeps shuffled ties
    if labels is None:
        for g in order:
            min(out, key=len).extend(by_group[g])
        return out
    totals = [max(1, sum(1 for y in labels if y == c)) for c in (0, 1)]
    filled = [[0, 0] for _ in range(k)]
    for g in order:
        rows = by_group[g]
        ones = sum(labels[i] for i in rows)
        add = (len(rows) - ones, ones)
        f = min(range(k), key=lambda f: (max((filled[f][c] + add[c]) / totals[c] for c in (0, 1)),
                                        len(out[f])))
        out[f].extend(rows)
        filled[f][0] += add[0]
        filled[f][1] += add[1]
    return out


@dataclass(frozen=True)
class Result:
    auc: float | None
    low: float | None
    high: float | None
    groups: int
    folds: int  # folds over all repeats that were scored (both classes in and out)


def _mean(parts, scores, labels, weights) -> float | None:
    """The folds' AUCs, each weighted by its member-by-his pairs."""
    num = den = 0.0
    for order in parts:
        got = _ordered(order, scores, labels, weights)
        if got is not None:
            num += got[0] * got[1]
            den += got[1]
    return num / den if den else None


def cross_validate(columns, labels, groups, *, seed: str, k: int = FOLDS,
                   repeats: int = REPEATS, boot: int = BOOT) -> Result:
    """Grouped k-fold over `repeats` fold draws: the folds' held-out AUCs averaged, and a
    cluster-bootstrap interval of that average."""
    rng = random.Random(seed)
    n = len(labels)
    n_groups = len(set(groups))
    if n_groups < 2:
        return Result(None, None, None, n_groups, 0)
    # One entry per held-out row per draw: its score, its label and the row it is.
    scores: list[float] = []
    ys: list[int] = []
    rows_of: list[int] = []
    parts: list[list[int]] = []  # each scored fold's entries, sorted by score
    for _ in range(max(1, repeats)):
        for test in folds(groups, k, rng, labels):
            held = set(test)
            train = [i for i in range(n) if i not in held]
            if len({labels[i] for i in train}) < 2 or len({labels[i] for i in test}) < 2:
                continue
            base = len(scores)
            scores += fit(columns, labels, train).scores(columns, test)
            ys += [labels[i] for i in test]
            rows_of += test
            parts.append(sorted(range(base, len(scores)), key=scores.__getitem__))
    if not parts:
        return Result(None, None, None, n_groups, 0)
    point = _mean(parts, scores, ys, [1.0] * len(scores))
    low, high = _interval(parts, scores, ys, rows_of, groups, rng=rng, boot=boot)
    return Result(point, low, high, n_groups, len(parts))


def _interval(parts, scores, ys, rows_of, groups, *, rng: random.Random, boot: int,
              level: float = LEVEL) -> tuple[float | None, float | None]:
    """The percentile interval of the averaged AUC over `boot` resamples of whole groups."""
    by_group: dict[str, list[int]] = {}
    for i, g in enumerate(groups):
        by_group.setdefault(g, []).append(i)
    keys = sorted(by_group)
    if len(keys) < 2 or boot <= 0:
        return None, None
    values = []
    for _ in range(boot):
        counts = [0.0] * len(groups)
        for g in (rng.choice(keys) for _ in keys):
            for i in by_group[g]:
                counts[i] += 1
        a = _mean(parts, scores, ys, [counts[r] for r in rows_of])
        if a is not None:
            values.append(a)
    if len(values) < boot // 2:
        return None, None
    values.sort()
    tail = (1 - level) / 2
    pick = lambda q: values[min(len(values) - 1, max(0, round(q * (len(values) - 1))))]
    return pick(tail), pick(1 - tail)
