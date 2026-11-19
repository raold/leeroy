"""The lab with the judge (`grader = "tells"`): the live-model score stage, the comparison on the
judge's 1-5 score, and measure-only mode (`promote = false`, the default, with either grader):
a variant ahead of the control is recorded and nothing reaches the learn bandit. Invented cast,
FakeLLMs."""
import asyncio
import json

import pytest

from lab.rig import InProcessChild, LIVE_TOML, judge_reply, make_live, night_ctx, title_of, variant_text
from leeroy_mind.lab import judge, night as lab_night, report, store as lab_store
from leeroy_mind.lab.config import load
from leeroy_mind.learn import arms
from leeroy_mind.talk.beat import MARK

WIN, LOSE = "Plainly", "Truly"
SCRIPTS = {"flirt-3", "smalltalk-3", "meet-3", "pushback-3"}
GOOD, PLAIN = "yes, tonight works for me", "ha maybe, we will see"  # under six words: a repeat is no copy
MEASURE = LIVE_TOML.replace('grader = "rubric"\n', 'grader = "tells"\n')
PROMOTE = LIVE_TOML.replace('grader = "rubric"\n', 'grader = "tells"\npromote = true\n')
DEFAULTS = LIVE_TOML.replace('grader = "rubric"\n', "")
NO_JUDGE = MEASURE.replace("judge = 2\n", "judge = 0\n")
assert MEASURE != LIVE_TOML and PROMOTE != LIVE_TOML and DEFAULTS != LIVE_TOML and NO_JUDGE != MEASURE


def answer(model, messages):
    last = messages[-1]["content"]
    if MARK in last:
        return json.dumps({"read": "he says hi", "lands": "warm", "want": "share", "use": None})
    return GOOD if f"{WIN} respond to what" in last else PLAIN


def scripted(seen):
    def reflection(model, messages):
        title = title_of(messages)
        text = messages[-1]["content"]
        system = messages[0]["content"]
        if title is None:   # the judge sends its schema as the format only
            title = {judge.TELL_SYSTEM: "lab_tell", judge.WIDE_SYSTEM: "lab_tell",
                     judge.GRADE_SYSTEM: "lab_grade", judge.PAIR_SYSTEM: "lab_pair"}.get(system)
        if title == "lab_pair":   # the good answer reads more like a person, wherever it is shown
            a, b = text.split("Exchange B")
            return json.dumps({"better": "same" if (GOOD in a) == (GOOD in b) else ("A" if GOOD in a else "B")})
        seen.append((model, title))
        if title == "lab_variants":
            return json.dumps({"variants": [{"text": variant_text(WIN), "idea": "plainer"},
                                            {"text": variant_text(LOSE), "idea": "truer"}]})
        if title == "lab_rubric":
            rows = [x for x in text.splitlines() if x.startswith("her (reply ")]
            return json.dumps({"grades": [
                {"reply": i, "score": 3 if GOOD in x else 1, "code": "passes" if GOOD in x else "generic"}
                for i, x in enumerate(rows, 1)]})
        if title == "lab_grade":
            return json.dumps({"problem": "none", "grade": 2 if PLAIN in text.rsplit("her reply", 1)[1] else 5})
        if title == "lab_tell":
            # the judge's tells: every tell is a defect of the plain answer, none of the good one
            question = text.rsplit("Question: ", 1)[1]
            bad = next(b for _, q, b, _ in judge.TELLS if q == question)
            good = "no" if bad == "yes" else "yes"
            return json.dumps({"evidence": "", "answer": bad if PLAIN in text.rsplit("Her reply", 1)[1] else good})
        if title == "turing_judge":
            return judge_reply(messages)
        raise AssertionError(f"unexpected call {title}")
    return reflection


def failing(seen):
    """The judge's grade call gives no valid answer for the good answers: their units fail."""
    base = scripted(seen)

    def reflection(model, messages):
        if (messages[0]["content"] == judge.GRADE_SYSTEM
                and GOOD in messages[-1]["content"].rsplit("her reply", 1)[1]):
            return "not json"
        return base(model, messages)
    return reflection


def spend_all(ctx):
    """The budget is gone once the battery has run."""
    with ctx.store.transaction():
        run = lab_store.night_run(ctx.store.conn, ctx.night)
        ctx.store.conn.execute("UPDATE lab_run SET spent=? WHERE id=?",
                               (run["plan"]["minutes"] * 60 + 1, run["id"]))


