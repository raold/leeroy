"""lab_run, prompt_variant and lab_score (migration *_lab.sql). The night and `lab run` write
them inside their units' transactions; the console, health and the CLI read them and never
migrate. Numbers, codes and ids only, but for prompt_variant's text and diff (prompt wording,
never persona text or his lines)."""
import json
import sqlite3
from statistics import mean

from . import rules

TABLES = frozenset({"lab_run", "prompt_variant", "lab_score"})


def available(conn: sqlite3.Connection) -> bool:
    return TABLES <= {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def create_run(conn, *, night: str, source: str, piece: str, seed: str, plan: dict,
               now: float) -> int:
    cur = conn.execute("INSERT INTO lab_run(night, source, piece, seed, plan, started, status) "
                       "VALUES (?,?,?,?,?,?,'running')",
                       (night, source, piece, seed, json.dumps(plan, sort_keys=True), now))
    return cur.lastrowid


def night_run(conn, night: str, source: str = "night") -> dict | None:
    row = conn.execute("SELECT * FROM lab_run WHERE night=? AND source=? ORDER BY id DESC LIMIT 1",
                       (night, source)).fetchone()
    return _run(conn, row)


def run(conn, run_id: int) -> dict | None:
    return _run(conn, conn.execute("SELECT * FROM lab_run WHERE id=?", (run_id,)).fetchone())


def _run(conn, row) -> dict | None:
    if row is None:
        return None
    cols = [d[0] for d in conn.execute("SELECT * FROM lab_run LIMIT 0").description]
    out = dict(zip(cols, row))
    out["plan"] = json.loads(out["plan"])
    out["summary"] = json.loads(out["summary"]) if out["summary"] else None
    return out


def set_spent(conn, run_id: int, spent: float) -> None:
    conn.execute("UPDATE lab_run SET spent=max(spent, ?) WHERE id=?", (round(spent, 1), run_id))


def finish(conn, run_id: int, *, status: str, reason: str | None, summary: dict | None,
           now: float) -> None:
    conn.execute("UPDATE lab_run SET status=?, reason=?, summary=?, finished=? WHERE id=?",
                 (status, reason, json.dumps(summary, sort_keys=True) if summary is not None else None,
                  now, run_id))


def pieces_history(conn) -> list[str]:
    """The pieces of earlier runs that proposed variants, oldest first (the round-robin)."""
    return [r[0] for r in conn.execute(
        "SELECT r.piece FROM lab_run r WHERE EXISTS (SELECT 1 FROM prompt_variant v WHERE v.run=r.id) "
        "ORDER BY r.id")]


def add_variant(conn, run_id: int, piece: str, text: str, diff: str, now: float) -> int:
    return conn.execute("INSERT INTO prompt_variant(run, piece, text, diff, status, created) "
                        "VALUES (?,?,?,?,'proposed',?)", (run_id, piece, text, diff, now)).lastrowid


def variants(conn, run_id: int) -> list[dict]:
    cols = ("id", "piece", "text", "diff", "status", "outcome", "arm_id", "scores")
    return [dict(zip(cols, r)) for r in conn.execute(
        "SELECT id, piece, text, diff, status, outcome, arm_id, scores FROM prompt_variant "
        "WHERE run=? ORDER BY id", (run_id,))]


def settle_variant(conn, vid: int, *, status: str, outcome: str, scores: dict,
                   arm_id: str | None = None) -> None:
    conn.execute("UPDATE prompt_variant SET status=?, outcome=?, scores=?, arm_id=? WHERE id=?",
                 (status, outcome, json.dumps(scores, sort_keys=True), arm_id, vid))


def put_scores(conn, run_id: int, arm: str, rows: list[dict]) -> None:
    conn.execute("DELETE FROM lab_score WHERE run=? AND arm=?", (run_id, arm))
    for r in rows:
        conn.execute(
            "INSERT INTO lab_score(run, arm, pair, turn, guid, kind, channel, answered, applied, "
            "rules, words, his_words) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, arm, r["pair"], r["turn"], r["guid"], r["kind"], r["channel"],
             int(r["answered"]), int(r["applied"]), json.dumps(r["rules"], sort_keys=True),
             r["words"], r["his_words"]))


def set_rubric(conn, run_id: int, arm: str, pair: int, grades: list[tuple[int, str]]) -> None:
    for turn, (score, code) in enumerate(grades, 1):
        conn.execute("UPDATE lab_score SET rubric=?, rubric_code=? WHERE run=? AND arm=? AND pair=? "
                     "AND turn=?", (score, code, run_id, arm, pair, turn))


def set_score(conn, run_id: int, arm: str, pair: int, marks: list[tuple[float, int]]) -> None:
    """The lab judge's 1-5 score of each turn of a pair, with the calls it took."""
    for turn, (score, calls) in enumerate(marks, 1):
        conn.execute("UPDATE lab_score SET score=?, score_calls=? WHERE run=? AND arm=? AND pair=? "
                     "AND turn=?", (score, calls, run_id, arm, pair, turn))


def set_prefer(conn, run_id: int, arm: str, pair: int, shares: list[float | None]) -> None:
    """A variant's answers against the control's to the same lines (judge.prefer's share)."""
    for turn, share in enumerate(shares, 1):
        conn.execute("UPDATE lab_score SET prefer=? WHERE run=? AND arm=? AND pair=? AND turn=?",
                     (share, run_id, arm, pair, turn))


