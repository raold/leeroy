"""Death, observed mechanic and elapsed-time scores; never inferred DPS.

Statistical consumers accept ledger rows (mapping objects, including sqlite3.Row).
Unknown historical covariates are not zeros: adjustment falls back to raw scores.
"""

from __future__ import annotations

import json
import math
import random
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from leeroy_mind.wow import is_raid_fight

from . import store

if TYPE_CHECKING:
    from .checks import CheckResult


COVARIATES = ("ilvl", "healers", "drunk", "flask_skips", "human_in_raid", "assisted")
SCORE_BASES = frozenset({"deaths_time", "mechanics_deaths_time"})


def _finite(value):
    try:
        return isinstance(value, (int, float)) and math.isfinite(value)
    except OverflowError:
        return False


def pull_score(outcome: str, deaths: int, failed_mechanics: int, kill_s: float,
               median_kill_s: float | None) -> float | None:
    if outcome == "wiped":
        return -100.0
    if outcome != "killed":
        return None
    median = kill_s if median_kill_s is None else median_kill_s
    if (not all(_finite(v) for v in (deaths, failed_mechanics, kill_s, median))
            or deaths < 0 or failed_mechanics < 0 or kill_s <= 0 or median <= 0):
        return None
    value = -8 * deaths - 2 * failed_mechanics - 30 * (kill_s / median - 1)
    return float(value) if _finite(value) else None


def _eligible(row):
    return (row.get("complete") == 1 and row.get("scoreable") == 1
            and row.get("outcome") in {"killed", "wiped"}
            and is_raid_fight(row.get("map"), row.get("boss_id")))


def _duration(row):
    return (row["end_ms"] - row["start_ms"]) / 1000.0


def _scored_rows(rows):
    result = []
    for value in rows:
        row = dict(value)
        if (not _eligible(row) or not _finite(row.get("score"))
                or row.get("score_basis") not in SCORE_BASES):
            continue
        if row["outcome"] == "killed" and (not _finite(_duration(row)) or _duration(row) <= 0):
            continue
        result.append(row)
    return result


def _context_rows(conn, context=None):
    # Arm membership is evidence of the comparison role at observation time.
    return conn.execute("SELECT p.*, COALESCE(a.champion,0) AS champion FROM tg_pull p "
                        "LEFT JOIN tg_arm a ON a.id=p.arm_id WHERE (? IS NULL OR p.context=?) "
                        "ORDER BY p.start_ms,p.id", (context, context)).fetchall()


@dataclass(frozen=True)
class ContextStats:
    n: int
    kills: int
    median_kill_s: float | None
    mean: float
    sd: float


def context_stats(rows) -> ContextStats:
    rows = _scored_rows(rows)
    if len({r["score_basis"] for r in rows}) > 1:
        raise ValueError("context statistics require one score basis")
    champion = [r for r in rows if r.get("champion") == 1]
    if len(champion) >= 3:
        rows = champion
    scores = [r["score"] for r in rows]
    durations = [_duration(r) for r in rows if r["outcome"] == "killed"]
    return ContextStats(len(rows), len(durations), statistics.median(durations) if durations else None,
                        statistics.fmean(scores) if scores else 0.0,
                        max(10.0, statistics.stdev(scores)) if len(scores) > 1 else 10.0)


def standardize(score, stats) -> float:
    return (score - stats.mean) / max(10.0, stats.sd)


def is_farm(outcomes: Sequence[str]) -> bool:
    recent = list(outcomes[-10:])
    return (len(recent) >= 3 and recent[-3:] == ["killed"] * 3
            and recent.count("killed") / len(recent) >= 0.8)


def _solve(matrix, target):
    """Small, scaled normal system with partial pivoting; None if rank deficient."""
    n = len(target)
    augmented = [list(row) + [value] for row, value in zip(matrix, target)]
    scale = max((abs(value) for row in matrix for value in row), default=1.0)
    for col in range(n):
        pivot = max(range(col, n), key=lambda i: abs(augmented[i][col]))
        if abs(augmented[pivot][col]) <= scale * 1e-10:
            return None
        augmented[col], augmented[pivot] = augmented[pivot], augmented[col]
        divisor = augmented[col][col]
        augmented[col] = [v / divisor for v in augmented[col]]
        for i in range(n):
            if i != col:
                factor = augmented[i][col]
                augmented[i] = [a - factor * b for a, b in zip(augmented[i], augmented[col])]
    solution = [row[-1] for row in augmented]
    return solution if all(_finite(v) for v in solution) else None


