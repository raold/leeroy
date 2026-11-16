"""The photo critic's parts without a night (selfies/critic.py, judge.py, params.py): the
worker's answer parsed, the verdicts, the vision judge on a FakeLLM, the rows and their settling,
the parameter bandit, the gallery's flag, the CLI and the migration. Every person and photo here
is invented."""
import asyncio
import base64
import json
import logging
import sqlite3
from types import SimpleNamespace

import pytest

from conftest import run
from dbtemplate import open_migrated
from leeroy_mind.llm.client import GpuPaused
from leeroy_mind.selfies import critic, files, params
from leeroy_mind.selfies.config import SelfieCfg, load_selfie_config
from leeroy_mind.selfies.judge import MODEL, SCHEMA, Judged, VisionJudge, parse
from leeroy_mind.storage.store import Store
from leeroy_mind.testing.fake_clock import FakeClock
from leeroy_mind.testing.fake_llm import FakeLLM
from selfies.fakes import CLOCK, JPEG, fake_worker, ready, renderable_cast

ON = SelfieCfg(enabled=True, snaps=True, critic=True, best_of=3)
BANDIT = SelfieCfg(enabled=True, snaps=True, critic=True, best_of=3, param_bandit=True)
CLEAN = {"limbs": "ok", "hands": "ok", "fingers_ok": True, "eyes_ok": True,
         "extra_people": False, "same_person": True, "overall": 3}


def answer(**over):
    base = {"faces": 1, "face_score": 0.9, "hands": 2,
            "hand": [{"conf": 0.97, "fingers": 5, "plausible": True, "spread": 2.0}] * 2,
            "eyes": {"tilt": 0.5, "level": 0.01, "gaze_gap": 0.3, "iod": 80.0},
            "identity": 0.8, "identity_code": "ok",
            "detectors": {"faces": "ok", "hands": "ok"}, "seconds": 0.1}
    base.update(over)
    return base


@pytest.fixture
def store(tmp_path):
    s = Store(open_migrated(tmp_path / "mind.db"), clock=FakeClock(CLOCK))
    yield s
    s.close()


def image(tmp_path, name="a.jpg"):
    path = tmp_path / "selfies" / "101" / "snaps" / "critic" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(JPEG)
    return path


def judge_llm(*answers, models=(MODEL,)):
    replies = iter(answers)
    return FakeLLM(lambda model, messages: json.dumps(next(replies)), models=models)


# -- the worker's answer and the verdict ----------------------------------------------------------

def test_a_worker_answer_parses_and_one_off_the_protocol_is_malformed():
    got = critic.from_answer(answer())
    assert (got.faces, got.hands, got.identity, got.hands_odd) == (1, 2, 0.8, 0)
    assert dict(got.detectors) == {"faces": "ok", "hands": "ok"}
    for bad in ({"faces": -1}, {"faces": 1.5}, {"identity": 2.0}, {"identity_code": "maybe"},
                {"detectors": {"faces": "ok"}}, {"hand": [{"conf": 0.9}]},
                {"eyes": {"tilt": float("nan")}}):
        with pytest.raises(critic.Malformed):
            critic.from_answer(answer(**bad))


