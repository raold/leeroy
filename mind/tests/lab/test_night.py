"""The lab's three night stages end to end on an invented cast: a scripted reflection model
(proposer, rubric, blind judge), the real sandbox in this process with a FakeLLM live model, and
the learn bandit receiving the winner. The live prompts dir is never written."""
import asyncio
import json

import pytest

from conftest import HUMAN
from lab.rig import InProcessChild, digest, judge_reply, night_ctx, title_of, variant_text
from leeroy_mind.lab import night as lab_night
from leeroy_mind.lab import store as lab_store
from leeroy_mind.lab.config import load
from leeroy_mind.learn import arms
from leeroy_mind.talk.beat import MARK

WIN, LOSE = "Plainly", "Truly"
SCRIPTS = {"flirt-3", "smalltalk-3", "meet-3", "pushback-3"}


def answer(model, messages):
    """The sandbox's live model: a beat when asked, else her line, by the instruction in force."""
    last = messages[-1]["content"]
    if MARK in last:
        return json.dumps({"read": "he says hi", "lands": "warm", "want": "share", "use": None})
    if f"{WIN} respond to what" in last:
        return "yes, tonight works for me"
    return "ha maybe, depends on the day"


def reflection(model, messages):
    title = title_of(messages)
    text = messages[-1]["content"]
    if title == "lab_variants":
        return json.dumps({"variants": [
            {"text": variant_text(WIN), "idea": "plainer"},
            {"text": variant_text(LOSE), "idea": "truer"},
            {"text": "Answer them.", "idea": "too short"}]})
    if title == "lab_rubric":
        n = len([line for line in text.splitlines() if line.startswith("her (reply ")])
        grades = []
        for i, line in enumerate((x for x in text.splitlines() if x.startswith("her (reply ")), 1):
            good = "tonight works" in line
            grades.append({"reply": i, "score": 3 if good else 1, "code": "passes" if good else "generic"})
        assert len(grades) == n
        return json.dumps({"grades": grades})
    if title == "turing_judge":
        return judge_reply(messages)
    raise AssertionError(f"unexpected call {title}")


def run_night(live, tmp_path, *, cfg=None, before=None):
    cfg = cfg or load(live / "mind.toml")

    async def go():
        ctx, llm = night_ctx(live, reflection)
        try:
            if before is not None:
                before(ctx)
            children = []

            def make_child(data_dir, seed):
                c = InProcessChild(data_dir, seed, answer, tmp_path)
                children.append(c)
                return c
            await lab_night.make_propose(cfg, scripts=SCRIPTS, members={102, 103})(ctx)
            await lab_night.make_battery(cfg, make_child=make_child)(ctx)
            await lab_night.make_grade(cfg)(ctx)
            with ctx.store.transaction():
                run = lab_store.night_run(ctx.store.conn, ctx.night)
                variants = lab_store.variants(ctx.store.conn, run["id"]) if run else []
                scores = lab_store.scores(ctx.store.conn, run["id"]) if run else {}
                registry = {a.id: a for a in arms.read(ctx.store.conn)}
            return run, variants, scores, registry, ctx.failures, children
        finally:
            ctx.store.close()
    return asyncio.run(go())


