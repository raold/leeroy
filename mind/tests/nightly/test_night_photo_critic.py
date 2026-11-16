"""The photo critic at night ([selfies] critic): best-of-N situational photos, the judge after
the worker has exited, a situation with no clean seed held for a later night, the profile set's
critic rows, the parameter bandit's draws, and the switch off leaving tonight's path as it was.
The fake identity worker renders and measures (no torch, no model, no GPU); the judge is a
FakeLLM. Every person and scene here is invented."""
import json
import logging

from conftest import run
from leeroy_mind.selfies import params
from leeroy_mind.selfies.config import SelfieCfg
from leeroy_mind.selfies.files import snap, snap_candidate
from leeroy_mind.selfies.judge import MODEL
from leeroy_mind.selfies.snapstage import STAGE, make_snaps_stage
from leeroy_mind.selfies.stage import STAGE as SETS, make_stage, seed_for
from leeroy_mind.selfies.worker import SelfieWorker
from selfies.fakes import fake_command, renderable_cast
from nightly.test_night_snaps import IDENTITY, cast, lines, situation, snap_row

CRITIC = SelfieCfg(enabled=True, snaps=True, critic=True, best_of=3)
CLEAN = {"limbs": "ok", "hands": "ok", "fingers_ok": True, "eyes_ok": True,
         "extra_people": False, "same_person": True, "overall": 3}


def critic_rows(h, **where):
    sql = " AND ".join(f"{k}=?" for k in where) or "1"
    return [dict(zip(("seed", "verdict", "codes", "chosen", "judge_model", "scale", "steps",
                      "purpose"), r)) for r in h.store.conn.execute(
        f"SELECT seed, verdict, codes, chosen, judge_model, scale, steps, purpose "
        f"FROM selfie_critic WHERE {sql} ORDER BY id", tuple(where.values()))]


class Tracked:
    """A worker factory that keeps its workers, so a judge can see whether they still run."""

    def __init__(self, tmp_path, **env):
        self.cmd, self.env = fake_command(tmp_path, **IDENTITY, **env)
        self.made = []

    def __call__(self):
        self.made.append(SelfieWorker(self.cmd, env=self.env))
        return self.made[-1]


def judging(ctx, answers, tracked=None, seen=None):
    """The night's FakeLLM lists the critic model and answers the judge from `answers` in turn
    (the last one again once they run out), checking the worker is gone first."""
    llm = ctx.reasoner.llm
    llm.set_models(llm._models | {MODEL})
    replies = list(answers)

    def script(model, messages):
        assert model == MODEL and messages[0]["images"]
        if tracked is not None:
            assert not any(w.running for w in tracked.made)
        if seen is not None:
            seen.append(model)
        return json.dumps(replies.pop(0) if len(replies) > 1 else replies[0])
    llm._script = script


def test_best_of_three_sends_the_clean_seed_with_the_best_score(h, tmp_path, caplog):
    cast(h, 101); s = situation(h, 101)
    seeds = [seed_for(h.night, 101, s, k, 0) for k in range(3)]
    log = tmp_path / "requests.log"
    worker = Tracked(tmp_path, FAKE_SELFIE_LOG=str(log), FAKE_SELFIE_CRITIC_BAD=str(seeds[0]),
                     FAKE_SELFIE_CRITIC_ID=json.dumps({str(seeds[1]): 0.6, str(seeds[2]): 0.85}))
    ctx = h.ctx()
    with caplog.at_level(logging.INFO, logger="leeroy_mind"):
        run(make_snaps_stage(CRITIC, worker=worker)(ctx))
    rendered = [line.split()[:2] for line in lines(log) if " critic " not in line]
    assert rendered == [[f"s{s}:{k}:0", str(seeds[k])] for k in range(3)]
    assert sum(" critic " in line for line in lines(log)) == 3
    rows = critic_rows(h, snap_id=s)
    assert [(r["seed"], r["verdict"], r["codes"], r["chosen"]) for r in rows] == [
        (seeds[0], "flagged", "extra_face", 0), (seeds[1], "clean", "", 0),
        (seeds[2], "clean", "", 1)]
    row = snap_row(h.store, s)
    assert row.status == "ready" and row.seed == seeds[2] and row.tries == 1
    data = h.settings.data_dir
    assert snap(data, 101, s).is_file() and snap(data, 101, s, thumb=True).is_file()
    assert all(snap_candidate(data, 101, s, k).is_file() for k in seeds)  # every seed kept
    assert ctx.done(STAGE, f"snap:{s}") and not ctx.failures
    assert f"selfies_snap_ready: night={h.night} snap={s}" in caplog.text
    assert "seeds=3 flagged=1" in caplog.text
    assert "selfies_snaps_done: night=2026-10-29 ready=0 expired=0 held=0 failed=0 measured=1" \
        in caplog.text
    assert str(data) not in caplog.text  # paths never reach a log