def test_detector_defects_reject_and_the_score_ranks_the_clean_ones():
    c = critic.from_answer(answer())
    assert critic.verdict(c).clean
    assert critic.verdict(critic.from_answer(answer(faces=2))).codes == ("extra_face",)
    assert critic.verdict(critic.from_answer(answer(faces=0))).codes == ("no_face",)
    assert critic.verdict(critic.from_answer(answer(faces=0)), face_expected=False).clean
    assert critic.verdict(critic.from_answer(answer(hands=3))).codes == ("extra_hands",)
    tilted = critic.from_answer(answer(eyes={"tilt": 12.0, "level": 0.01}))
    assert critic.verdict(tilted).codes == ("eyes_tilt",)
    judged = Judged("defect", "ok", False, True, False, True, 1)
    assert critic.verdict(c, judged).codes == ("limbs", "fingers")
    near = critic.score(critic.from_answer(answer(identity=0.85)), Judged(**{**CLEAN}))
    far = critic.score(critic.from_answer(answer(identity=0.55)), Judged(**{**CLEAN}))
    assert 0 < far < near <= 1
    not_her = Judged(**{**CLEAN, "same_person": False})
    assert critic.score(c, not_her) == pytest.approx(critic.score(c, Judged(**CLEAN)) / 2,
                                                     abs=1e-4)
    odd = critic.from_answer(answer(hand=[{"conf": 0.9, "fingers": 2, "plausible": True,
                                            "spread": 1.0}]))
    assert critic.score(odd) < critic.score(c)
    assert critic.best({1: critic.Verdict((), 0.5), 2: critic.Verdict((), 0.9),
                        3: critic.Verdict(("limbs",), 1.0)}) == 2
    assert critic.best({1: critic.Verdict(("hands",), 1.0)}) is None


# -- the vision judge ------------------------------------------------------------------------------

def test_the_judge_parses_its_schema_and_refuses_anything_else():
    assert parse(json.dumps(CLEAN), with_ref=True) == Judged("ok", "ok", True, True, False, True, 3)
    assert parse(json.dumps(CLEAN), with_ref=False).same_person is None
    for bad in ("not json", json.dumps({**CLEAN, "overall": 4}), json.dumps({**CLEAN, "limbs": "?"}),
                json.dumps({k: v for k, v in CLEAN.items() if k != "eyes_ok"}),
                json.dumps({**CLEAN, "fingers_ok": "yes"})):
        with pytest.raises(ValueError):
            parse(bad, with_ref=True)


def test_the_judge_sends_both_photos_as_images_with_its_fixed_schema(tmp_path):
    llm = judge_llm(CLEAN)
    photo, ref = image(tmp_path), image(tmp_path, "ref.jpg")
    got = run(VisionJudge(llm).judge(photo, ref))
    assert got == Judged("ok", "ok", True, True, False, True, 3)
    (call,) = llm.calls
    assert call["model"] == MODEL and call["think"] is False and call["format"] == SCHEMA
    (message,) = call["messages"]
    assert message["images"] == [base64.b64encode(JPEG).decode()] * 2
    assert "second image shows the same woman" in message["content"]
    assert call["options"]["temperature"] == 0


def test_an_absent_model_is_skipped_with_one_log_line(tmp_path, caplog):
    judge = VisionJudge(judge_llm(models=("someone-else:7b",)))
    with caplog.at_level(logging.INFO, logger="leeroy_mind"):
        assert run(judge.judge(image(tmp_path))) is None
        assert run(judge.judge(image(tmp_path))) is None
    assert caplog.text.count("critic_judge_absent") == 1 and not judge.used


def test_a_malformed_answer_or_a_paused_gpu_skips_the_photo(tmp_path, caplog):
    llm = FakeLLM(lambda model, messages: "the hands look fine", models={MODEL})
    with caplog.at_level(logging.INFO, logger="leeroy_mind"):
        assert run(VisionJudge(llm).judge(image(tmp_path))) is None
    assert "critic_judge_skipped: code=judge_not_json" in caplog.text
    assert "the hands look fine" not in caplog.text

    class Paused(FakeLLM):
        async def chat(self, *a, **kw):
            raise GpuPaused("paused")
    judge = VisionJudge(Paused(models={MODEL}))
    assert run(judge.judge(image(tmp_path))) is None and judge.paused
    assert run(judge.judge(image(tmp_path))) is None  # the rest of the pass is skipped


# -- rows, settling and the migration -----------------------------------------------------------

def test_the_migration_adds_the_critic_and_bandit_tables(store):
    names = {r[0] for r in store.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"selfie_critic", "selfie_param_arm", "selfie_param_credit"} <= names
    with pytest.raises(sqlite3.IntegrityError):  # a set's row names its slot
        store.conn.execute("INSERT INTO selfie_critic(ts, run, purpose, guid, seed, file) "
                           "VALUES (1, 'n', 'set', 101, 1, 'f')")