def test_a_night_measures_tests_variants_and_promotes_the_winner(live, tmp_path):
    prompts_before = digest(live / "prompts")
    run, variants, scores, registry, failures, children = run_night(live, tmp_path)
    assert not failures, dict(failures)
    assert run["status"] == "done" and run["piece"] == "reply_human"
    assert run["spent"] >= 0 and len(run["plan"]["pairs"]) == 4
    # the third proposal dropped its placeholders: refused by the screen, never stored
    assert [v["text"] for v in variants] == [variant_text(WIN), variant_text(LOSE)]
    win, lose = variants
    assert win["status"] == "promoted" and win["outcome"] == "won", win["scores"]
    assert lose["status"] == "retired" and lose["outcome"] == "lost"
    sheet = json.loads(win["scores"])
    assert sheet["delta"] == pytest.approx(2.0) and sheet["graded"] == 8 and sheet["applied"] == 8
    assert sheet["regressed"] == []
    # the winner is an active arm of the bandit's family for its piece, with a control
    arm = registry[win["arm_id"]]
    assert arm.family == "prompt.reply_human" and arm.status == "active"
    assert arm.params == {"piece": "reply_human", "variant": win["id"]}
    assert registry["prompt.reply_human.control.v1"].status == "control"
    # every arm ran the same pairs and turns; the control never read a variant
    assert set(scores) == {"control", f"v{win['id']}", f"v{lose['id']}"}
    assert {k for k in scores["control"]} == {k for k in scores[f"v{win['id']}"]}
    assert all(r["applied"] == 0 for r in scores["control"].values())
    assert all(r["rubric"] is not None for rows in scores.values() for r in rows.values())
    judged = [r for r in scores["control"].values() if r["judged_human"] is not None]
    assert judged and {r["pair"] for r in judged} == {1, 2}
    # one child, stopped; the live prompts dir is as it was, the overlays only in the copy
    assert len(children) == 1 and not children[0].killed
    assert digest(live / "prompts") == prompts_before
    copy = live / "scratch" / "lab" / "mind-data" / "prompts"
    assert (copy / "romance_explicit.txt").read_bytes() == (live / "prompts" / "romance_explicit.txt").read_bytes()


def test_a_resumed_night_runs_nothing_again(live, tmp_path):
    run_night(live, tmp_path)
    run, variants, scores, registry, failures, children = run_night(live, tmp_path)
    assert run["status"] == "done" and not children and not failures
    assert sum(1 for a in registry.values() if a.id.startswith("lab.")) == 1


def test_the_budget_cuts_the_battery_and_settles_every_variant(live, tmp_path):
    def spent_most(ctx):
        # the plan and proposals as usual, then the budget is gone before the battery
        pass
    cfg = load(live / "mind.toml")

    async def go():
        ctx, _ = night_ctx(live, reflection)
        try:
            await lab_night.make_propose(cfg, scripts=SCRIPTS, members={102, 103})(ctx)
            with ctx.store.transaction():
                run = lab_store.night_run(ctx.store.conn, ctx.night)
                ctx.store.conn.execute("UPDATE lab_run SET spent=? WHERE id=?",
                                       (cfg.minutes * 60 * lab_night.BATTERY_SHARE + 1, run["id"]))
            made = []
            await lab_night.make_battery(cfg, make_child=lambda d, s: made.append(s))(ctx)
            await lab_night.make_grade(cfg)(ctx)
            with ctx.store.transaction():
                run = lab_store.night_run(ctx.store.conn, ctx.night)
                variants = lab_store.variants(ctx.store.conn, run["id"])
            return run, variants, made
        finally:
            ctx.store.close()
    run, variants, made = asyncio.run(go())
    assert not made  # no child was started past the battery's share
    assert run["status"] == "budget"
    assert {v["outcome"] for v in variants} == {"budget"}
    assert all(v["status"] == "retired" and v["arm_id"] is None for v in variants)


def test_a_held_lease_makes_the_battery_wait(live, tmp_path):
    cfg = load(live / "mind.toml")

    async def go():
        ctx, _ = night_ctx(live, reflection)
        try:
            await lab_night.make_propose(cfg, scripts=SCRIPTS, members={102, 103})(ctx)
            ctx.arbiter.hold("human")
            made = []
            assert not lab_night._wants(ctx, lab_night.STAGE_GRADE)
            await lab_night.make_battery(cfg, make_child=lambda d, s: made.append(s))(ctx)
            await lab_night.make_grade(cfg)(ctx)
            with ctx.store.transaction():
                run = lab_store.night_run(ctx.store.conn, ctx.night)
            return run, made
        finally:
            ctx.store.close()
    run, made = asyncio.run(go())
    assert not made and run["status"] == "running"


def test_off_attaches_nothing(tmp_path):
    from lab.rig import make_live
    live = make_live(tmp_path, "[lab]\nenabled = false\n")

    class Runner:
        stages = ()

        def add_stage(self, *a, **k):
            raise AssertionError("no stage")
    assert lab_night.attach(Runner(), live) is False
