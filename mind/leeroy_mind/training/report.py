"""Reproducible weekly observations, with unevidenced learning milestones false."""

import json
import statistics
from collections import defaultdict
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone

from leeroy_mind.wow import boss_name

from .score import _context_rows, _duration, _finite, _scored_rows, bootstrap_diff, is_farm


BASELINE_SECONDS = 28 * 86400


def _meta(conn, key):
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    try:
        return json.loads(row[0], parse_constant=_invalid_constant) if row else None
    except (ValueError, TypeError):
        return None


def _invalid_constant(value):
    raise ValueError("nonfinite metadata")


def _mean_interval(values, seed):
    if not values:
        return None, None
    mean, low, high = bootstrap_diff([0], values, seed=seed)
    return mean, [low, high]


def training_section(conn, week: str, *, now: float, names: Callable[[int], str]) -> dict:
    """Report UTC ISO-week pulls against a disjoint first-28-receipt-day baseline.

    Pull time chooses the reporting week. Receipt time bounds the baseline and
    observation-age milestones: importing an old backlog cannot age the system.
    Comparisons use deaths per pull; kill durations are shown separately, since
    deaths falling does not itself establish improved damage or mechanic skill.
    ``names`` is reserved for the later member/trial reporting stages.
    """
    monday = date.fromisocalendar(int(week[:4]), int(week[6:]), 1)
    start = datetime.combine(monday, datetime.min.time(), timezone.utc).timestamp()
    end = start + timedelta(days=7).total_seconds()
    rows = [r for r in _scored_rows(_context_rows(conn))
            if _finite(r["received"]) and r["received"] <= now
            and r["end_ms"] <= now * 1000]
    groups = defaultdict(list)
    for row in rows:
        groups[(row["context"], row["score_basis"])].append(row)
    contexts = []
    for (context, basis), history in sorted(groups.items()):
        current = [r for r in history if start * 1000 <= r["start_ms"] < end * 1000]
        if not current:
            continue
        # Descriptive weekly totals use every eligible scored pull; champion
        # preference belongs to the score normalization, not observation counts.
        first = min(r["received"] for r in history)
        baseline_end = first + BASELINE_SECONDS
        baseline = [r for r in history if r["received"] < baseline_end]
        later = [r for r in current if r["received"] >= baseline_end]
        seed = f"{week}:{context}:{basis}"
        deaths, deaths_ci = _mean_interval([r["deaths"] for r in current], seed + ":deaths")
        kills = [r for r in current if r["outcome"] == "killed"]
        kill_s, kill_ci = _mean_interval([_duration(r) for r in kills], seed + ":kill_s")
        base_kills = [_duration(r) for r in baseline if r["outcome"] == "killed"]
        delta, delta_ci, change = None, None, "too few"
        if len(baseline) >= 5 and len(later) >= 5:
            delta, low, high = bootstrap_diff([r["deaths"] for r in baseline],
                [r["deaths"] for r in later], seed=seed + ":change")
            delta_ci = [low, high]
            change = "better" if high < 0 else "worse" if low > 0 else "no change"
        example = current[0]
        coverage = set()
        for row in current:
            try:
                reasons = json.loads(row["checks_coverage"])
            except (ValueError, TypeError):
                reasons = None
            if isinstance(reasons, list) and all(isinstance(reason, str) for reason in reasons):
                coverage.update(reasons)
            if not row["checks_complete"] and not reasons:
                coverage.add("coverage_unavailable")
        contexts.append(dict(context=context, score_basis=basis,
            checks_complete_pulls=sum(bool(r["checks_complete"]) for r in current),
            checks_coverage=sorted(coverage), boss=boss_name(example["map"], example["boss_id"]),
            size=example["size"], farm=is_farm([r["outcome"] for r in history if r["start_ms"] < end * 1000]),
            pulls=len(current), kills=len(kills), wipes=len(current) - len(kills),
            deaths_per_pull=deaths, deaths_per_pull_ci=deaths_ci, kill_s=kill_s, kill_s_ci=kill_ci,
            baseline_deaths=statistics.fmean(r["deaths"] for r in baseline) if baseline else None,
            baseline_kill_s=statistics.fmean(base_kills) if base_kills else None,
            baseline_pulls=len(baseline), comparison_pulls=len(later), baseline_complete=now >= baseline_end,
            deaths_change=delta, deaths_change_ci=delta_ci, change=change))
    since = _meta(conn, "tg_first_pull")
    if type(since) not in (int, float) or not _finite(since) or since < 0 or since > now:
        since = None
    gate = _meta(conn, "tg_gate")
    if not isinstance(gate, dict):
        gate = None
    receipts = [r["received"] for r in rows]
    four_weeks = bool(since is not None and receipts and now - since >= BASELINE_SECONDS
                      and max(receipts) - min(receipts) >= BASELINE_SECONDS)
    gate_ok = bool(gate and gate.get("phase") == "passed"
                   and type(gate.get("verdict_ms")) in (int, float) and _finite(gate.get("verdict_ms"))
                   and gate["verdict_ms"] <= 1.0)
    return dict(since=since, gate=gate, contexts=contexts,
        nothing_improved=sorted({r["boss"] for r in contexts if r["change"] == "no change"}),
        trials=[], promotions=[], rollbacks=[], suspended=[], rotations=[], spread=[], mentees=[], rivals=[],
        done_when=dict(four_weeks=four_weeks, won_tactic=False, won_rotation=False,
                       gate_within_1ms=gate_ok, voice_run=False, cast_callouts=False))
