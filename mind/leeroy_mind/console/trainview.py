"""The Training tab's read-only question to mind.db: what `training status`, `gate`, `trials` and
`cards` print, shared with the CLI through training/inspect.py. Nothing here writes or migrates."""
import math
from datetime import datetime, timezone

from leeroy_mind.realm.race import mode_label
from leeroy_mind.training import inspect
from leeroy_mind.training.report import BASELINE_SECONDS, training_section
from leeroy_mind.wow import boss_name

from . import views

TRIALS = 20
NO_TABLES = "this mind.db has no training ground tables."
OLD_TABLES = "this mind.db's training ground tables are not the current version."
DAY = 86400


def _week(now: float) -> str:
    year, week, _ = datetime.fromtimestamp(now, timezone.utc).isocalendar()
    return f"{year}-W{week:02d}"


def _scope(context: str) -> dict:
    try:
        map_text, boss_text, diff_text = context.split(":")
        map_id, difficulty = int(map_text), int(diff_text)
        boss = "Any boss" if boss_text == "*" else boss_name(map_id, int(boss_text))
        return {"boss": boss, "mode": mode_label(map_id, difficulty)}
    except (ValueError, AttributeError):
        return {"boss": context, "mode": ""}


def _not_set_up(why: str) -> dict:
    return {"setup": False, "why": why}


def training_view(conn, *, now: float) -> dict:
    if "tg_pull" not in views.tables(conn):
        return _not_set_up(NO_TABLES)
    week = _week(now)
    section = training_section(conn, week, now=now, names=str)
    if section["evidence"].get("schema") != "available":
        return _not_set_up(OLD_TABLES)

    since = section["since"]
    observed = (now - since) / DAY if since is not None else None
    left = max(0, math.ceil(BASELINE_SECONDS / DAY - observed)) if observed is not None else None
    gate = section["gate"] if isinstance(section["gate"], dict) else inspect.gate_state(conn)
    verdict = gate.get("verdict_ms")
    passed = gate.get("phase") == "passed"  # the gate's own verdict, against its configured limit

    monday = datetime.fromisocalendar(int(week[:4]), int(week[6:]), 1).replace(tzinfo=timezone.utc).timestamp()
    by_kind = {r[0]: r[1] for r in conn.execute("SELECT kind, count(*) FROM tg_pull GROUP BY kind")}
    week_kind = {r[0]: r[1] for r in conn.execute(
        "SELECT kind, count(*) FROM tg_pull WHERE start_ms >= ? AND start_ms < ? GROUP BY kind",
        (int(monday * 1000), int((monday + 7 * DAY) * 1000)))}

    all_trials = inspect.trials_view(conn, None)["trials"]
    trials = [{"id": t["id"], "scope": t["scope"], **_scope(t["scope"]), "lever": t["lever"],
               "status": t["status"], "opened": t["opened"], "closed": t["closed"],
               "verdict": t["verdict"],
               "arms": len(t["arms"]), "observations": sum(a["observations"] for a in t["arms"])}
              for t in all_trials[:TRIALS]]

    cards: dict[str, dict] = {}
    for c in inspect.cards_view(conn, None)["cards"]:
        group = cards.setdefault(c["context"], {**_scope(c["context"]), "cards": []})
        group["cards"].append({k: c[k] for k in ("id", "version", "status", "origin", "call",
                                                  "reason", "created", "changed")})

    return {"setup": True, "why": "", "week": week, "since": since, "days_observed": observed,
            "days_left": left, "four_weeks": section["done_when"].get("four_weeks") is True,
            "gate": {"phase": gate.get("phase"), "raids": gate.get("raids"),
                     "verdict_ms": verdict, "passed": passed},
            "pulls": {"total": sum(by_kind.values()), "by_kind": by_kind, "week_by_kind": week_kind},
            "contexts": section["contexts"], "trials": trials,
            "trials_open": sum(t["status"] == "open" for t in all_trials), "cards": cards,
            "checklist": inspect.checklist(section["done_when"]), "evidence": section["evidence"]}