def run_night(live, tmp_path, *, hold_before_score=False, script=scripted, before_score=None):
    cfg = load(live / "mind.toml")
    seen = []

    async def go():
        ctx, llm = night_ctx(live, script(seen))
        try:
            def make_child(data_dir, seed):
                return InProcessChild(data_dir, seed, answer, tmp_path)
            await lab_night.make_propose(cfg, scripts=SCRIPTS, members={102, 103})(ctx)
            await lab_night.make_battery(cfg, make_child=make_child)(ctx)
            if before_score is not None:
                before_score(ctx)
            if hold_before_score:
                ctx.arbiter.hold("human")
            wanted = lab_night._wants(ctx, lab_night.STAGE_SCORE), lab_night._wants(ctx, lab_night.STAGE_GRADE)
            await lab_night.make_score(cfg)(ctx)
            await lab_night.make_grade(cfg)(ctx)
            with ctx.store.transaction():
                run = lab_store.night_run(ctx.store.conn, ctx.night)
                variants = lab_store.variants(ctx.store.conn, run["id"])
                scores = lab_store.scores(ctx.store.conn, run["id"])
                registry = {a.id: a for a in arms.read(ctx.store.conn)}
                shown = report.render(report.report(ctx.store.conn))
                panel = report.panel(ctx.store.conn)
            return {"run": run, "variants": variants, "scores": scores, "registry": registry,
                    "failures": dict(ctx.failures), "calls": llm.calls, "wanted": wanted,
                    "settings": ctx.settings, "shown": shown, "panel": panel,
                    "metered": ctx.reasoner.calls}
        finally:
            ctx.store.close()
    out = asyncio.run(go())
    out["seen"] = seen
    return out


def test_the_default_night_grades_with_the_rubric_and_only_measures(tmp_path):
    live = make_live(tmp_path, DEFAULTS)
    cfg = load(live / "mind.toml")
    assert cfg.grader == "rubric" and cfg.promote is False
    got = run_night(live, tmp_path)
    assert not got["failures"], got["failures"]
    assert got["wanted"] == (False, True)   # no score stage for the rubric
    win, lose = got["variants"]
    assert (win["status"], win["outcome"], win["arm_id"]) == ("retired", "measure_only", None)
    assert json.loads(win["scores"])["grade"] == "rubric"
    assert not any(a.startswith("lab.") or a.startswith("prompt.") for a in got["registry"])
    assert not {"lab_tell", "lab_grade"} & {t for _, t in got["seen"]}
    assert "measure only" in got["shown"] and "grader rubric" in got["shown"]
    assert got["panel"]["runs"][0]["promote"] is False


def test_a_judge_night_scores_on_the_live_model_and_only_measures(tmp_path):
    live = make_live(tmp_path, MEASURE)
    cfg = load(live / "mind.toml")
    assert cfg.grader == "tells" and cfg.promote is False
    got = run_night(live, tmp_path)
    assert not got["failures"], got["failures"]
    run = got["run"]
    assert run["status"] == "done" and run["plan"]["grader"] == "tells" and run["plan"]["promote"] is False
    assert got["wanted"] == (True, False)   # scoring first; grading waits for it
    win, lose = got["variants"]
    # the variant that would have won is recorded as measured: no arm, nothing for the weekly gate
    assert (win["status"], win["outcome"], win["arm_id"]) == ("retired", "measure_only", None)
    assert (lose["status"], lose["outcome"]) == ("retired", "lost")
    assert not any(a.startswith("lab.") or a.startswith("prompt.") for a in got["registry"])
    sheet = json.loads(win["scores"])
    assert sheet["grade"] == "score" and sheet["delta"] > 0.2 and sheet["graded"] == 8
    # every answer of every arm has the judge's score and its calls; the rubric was never asked
    rows = [r for arm in got["scores"].values() for r in arm.values()]
    assert all(1.0 <= r["score"] <= 5.0 and r["score_calls"] > 0 and r["rubric"] is None for r in rows)
    assert "lab_rubric" not in {t for _, t in got["seen"]}
    control = {k: r["score"] for k, r in got["scores"]["control"].items()}
    winner = {k: r["score"] for k, r in got["scores"][f"v{win['id']}"].items()}
    assert all(winner[k] > control[k] for k in control)
    # the judge ran on the live model at the live num_ctx, think off, temperature 0, seeded;
    # the same pair and turn has the same seed in every arm
    # each variant's answers were also compared with the control's to the same lines, both ways
    assert all(r["prefer"] is None for r in got["scores"]["control"].values())
    assert {r["prefer"] for r in got["scores"][f"v{win['id']}"].values()} == {1.0}
    assert {r["prefer"] for r in got["scores"][f"v{lose['id']}"].values()} == {0.5}
    assert sheet["prefer"] == {"n": 8, "share": 1.0}
    assert json.loads(lose["scores"])["prefer"] == {"n": 8, "share": 0.5}
    assert "preferred to the control's answer 100% over 8 lines" in got["shown"]
    pairs = [c for c in got["calls"] if (c.get("format") or {}).get("title") == "lab_pair"]
    assert len(pairs) == 2 * 2 * 8 and all(c["options"]["temperature"] == 0.0 for c in pairs)
    tells = [c for c in got["calls"] if (c.get("format") or {}).get("title") in ("lab_tell", "lab_grade")]
    assert tells and all(c["model"] == got["settings"].live_model for c in tells)
    assert all(c["think"] is False and c["options"]["temperature"] == 0.0
               and c["options"]["num_ctx"] == 4096 for c in tells)
    per_arm = len(tells) // 3
    assert sorted(c["options"]["seed"] for c in tells[:per_arm]) == sorted(
        c["options"]["seed"] for c in tells[per_arm:2 * per_arm])
    assert got["metered"] >= len(tells)   # the runner's meter saw the judge's calls
    # the report and the console panel say what the run was
    assert "measure only" in got["shown"] and "judge score" in got["shown"] and "grader tells" in got["shown"]
    night = got["panel"]["runs"][0]
    assert night["promote"] is False and night["grader"] == "tells" and night["scored"] == 8
    assert night["score"] == pytest.approx(sum(control.values()) / len(control), abs=1e-3)
    assert [v["outcome"] for v in night["variants"]] == ["measure_only", "lost"]


