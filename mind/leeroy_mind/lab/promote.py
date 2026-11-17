"""Promotion: each variant against the control, on the same pairs and turns of the same night.

A variant wins when its rubric mean beats the control's by THRESHOLD or more over the answers
both arms had graded (at least MIN_GRADED), its piece was in her prompt on at least one of them,
and no rule check regressed: for no rule code does it have REGRESS_MIN or more, and at least
REGRESS_SHARE of the paired answers, more flagged answers than the control (silent and
unanswered included; one more wordy answer in 30 is the unseeded model's noise, not a
regression). The best winner becomes an active arm of the learn
bandit's family for its piece (learn.arms "prompt.<piece>"), sampled in live talk like any arm and
promoted there only through the weekly cycle's own gate (learn/cycle.py: P(beats control) > 0.9
on 30+ rewarded uses, no guard regression, no veto). Every other variant is retired with its
outcome code. Every variant keeps its scoresheet (numbers only). A global learning freeze or a
frozen family promotes nothing (outcome `frozen`).

The grade compared is the run's grader's (`key`): "rubric" (lab/grade.py's 0-3) or "score"
(lab/judge.py's 1-5), each with its own margin (THRESHOLDS). With `allow` false ([lab] promote,
the default) nothing becomes an arm: the variant that would have is retired `measure_only` with
its scoresheet, so the lab records and reports and the weekly gate never sees it."""
import json
import math
from statistics import mean

from leeroy_mind.learn import arms
from leeroy_mind.learn.control import effective

from . import rules, store

THRESHOLD = 0.15
THRESHOLDS = {"rubric": THRESHOLD, "score": 0.2}   # of a 0-3 and of a 1-5 scale
MIN_GRADED = 8
REGRESS_MIN, REGRESS_SHARE = 2, 0.10


def compare(control: dict, variant: dict, *, key: str = "rubric") -> dict:
    """The scoresheet of `variant` against `control` (each {(pair, turn): lab_score row}), on
    the rows' `key` grade."""
    keys = sorted(set(control) & set(variant))
    graded = [k for k in keys if control[k].get(key) is not None and variant[k].get(key) is not None]
    sheet = {"lines": len(keys), "graded": len(graded), "grade": key,
             "applied": sum(int(variant[k]["applied"]) for k in keys)}
    c_flags = rules.tally([control[k]["rules"] for k in keys])
    v_flags = rules.tally([variant[k]["rules"] for k in keys])
    sheet["flags"] = {"control": c_flags, "variant": v_flags}
    margin = max(REGRESS_MIN, math.ceil(REGRESS_SHARE * len(keys)))
    sheet["regressed"] = [c for c in rules.CODES if v_flags[c] - c_flags[c] >= margin]
    if graded:
        c_mean = mean(control[k][key] for k in graded)
        v_mean = mean(variant[k][key] for k in graded)
        sheet.update(control=round(c_mean, 4), variant=round(v_mean, 4),
                     delta=round(v_mean - c_mean, 4))
    judged = [k for k in keys if control[k]["judged_human"] is not None
              and variant[k]["judged_human"] is not None]
    pairs = {k[0]: k for k in judged}
    if pairs:
        sheet["judge"] = {"pairs": len(pairs),
                          "control_human": round(mean(control[k]["judged_human"] for k in pairs.values()), 4),
                          "variant_human": round(mean(variant[k]["judged_human"] for k in pairs.values()), 4)}
    if len(graded) < MIN_GRADED:
        sheet["outcome"] = "too_few"
    elif not sheet["applied"]:
        sheet["outcome"] = "untested"
    elif sheet["regressed"]:
        sheet["outcome"] = "regressed"
    elif sheet["delta"] >= THRESHOLDS[key] - 1e-9:
        sheet["outcome"] = "won"
    else:
        sheet["outcome"] = "lost"
    return sheet


def arm_id(piece: str, variant: int) -> str:
    return f"lab.{piece}.v{variant}"


def promote(conn, run_id: int, *, settings=None, now: float, budget_cut: bool = False,
            key: str = "rubric", allow: bool = True) -> dict:
    """Settle every proposed variant of the run and promote the best winner. Returns
    {"promoted": arm id or None, "outcomes": {variant id: outcome}, "summary": per arm}.
    `budget_cut`: an arm the budget left unfinished is `budget` rather than too_few. `key`:
    the grade compared; `allow` false: measure only (the winner is retired `measure_only`)."""
    by_arm = store.scores(conn, run_id)
    control = by_arm.get("control", {})
    proposed = [v for v in store.variants(conn, run_id) if v["status"] == "proposed"]
    sheets = {v["id"]: compare(control, by_arm.get(f"v{v['id']}", {}), key=key) for v in proposed}
    if budget_cut:
        for sheet in sheets.values():
            if sheet["outcome"] == "too_few":
                sheet["outcome"] = "budget"
    cfg = effective(conn, settings)
    winners = sorted((sheets[v["id"]]["delta"], -v["id"], v) for v in proposed
                     if sheets[v["id"]]["outcome"] == "won")
    best = winners[-1][2] if winners else None
    promoted = None
    for v in proposed:
        sheet = sheets[v["id"]]
        family = arms.PROMPT_PREFIX + v["piece"]
        if v is best and (cfg.freeze or family in cfg.frozen_families):
            sheet["outcome"] = "frozen"
        if v is best and sheet["outcome"] == "won" and not allow:
            sheet["outcome"] = "measure_only"
        if v is best and sheet["outcome"] == "won":
            ident = arm_id(v["piece"], v["id"])
            control_id = arms.control_of(family)
            conn.execute("INSERT OR IGNORE INTO learn_arm VALUES (?,?,?,?,?,?,?)",
                         (control_id, family, json.dumps({"mode": "control"}), 1, "control",
                          "builtin", now))
            params = {"piece": v["piece"], "variant": v["id"]}
            # origin stays 'coach' (0034's CHECK); the "lab." id and prompt_variant.arm_id say it
            conn.execute("INSERT OR IGNORE INTO learn_arm VALUES (?,?,?,?,?,?,?)",
                         (ident, family, json.dumps(params, sort_keys=True), 1, "active", "coach", now))
            store.settle_variant(conn, v["id"], status="promoted", outcome="won", scores=sheet,
                                 arm_id=ident)
            promoted = ident
        else:
            if sheet["outcome"] == "won":
                sheet["outcome"] = "beaten"  # beat the control, not tonight's best
            store.settle_variant(conn, v["id"], status="retired", outcome=sheet["outcome"],
                                 scores=sheet)
    summary = {arm: store.arm_summary(rows) for arm, rows in sorted(by_arm.items())}
    return {"promoted": promoted, "outcomes": {v["id"]: sheets[v["id"]]["outcome"] for v in proposed},
            "summary": summary,
            "deltas": {v["id"]: sheets[v["id"]].get("delta") for v in proposed}}