def test_the_judge_runs_after_the_worker_exits_and_its_defect_rejects_a_seed(h, tmp_path, caplog):
    cast(h, 101); s = situation(h, 101)
    seeds = [seed_for(h.night, 101, s, k, 0) for k in range(3)]
    worker = Tracked(tmp_path)
    ctx = h.ctx()
    seen = []
    judging(ctx, [{**CLEAN, "limbs": "defect", "overall": 1}, {**CLEAN, "overall": 2}, CLEAN],
            tracked=worker, seen=seen)
    with caplog.at_level(logging.INFO, logger="leeroy_mind"):
        run(make_snaps_stage(CRITIC, worker=worker)(ctx))
    assert len(seen) == 3
    rows = critic_rows(h, snap_id=s)
    assert [(r["verdict"], r["codes"], r["chosen"], r["judge_model"]) for r in rows] == [
        ("flagged", "limbs", 0, MODEL), ("clean", "", 0, MODEL), ("clean", "", 1, MODEL)]
    assert snap_row(h.store, s).seed == seeds[2]
    assert ("unload", MODEL) in ctx.arbiter.admin.calls  # the judge leaves the GPU after it
    assert "critic_settled: rows=3 judged=3 flagged=1" in caplog.text
    assert "defect" not in caplog.text.replace("critic_settled", "")  # no judge words logged


def test_a_situation_with_no_clean_seed_stays_planned_for_another_night(h, tmp_path, caplog):
    cast(h, 101); s = situation(h, 101)
    seeds = ",".join(str(seed_for(h.night, 101, s, k, 0)) for k in range(3))
    ctx = h.ctx()
    with caplog.at_level(logging.INFO, logger="leeroy_mind"):
        run(make_snaps_stage(CRITIC, worker=Tracked(tmp_path, FAKE_SELFIE_CRITIC_BAD=seeds))(ctx))
    row = snap_row(h.store, s)
    assert row.status == "planned" and row.rendered is None
    assert not snap(h.settings.data_dir, 101, s).exists()
    assert ctx.done(STAGE, f"snap:{s}") and not ctx.failures
    assert f"selfies_snap_held: night={h.night} snap={s} reason=critic seeds=3 flagged=3" \
        in caplog.text
    assert [r["verdict"] for r in critic_rows(h, snap_id=s)] == ["flagged"] * 3


def test_with_the_critic_off_the_night_renders_one_seed_and_measures_nothing(h, tmp_path):
    cast(h, 101); s = situation(h, 101)
    log = tmp_path / "requests.log"
    off = SelfieCfg(enabled=True, snaps=True, best_of=3)  # best_of needs the critic
    ctx = h.ctx()
    run(make_snaps_stage(off, worker=Tracked(tmp_path, FAKE_SELFIE_LOG=str(log)))(ctx))
    assert [line.split()[:2] for line in lines(log)] == [
        [f"s{s}:0", str(seed_for(h.night, 101, s, 0, 0))]]
    assert critic_rows(h) == []
    assert snap_row(h.store, s).status == "ready"
    assert not snap_candidate(h.settings.data_dir, 101, s, seed_for(h.night, 101, s, 0, 0)).exists()


def test_the_bandit_picks_scale_and_steps_from_its_grid(h, tmp_path, caplog):
    cast(h, 101); s = situation(h, 101)
    cfg = SelfieCfg(enabled=True, snaps=True, critic=True, best_of=2, param_bandit=True)
    log = tmp_path / "requests.log"
    ctx = h.ctx()
    with caplog.at_level(logging.INFO, logger="leeroy_mind"):
        run(make_snaps_stage(cfg, worker=Tracked(tmp_path, FAKE_SELFIE_LOG=str(log),
                                                 FAKE_SELFIE_LOG_STEPS="1"))(ctx))
    renders = [line for line in lines(log) if " critic " not in line]
    assert len(renders) == 2
    for line in renders:
        parts = line.split()
        assert float(parts[parts.index("scale") + 1]) in params.SCALES
        assert int(parts[parts.index("steps") + 1]) in params.STEPS
    assert caplog.text.count("selfie_param_chosen: framing=portrait:upper_body") == 2
    rows = critic_rows(h, snap_id=s)
    assert all(r["scale"] in params.SCALES and r["steps"] in params.STEPS for r in rows)
    credited = h.store.conn.execute("SELECT count(*) FROM selfie_param_credit").fetchone()[0]
    assert credited == 2  # each settled seed credits its parameters


def test_a_profile_set_gets_a_row_per_candidate_settled_after_the_worker(h, tmp_path):
    renderable_cast(h.store, 101)
    ctx = h.ctx()
    run(make_stage(SelfieCfg(enabled=True, critic=True),
                   worker=Tracked(tmp_path))(ctx))
    rows = critic_rows(h, purpose="set")
    assert len(rows) == 4 and {r["verdict"] for r in rows} == {"clean"}
    assert all(r["steps"] == 30 and r["scale"] is None for r in rows)  # a first set: no face
    assert ctx.done(SETS, "101:1") and not ctx.failures


def test_a_resumed_night_measures_each_seed_once(h, tmp_path):
    cast(h, 101); s = situation(h, 101)
    seeds = ",".join(str(seed_for(h.night, 101, s, k, 0)) for k in range(3))
    from leeroy_mind.selfies import critic
    critic.insert(h.store, run=h.night, purpose="snap", guid=101, snap_id=s, seed=1,
                  file="selfies/101/snaps/critic/x.jpg", critique=None, now=1.0)  # a cut-off run's
    ctx = h.ctx()
    run(make_snaps_stage(CRITIC, worker=Tracked(tmp_path, FAKE_SELFIE_CRITIC_BAD=seeds))(ctx))
    assert len(critic_rows(h, snap_id=s)) == 3
