"""turing_night and turing_judgement (migration *_turing.sql). The night writes them inside its
units' transactions; readers check the tables exist and never migrate."""
import json
import sqlite3

from .corpus import FAMILIES, tables

PLAN_KEY = "turing:plan:"  # meta: the night's chosen windows, so a resumed run judges the same


def available(conn: sqlite3.Connection) -> bool:
    return {"turing_night", "turing_judgement"} <= tables(conn)


def put_stylo(conn: sqlite3.Connection, night: str, result: dict, *, now: float) -> None:
    """Each family's stylometric row for the night, keeping any judge totals already there."""
    for family in FAMILIES:
        r = result["families"].get(family)
        if r is None:
            continue
        conn.execute(
            "INSERT INTO turing_night(night, family, ts, days, status, human_lines, member_lines, "
            "sample, groups, auc, auc_low, auc_high, features) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(night, family) DO UPDATE SET ts=excluded.ts, days=excluded.days, "
            "status=excluded.status, human_lines=excluded.human_lines, "
            "member_lines=excluded.member_lines, sample=excluded.sample, groups=excluded.groups, "
            "auc=excluded.auc, auc_low=excluded.auc_low, auc_high=excluded.auc_high, "
            "features=excluded.features",
            (night, family, now, result["days"], r["status"], r["human_lines"], r["member_lines"],
             r["sample"], r["groups"], r["auc"], r["low"], r["high"], json.dumps(r["features"])))


def put_judgement(conn: sqlite3.Connection, night: str, unit: str, family: str, refs: list[str],
                  scored: dict, *, now: float) -> None:
    """One judged window, and its family's night totals brought up to date with it."""
    conn.execute(
        "INSERT INTO turing_judgement(night, unit, family, refs, status, speakers, correct, his, "
        "his_correct, tells, ts) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (night, unit, family, json.dumps(refs), scored["status"], scored["speakers"],
         scored["correct"], scored["his"], scored["his_correct"], json.dumps(scored["tells"]), now))
    totals = conn.execute(
        "SELECT count(*), coalesce(sum(speakers),0), coalesce(sum(correct),0), "
        "coalesce(sum(his),0), coalesce(sum(his_correct),0) FROM turing_judgement "
        "WHERE night=? AND family=? AND status='ok'", (night, family)).fetchone()
    tells: dict[str, dict[str, int]] = {"caught": {}, "missed": {}, "him": {}}
    for (value,) in conn.execute("SELECT tells FROM turing_judgement WHERE night=? AND family=? "
                                 "AND status='ok'", (night, family)):
        for t in json.loads(value):
            side = "him" if t["truth"] == "human" else "caught" if t["judged"] == "bot" else "missed"
            tells[side][t["code"]] = tells[side].get(t["code"], 0) + 1
    conn.execute(
        "INSERT INTO turing_night(night, family, ts, judge_windows, judge_speakers, judge_correct, "
        "judge_his, judge_his_correct, tells) VALUES (?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(night, family) DO UPDATE SET judge_windows=excluded.judge_windows, "
        "judge_speakers=excluded.judge_speakers, judge_correct=excluded.judge_correct, "
        "judge_his=excluded.judge_his, judge_his_correct=excluded.judge_his_correct, "
        "tells=excluded.tells",
        (night, family, now, *totals, json.dumps(tells, sort_keys=True)))


def judged_refs(conn: sqlite3.Connection) -> set[str]:
    """Every line some earlier window showed the judge."""
    if "turing_judgement" not in tables(conn):
        return set()
    out: set[str] = set()
    for (value,) in conn.execute("SELECT refs FROM turing_judgement"):
        try:
            out.update(r for r in json.loads(value) if isinstance(r, str))
        except (ValueError, TypeError):
            continue
    return out
