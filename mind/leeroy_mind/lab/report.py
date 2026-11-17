"""The lab's stored runs as numbers: `lab report`, the console's Conversation lab panel and
health's note. Read only; an older database (no lab tables) reads as not set up."""
import json
import sqlite3

from . import authored, store

DIFF_CHARS = 400


def _arm_status(conn, arm_id: str | None) -> str | None:
    if not arm_id:
        return None
    try:
        row = conn.execute("SELECT status FROM learn_arm WHERE id=?", (arm_id,)).fetchone()
    except sqlite3.OperationalError:
        return None
    return row[0] if row else None


def report(conn, *, night: str | None = None, runs: int = 7) -> dict:
    """The newest `runs` runs (or the runs of `night`): status, piece, budget used, each arm's
    numbers and each variant's outcome, change in rubric mean and the arm it became."""
    if not store.available(conn):
        return {"available": False, "runs": []}
    if night is not None:
        ids = [r[0] for r in conn.execute("SELECT id FROM lab_run WHERE night=? ORDER BY id DESC",
                                          (night,))]
    else:
        ids = [r[0] for r in conn.execute("SELECT id FROM lab_run ORDER BY id DESC LIMIT ?", (runs,))]
    out = []
    for rid in ids:
        r = store.run(conn, rid)
        arms = (r["summary"] or {}).get("arms") or {a: store.arm_summary(rows)
                                                     for a, rows in store.scores(conn, rid).items()}
        variants = []
        found = store.variants(conn, rid)
        written = authored.names(conn, [v["id"] for v in found])
        for v in found:
            try:
                sheet = json.loads(v["scores"]) if v["scores"] else {}
            except ValueError:
                sheet = {}
            variants.append({"id": v["id"], "status": v["status"], "outcome": v["outcome"],
                             "authored": written.get(v["id"]),
                             "delta": sheet.get("delta"), "graded": sheet.get("graded"),
                             "applied": sheet.get("applied"), "regressed": sheet.get("regressed", []),
                             "arm": v["arm_id"], "arm_status": _arm_status(conn, v["arm_id"]),
                             "diff": v["diff"][:DIFF_CHARS]})
        out.append({"id": rid, "night": r["night"], "source": r["source"], "status": r["status"],
                    "grader": r["plan"].get("grader", "rubric"),
                    "promote": bool(r["plan"].get("promote", True)),
                    "reason": r["reason"], "piece": r["piece"], "started": r["started"],
                    "finished": r["finished"], "spent": r["spent"],
                    "pairs": len(r["plan"].get("pairs", [])), "arms": arms, "variants": variants})
    return {"available": True, "runs": out}


def _f(v, digits=2):
    return "n/a" if not isinstance(v, (int, float)) else f"{v:.{digits}f}"


def render(data: dict) -> str:
    if not data.get("available"):
        return "lab: not set up (no lab tables in this mind.db)"
    if not data["runs"]:
        return "lab: no runs yet"
    lines = []
    for r in data["runs"]:
        lines.append(f"run {r['id']} night {r['night']} ({r['source']}): {r['status']}"
                     + (f" ({r['reason']})" if r["reason"] else "")
                     + f", piece {r['piece']}, {r['pairs']} pairs, {round(r['spent'] or 0)} s"
                     + (f", grader {r['grader']}" if r.get("grader") else "")
                     + ("" if r.get("promote", True) else ", measure only (deltas are indicative; "
                        "nothing is proposed to the weekly gate)"))
        for arm, s in sorted(r["arms"].items(), key=lambda kv: (kv[0] != "control", kv[0])):
            flags = ", ".join(f"{c} {n}" for c, n in (s.get("flags") or {}).items() if n) or "none"
            human = s.get("human")
            if s.get("scored"):
                graded = (f"judge score {_f(s.get('score'))} (1-5) over {s['scored']} of "
                          f"{s.get('lines', 0)} answers ({s.get('score_calls', 0)} calls)")
            else:
                graded = (f"rubric {_f(s.get('rubric'))} over {s.get('graded', 0)} of "
                          f"{s.get('lines', 0)} answers")
            lines.append(f"  {arm}: {graded}, answered {s.get('answered', 0)}, "
                         f"applied {s.get('applied', 0)}; flags: {flags}; judge called her human "
                         + (f"{round(100 * human)}% of {s.get('judged')}" if isinstance(human, (int, float))
                            else "n/a"))
            if arm == "control" and s.get("kinds"):
                lines.append("    by kind: " + ", ".join(f"{k} {_f(v)}" for k, v in s["kinds"].items()))
        for v in r["variants"]:
            lines.append(f"  variant {v['id']}"
                         + (f" (hand-written: {v['authored']})" if v.get("authored") else "")
                         + f": {v['outcome'] or v['status']}, change {_f(v['delta'])}"
                         + (f", regressed {','.join(v['regressed'])}" if v["regressed"] else "")
                         + (f", arm {v['arm']} ({v['arm_status']})" if v["arm"] else ""))
            lines.append(f"    {v['diff']}")
    return "\n".join(lines)


def panel(conn, *, nights: int = 14) -> dict:
    """The console's Conversation lab panel: the last nights' control means by kind (the lab
    judge's 1-5 score, or the old 0-3 rubric: `grader` says which), rule-flag rates and the
    blind judge, and the variants tried and promoted (their word diffs: prompt wording, never a
    member's line). `promote` false: the run only measured."""
    data = report(conn, runs=nights)
    if not data["available"]:
        return {"available": False, "runs": []}
    runs = []
    for r in data["runs"]:
        control = r["arms"].get("control") or {}
        runs.append({"night": r["night"], "status": r["status"], "reason": r["reason"],
                     "piece": r["piece"], "pairs": r["pairs"], "spent": r["spent"],
                     "grader": r["grader"], "promote": r["promote"],
                     "score": control.get("score"), "scored": control.get("scored", 0),
                     "rubric": control.get("rubric"), "graded": control.get("graded", 0),
                     "human": control.get("human"), "kinds": control.get("kinds", {}),
                     "flags": control.get("flag_rates", {}),
                     "variants": [{k: v[k] for k in ("id", "outcome", "delta", "arm", "arm_status",
                                                     "diff")} for v in r["variants"]]})
    tried = sum(len(r["variants"]) for r in runs)
    promoted = sum(1 for r in runs for v in r["variants"] if v["outcome"] == "won")
    return {"available": True, "runs": runs, "tried": tried, "promoted": promoted}


def last_completed(conn) -> float | None:
    """When the newest night run that ran to its end finished (done or cut by its budget)."""
    if not store.available(conn):
        return None
    row = conn.execute("SELECT max(finished) FROM lab_run WHERE source='night' AND status IN "
                       "('done','budget')").fetchone()
    return row[0] if row else None