def test_with_promote_on_the_judges_winner_becomes_an_arm(tmp_path):
    live = make_live(tmp_path, PROMOTE)
    got = run_night(live, tmp_path)
    win, lose = got["variants"]
    assert (win["status"], win["outcome"]) == ("promoted", "won")
    arm = got["registry"][win["arm_id"]]
    assert arm.family == "prompt.reply_human" and arm.status == "active"
    assert "measure only" not in got["shown"]


def test_a_resumed_judge_night_asks_nothing_again(tmp_path):
    live = make_live(tmp_path, MEASURE)
    run_night(live, tmp_path)
    again = run_night(live, tmp_path)
    assert again["run"]["status"] == "done" and not again["seen"] and not again["failures"]


def test_a_held_lease_makes_the_scoring_and_the_grading_wait(tmp_path):
    live = make_live(tmp_path, MEASURE)
    got = run_night(live, tmp_path, hold_before_score=True)
    assert got["run"]["status"] == "running"
    assert not {"lab_tell", "lab_grade", "lab_pair"} & {t for _, t in got["seen"]}
    assert all(r["score"] is None and r["prefer"] is None
               for arm in got["scores"].values() for r in arm.values())
    assert all(v["status"] == "proposed" for v in got["variants"])


def test_with_judge_0_the_score_stage_finishes_and_the_reflection_model_is_not_needed(tmp_path):
    """Review of #369: with the tells grader and no blind-judge pair the grade stage had nothing
    for the reflection model but still swapped to it to commit `finish`."""
    live = make_live(tmp_path, NO_JUDGE)
    got = run_night(live, tmp_path)
    assert not got["failures"], got["failures"]
    assert got["wanted"] == (True, False)   # the runner never runs lab_grade: no swap
    run = got["run"]
    assert run["status"] == "done" and run["plan"]["judge"] == 0 and run["summary"]["failed_units"] == 0
    reflection = got["settings"].reflection_model
    assert [t for m, t in got["seen"] if m == reflection] == ["lab_variants"]   # the proposal only
    assert [v["outcome"] for v in got["variants"]] == ["measure_only", "lost"]


def test_a_judge_unit_that_failed_is_failed_not_the_budgets(tmp_path):
    live = make_live(tmp_path, MEASURE)
    got = run_night(live, tmp_path, script=failing)
    assert got["failures"] == {lab_night.STAGE_SCORE: 4}   # the winner's four pairs
    run = got["run"]
    assert (run["status"], run["reason"]) == ("done", "units_failed")
    assert run["summary"]["failed_units"] == 4 and run["summary"]["cut"] is False
    win, lose = got["variants"]
    assert (win["status"], win["outcome"], win["arm_id"]) == ("retired", "failed", None)
    assert lose["outcome"] == "lost"
    assert "done (units_failed)" in got["shown"]