def has_score(conn) -> bool:
    """Whether this mind.db has the judge's columns (migration *_lab_judge.sql); a reader of an
    older database sees every score as None."""
    return "score" in {r[1] for r in conn.execute("PRAGMA table_info(lab_score)")}


def set_judged(conn, run_id: int, arm: str, pair: int, her: int, him: int | None) -> None:
    conn.execute("UPDATE lab_score SET judged_human=?, his_judged_human=? WHERE run=? AND arm=? "
                 "AND pair=?", (her, him, run_id, arm, pair))


def scores(conn, run_id: int) -> dict[str, dict[tuple[int, int], dict]]:
    cols = ("arm", "pair", "turn", "guid", "kind", "channel", "answered", "applied", "rules",
            "words", "his_words", "rubric", "rubric_code", "judged_human", "his_judged_human")
    extra = ("score", "score_calls", "prefer") if has_score(conn) else ()
    out: dict[str, dict] = {}
    for r in conn.execute(f"SELECT {', '.join(cols + extra)} FROM lab_score WHERE run=?", (run_id,)):
        row = dict(zip(cols + extra, r))
        row.setdefault("score", None)
        row.setdefault("score_calls", None)
        row.setdefault("prefer", None)
        row["rules"] = json.loads(row["rules"])
        out.setdefault(row["arm"], {})[(row["pair"], row["turn"])] = row
    return out


def arm_summary(rows: dict) -> dict:
    """An arm's numbers: lines, answered, the rubric mean and how many graded (the 0-3 rubric)
    or the judge's mean score and how many scored (lab/judge.py's 1-5), rule-flag rates, the
    blind judge's rate of calling her human (per pair), by kind too (the judge's score when the
    arm has one, else the rubric)."""
    items = list(rows.values())
    graded = [r["rubric"] for r in items if r["rubric"] is not None]
    scored = [r["score"] for r in items if r.get("score") is not None]
    calls = [r["score_calls"] for r in items if r.get("score_calls") is not None]
    judged = {r["pair"]: r["judged_human"] for r in items if r["judged_human"] is not None}
    flags = rules.tally([r["rules"] for r in items])
    kinds: dict[str, list[int]] = {}
    key = "score" if scored else "rubric"
    for r in items:
        if r.get(key) is not None:
            kinds.setdefault(r["kind"], []).append(r[key])
    codes: dict[str, int] = {}
    for r in items:
        if r["rubric_code"]:
            codes[r["rubric_code"]] = codes.get(r["rubric_code"], 0) + 1
    return {"lines": len(items), "answered": sum(r["answered"] for r in items),
            "applied": sum(r["applied"] for r in items),
            "graded": len(graded), "rubric": round(mean(graded), 4) if graded else None,
            "scored": len(scored), "score": round(mean(scored), 4) if scored else None,
            "score_calls": sum(calls),
            "flags": flags, "flag_rates": {c: round(n / len(items), 4) if items else 0.0
                                           for c, n in flags.items()},
            "judged": len(judged), "human": round(sum(judged.values()) / len(judged), 4) if judged else None,
            "kinds": {k: round(mean(v), 4) for k, v in sorted(kinds.items())},
            "codes": dict(sorted(codes.items(), key=lambda kv: -kv[1]))}


def feedback(conn, piece: str, *, tried: int = 5) -> dict:
    """What the proposer reads: the newest graded control (its rubric mean, or the judge's
    mean score with its scale, count, commonest reason codes, rule-flag rates) and up to `tried`
    earlier variants of this piece with their change against their control and outcome."""
    out: dict = {}
    graded = "(rubric IS NOT NULL OR score IS NOT NULL)" if has_score(conn) else "rubric IS NOT NULL"
    for (rid,) in conn.execute(f"SELECT DISTINCT run FROM lab_score WHERE arm='control' AND {graded} "
                               "ORDER BY run DESC LIMIT 1"):
        s = arm_summary(scores(conn, rid).get("control", {}))
        if s["scored"]:
            out["control"] = {"rubric": s["score"], "n": s["scored"], "scale": "1-5",
                              "codes": [], "flags": s["flag_rates"]}
        elif s["graded"]:
            out["control"] = {"rubric": s["rubric"], "n": s["graded"],
                              "codes": list(s["codes"].items())[:4], "flags": s["flag_rates"]}
    rows = conn.execute("SELECT text, outcome, scores FROM prompt_variant WHERE piece=? AND scores "
                        "IS NOT NULL ORDER BY id DESC LIMIT ?", (piece, tried)).fetchall()
    out["tried"] = []
    for text, outcome, raw in rows:
        try:
            delta = json.loads(raw).get("delta")
        except (ValueError, TypeError, AttributeError):
            delta = None
        if isinstance(delta, (int, float)):
            out["tried"].append({"text": text, "outcome": outcome or "?", "delta": float(delta)})
    return out


def earlier_texts(conn, piece: str) -> list[str]:
    return [r[0] for r in conn.execute("SELECT text FROM prompt_variant WHERE piece=?", (piece,))]