def _row(store, tmp_path, name, measured, **over):
    path = image(tmp_path, name)
    return critic.insert(store, run="n1", purpose="snap", guid=101, snap_id=7, seed=1,
                         file=critic.rel(tmp_path, path), critique=measured,
                         framing="upper_body", scale=0.45, steps=30, now=1.0, **over)


def test_settle_judges_the_pending_rows_and_best_picks_the_clean_one(store, tmp_path, caplog):
    a = _row(store, tmp_path, "a.jpg", critic.from_answer(answer(identity=0.6)))
    b = _row(store, tmp_path, "b.jpg", critic.from_answer(answer(identity=0.85)))
    c = _row(store, tmp_path, "c.jpg", critic.from_answer(answer(faces=2)))
    gone = critic.insert(store, run="n1", purpose="snap", guid=101, snap_id=7, seed=4,
                         file="selfies/101/snaps/critic/gone.jpg",
                         critique=critic.from_answer(answer()), now=1.0)
    llm = judge_llm(CLEAN, {**CLEAN, "hands": "defect", "overall": 1}, CLEAN)
    seen = []
    with caplog.at_level(logging.INFO, logger="leeroy_mind"):
        verdicts = run(critic.settle(store, tmp_path, VisionJudge(llm), ids=[a, b, c, gone],
                                     now=2.0, on_settled=lambda r, v, j: seen.append(r["id"])))
    assert verdicts[a].clean and verdicts[b].codes == ("hands",)
    assert verdicts[c].codes == ("extra_face",) and verdicts[gone].clean  # no file: detectors
    assert critic.best(verdicts) == gone or critic.best(verdicts) == a
    assert sorted(seen) == sorted([a, b, c, gone]) and len(llm.calls) == 3
    rows = dict(store.conn.execute("SELECT id, verdict FROM selfie_critic").fetchall())
    assert rows == {a: "clean", b: "flagged", c: "flagged", gone: "clean"}
    judged = store.conn.execute("SELECT judge_model, overall, hands_judged FROM selfie_critic "
                                "WHERE id=?", (b,)).fetchone()
    assert tuple(judged) == (MODEL, 1, "defect")
    assert "critic_settled: rows=4 judged=3 flagged=2" in caplog.text
    assert "snaps/critic" not in caplog.text and "base64" not in caplog.text


def test_without_the_judge_the_detectors_decide(store, tmp_path):
    a = _row(store, tmp_path, "a.jpg", critic.from_answer(answer()))
    b = _row(store, tmp_path, "b.jpg", critic.from_answer(answer(hands=4)))
    verdicts = run(critic.settle(store, tmp_path, None, run="n1", now=2.0))
    assert verdicts[a].clean and verdicts[b].codes == ("extra_hands",)
    assert store.conn.execute("SELECT count(*) FROM selfie_critic WHERE judge_model IS NULL"
                              ).fetchone()[0] == 2


def test_the_report_counts_defect_rates_by_what_was_rendered(store, tmp_path):
    a = _row(store, tmp_path, "a.jpg", critic.from_answer(answer()), outfit="casual", prop="mug")
    b = _row(store, tmp_path, "b.jpg", critic.from_answer(answer(faces=2)), outfit="casual",
             prop="none")
    run(critic.settle(store, tmp_path, None, ids=[a, b], now=2.0))
    got = critic.report(store.conn)
    assert got["rows"] == 2 and got["pending"] == 0
    assert got["framing"]["upper_body"] == {"n": 2, "flagged": 0.5,
                                            "codes": {"extra_face": 0.5}, "overall": None}
    assert got["prop"]["mug"]["flagged"] == 0.0 and got["prop"]["none"]["flagged"] == 1.0


# -- the parameter bandit -----------------------------------------------------------------------

