"""Read-only training inspection that the CLI and the console share. Nothing here writes."""
import json

from .experiments import Experimenter

CHECKLIST = (
    ("four_weeks", "28 days of eligible observations"),
    ("won_tactic", "Offline-validated tactic adopted in a human raid"),
    ("won_rotation", "Field-validated rotation"),
    ("gate_within_1ms", "Measured telemetry overhead within 1 ms"),
    ("voice_run", "Voice delivery accepted in a live raid"),
    ("cast_callouts", "Cast callouts accepted in a live raid"),
)
_RECORDING = {"voice_run", "cast_callouts"}


def meta(conn, key):
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return json.loads(row[0]) if row else None


def gate_state(conn):
    row = conn.execute("SELECT value FROM meta WHERE key='tg_gate'").fetchone()
    return json.loads(row[0]) if row else {"phase": "ab", "raids": 0, "verdict_ms": None}


def trials_view(conn, context):
    rows = conn.execute("SELECT * FROM tg_trial" + (" WHERE scope=?" if context else "")
        + " ORDER BY opened DESC,id DESC LIMIT 200", (context,) if context else ()).fetchall()
    result = {"trials": [], "refusals": meta(conn, "tg:experiment_refusals"),
              "metrics": {"raid": "fixed per-trial score basis",
                          "spec": {"available": False, "reason": Experimenter.SPEC_UNAVAILABLE}}}
    for row in rows:
        trial = dict(row)
        trial["provenance"] = meta(conn, f"tg:trial:{row['id']}")
        trial["invalidation"] = meta(conn, f"tg:trial_invalidated:{row['id']}")
        trial["arms"] = []
        for arm in conn.execute("SELECT a.*,c.lineage,c.origin,c.evidence,"
                "(SELECT count(*) FROM tg_obs o WHERE o.arm=a.id) AS observations "
                "FROM tg_arm a LEFT JOIN tg_card c ON c.id=a.card WHERE a.trial=? ORDER BY a.id",
                (row["id"],)).fetchall():
            entry = dict(arm)
            entry["evidence"] = json.loads(entry["evidence"]) if entry["evidence"] else []
            trial["arms"].append(entry)
        result["trials"].append(trial)
    return result


def cards_view(conn, context):
    fields = "id,context,version,lineage,status,origin,directives,call,assignment,reason,evidence,vetoes,prev_champion,created,changed"
    rows = conn.execute("SELECT " + fields + " FROM tg_card"
        + (" WHERE context=?" if context else "") + " ORDER BY context,version,id LIMIT 200",
        (context,) if context else ()).fetchall()
    result = {"cards": [dict(row) for row in rows]}
    for row in result["cards"]:
        for field in ("directives", "assignment", "evidence", "vetoes"):
            row[field] = json.loads(row[field])
    return result


def checklist(done_when):
    """What counts as done: [{key, label, done, waiting}]; the voice and callout milestones wait for
    a recording until the overhead gate has passed."""
    out = []
    for key, label in CHECKLIST:
        done = done_when.get(key) is True
        out.append({"key": key, "label": label, "done": done,
                    "waiting": key in _RECORDING and not done and done_when.get("gate_within_1ms") is not True})
    return out
