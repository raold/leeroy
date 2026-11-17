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
GOOD, PLAIN = "yes, tonight works for me", "ha maybe, depends on the day"
MEASURE = LIVE_TOML.replace('grader = "rubric"\npromote = true\n', 'grader = "tells"\n')
PROMOTE = LIVE_TOML.replace('grader = "rubric"\n', 'grader = "tells"\n')
DEFAULTS = LIVE_TOML.replace('grader = "rubric"\npromote = true\n', "")
assert MEASURE != LIVE_TOML and PROMOTE != LIVE_TOML and DEFAULTS != LIVE_TOML


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


def run_night(live, tmp_path, *, hold_before_score=False):
    cfg = load(live / "mind.toml")
    seen = []

    async def go():
        ctx, llm = night_ctx(live, scripted(seen))
        try:
            def make_child(data_dir, seed):
                return InProcessChild(data_dir, seed, answer, tmp_path)
            await lab_night.make_propose(cfg, scripts=SCRIPTS, members={102, 103})(ctx)
            await lab_night.make_battery(cfg, make_child=make_child)(ctx)
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


def test_a_run_keeps_the_grader_it_began_with(tmp_path):
    """A run planned under the rubric is graded by the rubric even if the setting changed."""
    live = make_live(tmp_path)   # the rig's table: grader rubric, promote on
    cfg = load(live / "mind.toml")
    assert cfg.grader == "rubric" and cfg.promote is True

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