def test_the_bandit_off_or_frozen_draws_nothing_and_writes_nothing(store, tmp_path, caplog):
    for cfg in (SelfieCfg(), ON):
        got = params.choose(store, cfg, tmp_path, shape="portrait", framing="upper_body",
                            control=(0.45, 30))
        assert got == params.Params(0.45, 30, False)
    store.set_meta("learn:control:freeze", "true")
    with caplog.at_level(logging.INFO, logger="leeroy_mind"):
        got = params.choose(store, BANDIT, tmp_path, shape="portrait", framing="upper_body",
                            control=(0.45, 30))
    assert got == params.Params(0.45, 30, False) and "selfie_param_chosen" not in caplog.text
    assert store.conn.execute("SELECT count(*) FROM selfie_param_arm").fetchone()[0] == 0


def test_the_bandit_draws_inside_its_grid_and_learns_from_credit(store, tmp_path, caplog):
    import random
    rng = random.Random(5)
    with caplog.at_level(logging.INFO, logger="leeroy_mind"):
        draws = [params.choose(store, BANDIT, tmp_path, shape="portrait", framing="upper_body",
                               control=(0.45, 30), rng=rng, now=1000.0) for _ in range(20)]
    assert all(d.arm and d.scale in params.SCALES and d.steps in params.STEPS for d in draws)
    assert "selfie_param_chosen: framing=portrait:upper_body scale=" in caplog.text
    drawn = store.conn.execute("SELECT sum(drawn) FROM selfie_param_arm").fetchone()[0]
    assert drawn == 20
    good = {"id": 1, "scale": 0.3, "steps": 40, "framing": "upper_body"}
    for i in range(40):  # 0.3 / 40 always clean, the rest flagged
        row = {**good, "id": i + 1}
        params.credit_row(store, BANDIT, tmp_path, row, critic.Verdict((), 1.0), None,
                          shape="portrait", now=1000.0)
        bad = {"id": 100 + i, "scale": 0.6, "steps": 30, "framing": "upper_body"}
        params.credit_row(store, BANDIT, tmp_path, bad, critic.Verdict(("limbs",), 0.0), None,
                          shape="portrait", now=1000.0)
    later = [params.choose(store, BANDIT, tmp_path, shape="portrait", framing="upper_body",
                           control=(0.45, 30), rng=rng, now=1000.0) for _ in range(30)]
    assert sum((d.scale, d.steps) == (0.3, 40) for d in later) > 15
    assert not any((d.scale, d.steps) == (0.6, 30) for d in later)
    off_grid = {"id": 999, "scale": 0.7, "steps": 30, "framing": "upper_body"}
    assert not params.credit_row(store, BANDIT, tmp_path, off_grid, critic.Verdict((), 1.0),
                                 None, shape="portrait", now=1000.0)


def test_his_pick_credits_the_picked_candidate_and_an_undo_takes_it_back(store, tmp_path):
    from leeroy_mind.selfies.store import pick, undo
    from conftest import seed_test_cast
    seed_test_cast(store)
    renderable_cast(store, 101)
    ready(store, tmp_path, 101, 1)
    for idx in range(4):
        critic.insert(store, run="n1", purpose="set", guid=101, set_no=1, idx=idx, seed=idx,
                      file=f"selfies/101/sets/1/c{idx}.jpg", critique=None,
                      framing="upper_body", scale=0.6 if idx else 0.3, steps=30, now=1.0)
    first = pick(store, 101, 1, 0, source="console", now=2.0)
    assert params.credit_picks(store, BANDIT, tmp_path, now=3.0) == 1
    credit = dict(store.conn.execute("SELECT source, success FROM selfie_param_credit"))
    assert credit[f"pick:{first}:0"] == 1.0 and credit[f"pick:{first}:1"] == 0.0
    undo(store, first, source="console", now=4.0)
    assert params.credit_picks(store, BANDIT, tmp_path, now=5.0) == 1
    assert store.conn.execute("SELECT count(*) FROM selfie_param_credit").fetchone()[0] == 0
    assert params.credit_picks(store, ON, tmp_path, now=6.0) == 0  # the bandit off: nothing


