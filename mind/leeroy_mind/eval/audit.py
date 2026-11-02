"""Owner-only grounding and run-health summaries; no model calls or mutations."""
import json
from collections import Counter, defaultdict

from leeroy_mind.eval.quiz import _rows, safe_text
from leeroy_mind.nightly.audience import has_line_audience


def grounding_audit(store, bounds, rng, *, sample=20, ident=None):
    if sample < 0:
        raise ValueError("negative audit sample")
    start, end = bounds
    # Use the run's original window when present, including a Sunday resumed Monday.
    logs = _rows(store, "SELECT g.* FROM grounding_log g LEFT JOIN night_run n ON n.night=g.night "
                 "WHERE (n.night IS NOT NULL AND n.window_start>=? AND n.window_start<?) OR "
                 "(n.night IS NULL AND g.ts>=? AND g.ts<?) ORDER BY g.id", (start, end, start, end))
    stages, reasons, beliefs = defaultdict(Counter), Counter(), []
    screen = (lambda s: safe_text(s, ident)) if ident is not None else (lambda s: s)
    for row in logs:
        stages[row["stage"]][row["verdict"]] += 1
        if row["verdict"] == "rejected":
            reasons[screen(row["reason"] or "unspecified")] += 1
        elif row["kind"] == "belief" and row["owner"] > 0:
            # The log has no evidence column. Match the accepted claim's exact owner,
            # text and commit second; a supersession can legitimately have no new row.
            candidates = _rows(store, "SELECT evidence FROM memory WHERE owner=? AND text=? "
                "AND source='reflection' AND kind='reflection' AND t_created=? ORDER BY id",
                (row["owner"], row["text"], int(row["ts"])))
            evidence = []
            if len(candidates) == 1:
                try:
                    refs = json.loads(candidates[0]["evidence"])
                    if isinstance(refs, list) and all(isinstance(ref, str) for ref in refs):
                        evidence = [screen(ref) for ref in refs]
                except (ValueError, TypeError):
                    pass
            beliefs.append({"guid": row["owner"], "text": screen(row["text"]), "evidence": evidence})
    chosen = rng.sample(range(len(beliefs)), min(sample, len(beliefs)))
    return {"by_stage": {stage: {"accepted": c["accepted"], "rejected": c["rejected"],
                "reject_rate": c["rejected"] / (c["accepted"] + c["rejected"])}
                for stage, c in sorted(stages.items())},
            "top_reasons": [[reason, n] for reason, n in sorted(reasons.items(), key=lambda x: (-x[1], x[0]))[:5]],
            "rate_these": [beliefs[i] for i in sorted(chosen)]}


def night_health(store, bounds):
    start, end = bounds
    runs = _rows(store, "SELECT * FROM night_run WHERE window_start>=? AND window_start<? "
                 "ORDER BY night", (start, end))
    seconds, plans = defaultdict(float), []
    failures = 0
    for row in runs:
        try:
            errors = json.loads(row["error"]) if row["error"] else {}
            if isinstance(errors, dict):
                failures += sum(n for n in errors.values() if type(n) is int and n > 0)
        except (ValueError, TypeError):
            pass  # A run-level failure message is not a count of failed units.
        try:
            plan = json.loads(row["plan"])
        except (ValueError, TypeError):
            plan = {}
        if not isinstance(plan, dict):
            plan = {}
        plans.append({"night": row["night"], "estimate_s": plan.get("estimate_s"),
                      "budget_s": plan.get("budget_s"), "steps": plan.get("steps", [])})
        for stage in _rows(store, "SELECT stage,started,finished FROM night_stage WHERE night=?",
                           (row["night"],)):
            if stage["finished"] is not None:
                seconds[stage["stage"]] += max(0.0, stage["finished"] - stage["started"])
    return {"runs": len(runs), "failed": sum(r["status"] == "failed" for r in runs),
            "foreign_loads": sum(r["foreign_loads"] for r in runs), "unit_failures": failures,
            "stage_seconds": dict(sorted(seconds.items())), "plans": plans,
            "audience_source": "column" if has_line_audience(store) else "fallback"}