def adjusted(rows) -> dict[str, float]:
    rows = sorted(_scored_rows(rows), key=lambda row: row["id"])
    raw = {row["id"]: float(row["score"]) for row in rows}
    if len(rows) < 20 or len({row["score_basis"] for row in rows}) > 1:
        return raw
    covariates = []
    for row in rows:
        value = row.get("covariates")
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (ValueError, TypeError):
                return raw
        if not isinstance(value, dict) or any(not _finite(value.get(k)) for k in COVARIATES):
            return raw
        covariates.append([float(value[k]) for k in COVARIATES])
    columns = []
    for values in zip(*covariates):
        if min(values) == max(values):
            continue
        mean = statistics.fmean(values)
        scale = max(abs(v - mean) for v in values)
        columns.append([(v - mean) / scale for v in values])
    if not columns:
        return raw
    scores = [row["score"] for row in rows]
    mean = statistics.fmean(scores)
    centered = [s - mean for s in scores]
    matrix = [[math.fsum(a * b for a, b in zip(x, y)) for y in columns] for x in columns]
    target = [math.fsum(a * b for a, b in zip(x, centered)) for x in columns]
    coefficients = _solve(matrix, target)
    if coefficients is None:
        return raw
    result = {row["id"]: row["score"] - math.fsum(b * column[i]
              for b, column in zip(coefficients, columns)) for i, row in enumerate(rows)}
    return result if all(_finite(value) for value in result.values()) else raw


def _quantile(values, fraction):
    pos = (len(values) - 1) * fraction
    lower = math.floor(pos)
    upper = math.ceil(pos)
    return values[lower] + (values[upper] - values[lower]) * (pos - lower)


def bootstrap_diff(before, after, *, seed: str, n=2000, level=0.9) -> tuple[float, float, float]:
    before, after = tuple(before), tuple(after)
    if (not before or not after or not all(_finite(v) for v in (*before, *after))
            or type(n) is not int or n < 1 or not _finite(level) or not 0 < level < 1):
        raise ValueError("bootstrap requires finite nonempty samples and valid n/level")
    rng = random.Random(seed)
    draws = sorted(statistics.fmean(rng.choices(after, k=len(after)))
                   - statistics.fmean(rng.choices(before, k=len(before))) for _ in range(n))
    tail = (1 - level) / 2
    return (statistics.fmean(after) - statistics.fmean(before),
            _quantile(draws, tail), _quantile(draws, 1 - tail))


async def score_pull(ports, rec, ctx, result: CheckResult) -> float | None:
    """Persist checks then score a committed eligible pull, respecting ledger vetoes.

    Deaths and duration remain observable when damage/spell attribution is unknown.
    CheckResult supplies only failures supported by its own evidence checks.
    """
    conn = ports.conn
    saved = conn.execute("SELECT * FROM tg_pull WHERE id=?", (rec.header.id,)).fetchone()
    if saved is None:
        raise ValueError("cannot score an uncommitted pull")
    row = dict(saved)
    if not rec.complete or not _eligible(row):
        return None
    version = getattr(result, "version", 1)
    if (row["checks_version"] >= version and row["score"] is not None
            and row["score_basis"] in SCORE_BASES):
        return row["score"]
    store.set_checks(conn, row["id"], result.failures, result.causes,
                     deaths=row["deaths"], failed=result.failed_mechanics, version=version,
                     complete=getattr(result, "complete", False), coverage=getattr(result, "coverage", ()))
    # Read back the committed check version: stale callbacks cannot overwrite a
    # newer result. Exclude this pull and later observations from its reference.
    row = dict(conn.execute("SELECT * FROM tg_pull WHERE id=?", (row["id"],)).fetchone())
    basis = "mechanics_deaths_time" if row["checks_complete"] else "deaths_time"
    history = [dict(r) for r in _context_rows(conn, row["context"])
               if r["start_ms"] < row["start_ms"] and r["received"] <= row["received"]
               and r["score_basis"] == basis]
    median = context_stats(history).median_kill_s
    failed = row["failed_mechanics"] if row["checks_complete"] else 0
    score = pull_score(row["outcome"], row["deaths"], failed, _duration(row), median)
    store.set_score(conn, row["id"], score, basis=basis)
    return score