# -- config ------------------------------------------------------------------------------------

def test_the_critic_switches_default_off_and_an_invalid_table_keeps_them_off(tmp_path):
    cfg = load_selfie_config(tmp_path / "missing.toml")
    assert (cfg.critic, cfg.best_of, cfg.critic_model, cfg.param_bandit) == (
        False, 1, "qwen2.5vl:7b", False)
    (tmp_path / "mind.toml").write_text("[selfies]\nenabled = true\ncritic = true\nbest_of = 3\n"
                                        "param_bandit = true\ncritic_model = \"x-vl:3b\"\n")
    cfg = load_selfie_config(tmp_path / "mind.toml")
    assert (cfg.critic, cfg.best_of, cfg.critic_model, cfg.param_bandit) == (
        True, 3, "x-vl:3b", True)
    (tmp_path / "mind.toml").write_text("[selfies]\ncritic = true\nbest_of = 3\nbogus = 1\n")
    cfg = load_selfie_config(tmp_path / "mind.toml")
    assert (cfg.critic, cfg.best_of, cfg.param_bandit) == (False, 1, False)
    from leeroy_mind.config import ConfigError
    for line in ("best_of = 0", "best_of = 5", "critic_model = ''", "critic = 1"):
        (tmp_path / "mind.toml").write_text(f"[selfies]\n{line}\n")
        with pytest.raises(ConfigError):
            load_selfie_config(tmp_path / "mind.toml")


# -- the gallery and the CLI ---------------------------------------------------------------------

def test_the_gallery_marks_a_flagged_candidate_with_its_codes(store, tmp_path):
    from conftest import seed_test_cast
    from leeroy_mind.console.galleryview import gallery
    seed_test_cast(store)
    renderable_cast(store, 101)
    ready(store, tmp_path, 101, 1)
    row = critic.insert(store, run="n1", purpose="set", guid=101, set_no=1, idx=2, seed=2,
                        file="selfies/101/sets/1/c2.jpg",
                        critique=critic.from_answer(answer(hands=3)), now=1.0)
    run(critic.settle(store, tmp_path, None, ids=[row], now=2.0))
    photos = gallery(store.conn, tmp_path, 101)["photos"]
    flags = {p["index"]: p.get("flag") for p in photos}
    assert flags == {0: None, 1: None, 2: "extra_hands", 3: None}


def test_critic_report_reads_an_older_database_as_not_set_up(tmp_path):
    from leeroy_mind.selfies.cli import _critic_report
    conn = sqlite3.connect(":memory:")
    assert _critic_report(SimpleNamespace(conn=conn)) == {"missing": ["selfie_critic"]}


def test_the_critic_command_measures_then_judges_with_the_worker_gone(tmp_path, capsys):
    from leeroy_mind.selfies.cli import selfies_main
    data = tmp_path / "data"
    data.mkdir()
    open_migrated(data / "mind.db").close()
    (data / "mind.toml").write_text("[selfies]\nenabled = true\n")
    photo = tmp_path / "photo.jpg"
    photo.write_bytes(JPEG)
    made = []
    factory = fake_worker(tmp_path)

    def make_worker():
        made.append(factory())
        return made[-1]

    def script(model, messages):
        assert not made[0].running  # the worker exited before the judge was asked
        return json.dumps({**CLEAN, "hands": "defect", "overall": 1})
    code = selfies_main(["critic", "--data-dir", str(data), "--path", str(photo)],
                        make_worker=make_worker,
                        make_llm=lambda cfg: FakeLLM(script, models={MODEL}))
    out = json.loads(capsys.readouterr().out)
    assert code == 0
    assert (out["faces"], out["verdict"], out["codes"], out["judge"]["overall"]) == (
        1, "flagged", ["hands"], 1)
    assert out["identity"] is None and out["identity_code"] == "no_ref"