def test_a_score_stage_the_budget_cut_settles_budget(tmp_path):
    live = make_live(tmp_path, NO_JUDGE)
    got = run_night(live, tmp_path, before_score=spend_all)
    assert not got["failures"]
    run = got["run"]
    assert (run["status"], run["reason"]) == ("budget", "budget") and run["summary"]["failed_units"] == 0
    assert {v["outcome"] for v in got["variants"]} == {"budget"}
    assert not {"lab_tell", "lab_grade", "lab_pair"} & {t for _, t in got["seen"]}


def test_a_run_keeps_the_grader_it_began_with(tmp_path):
    """A run planned under the rubric is graded by the rubric even if the setting changed."""
    live = make_live(tmp_path)   # the rig's table: grader rubric (promote off: refused with it)
    cfg = load(live / "mind.toml")
    assert cfg.grader == "rubric" and cfg.promote is False

    async def go():
        ctx, _ = night_ctx(live, scripted([]))
        try:
            await lab_night.make_propose(cfg, scripts=SCRIPTS, members={102, 103})(ctx)
            with ctx.store.transaction():
                run = lab_store.night_run(ctx.store.conn, ctx.night)
            assert lab_night._grader(run) == "rubric"
            assert not lab_night._wants(ctx, lab_night.STAGE_SCORE)
            await lab_night.make_score(load(make_live(tmp_path / "other", MEASURE) / "mind.toml"))(ctx)
            return [k for k in ctx.failures]
        finally:
            ctx.store.close()
    (tmp_path / "other").mkdir()
    assert asyncio.run(go()) == []


TELLS = {"grader": "tells"}


@pytest.mark.parametrize("plan,allowed", [
    ({"promote": True, **TELLS}, True), ({"promote": False, **TELLS}, False),
    ({"promote": True}, False), ({"promote": True, "grader": "rubric"}, False),  # the rubric never
    ({}, False), ({"promote": 1, **TELLS}, False), ({"promote": "yes", **TELLS}, False)])
def test_a_run_promotes_only_when_its_plan_says_so(plan, allowed):
    """A run planned before the setting existed (no `promote` in its plan) only measures, and
    so does one graded by the rubric whatever its plan says."""
    assert lab_night._may_promote({"plan": plan}) is allowed


@pytest.mark.parametrize("status,plan,summary,shown", [
    ("done", {}, {"promote": True}, True), ("done", {"promote": True}, {"promote": False}, False),
    ("running", {"promote": True, **TELLS}, None, True), ("running", {"promote": False}, None, False),
    ("running", {"promote": True}, None, False),   # the rubric's plan: measures
    ("running", {}, None, False),       # planned before the setting, finished by this code: measures
    ("done", {}, {"arms": {}}, True),   # finished before the setting existed: it could promote
    ("budget", {}, {"arms": {}}, True),
    ("failed", {}, None, False), ("skipped", {}, None, False)])  # older code: promoted nothing
def test_the_report_says_measure_only_as_the_night_decides_it(status, plan, summary, shown):
    assert report._promote({"status": status, "plan": plan, "summary": summary}) is shown


@pytest.mark.parametrize("toml,models", [
    (DEFAULTS, ["reflection", "live", "reflection"]),       # the rubric: nothing to score
    (MEASURE, ["reflection", "live", "live", "reflection"]),  # the judge, then the blind judge
    (NO_JUDGE, ["reflection", "live", "live"])])             # nothing for the reflection model
def test_lab_run_loads_a_model_only_for_a_stage_with_work(tmp_path, monkeypatch, toml, models):
    """`lab run` swaps models as the night's runner does: never for a stage with nothing to do."""
    from leeroy_mind.config import load_config
    from leeroy_mind.lab.cli import lab_main
    from leeroy_mind.nightly.gpu import GpuArbiter
    from leeroy_mind.testing.fake_llm import FakeLLM
    live = make_live(tmp_path, toml)
    mind = load_config(live / "mind.toml").models
    names = {mind.reflection: "reflection", mind.live: "live"}
    loaded = []

    async def ensure_model(self, model):
        loaded.append(names.get(model, model))
    monkeypatch.setattr(GpuArbiter, "ensure_model", ensure_model)

    def make_llm(cfg):
        return FakeLLM(scripted([]), models={cfg.models.reflection, cfg.models.live, cfg.models.embedding})
    code = lab_main(["--data-dir", str(live), "run", "--scripts", ",".join(sorted(SCRIPTS)),
                     "--members", "102,103", "--pairs", "4"], make_llm=make_llm,
                    make_child=lambda d, s: InProcessChild(d, s, answer, tmp_path))
    assert code == 0 and loaded == models
