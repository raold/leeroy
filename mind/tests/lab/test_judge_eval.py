"""`lab judge-eval`: the judges against invented gold conversations, numbers only (FakeLLM)."""
import asyncio
import json
import random

import pytest

from leeroy_mind.lab import gold, judge, judge_eval
from leeroy_mind.lab.cli import lab_main
from leeroy_mind.nightly.llm import Reasoner
from leeroy_mind.testing.fake_llm import FakeLLM

GOOD = ("yeah after nine", "got four, need six more", "nah im wiped", "haha ok fine", "sure, when")
BAD = ("That sounds wonderful and I truly appreciate you sharing that with me today",
       "As I mentioned before, communication really is the foundation of everything we do",
       "I completely understand how you feel and I am always here for you no matter what")
SECRET = "zebrafish"   # a word only the gold holds: it must never reach stdout


def make_gold(tmp_path, convs=10, turns=8):
    folder = tmp_path / "gold"
    folder.mkdir()
    for c in range(convs):
        rows = []
        for t in range(turns):
            bad = (c + t) % 3 == 0
            text = (BAD[(c + t) % len(BAD)] if bad else GOOD[(c + t) % len(GOOD)]) + f" {SECRET}"
            rows.append({"id": 2 * t, "who": "him", "text": f"so what about the {SECRET} raid number {t}?"})
            rows.append({"id": 2 * t + 1, "who": "her", "answers": 2 * t, "text": text,
                         "grade": {"score": 2 if bad else 4 + (t % 2), "flags": "T" if bad else ""}})
        (folder / f"x-conv-{c}.json").write_bytes(json.dumps(
            {"guid": c, "case": "invented", "card": f"A card about {SECRET}.", "turns": rows}).encode())
    return folder


def script(model, messages):
    """A judge that sees what the invented gold's grader saw: the long polished replies."""
    text = messages[-1]["content"]
    last = text.rsplit("Reply with only a JSON object", 1)[0]
    if "Question: " in last:
        question = last.rsplit("Question: ", 1)[1].strip()
        _, _, bad, _ = next(t for t in judge.TELLS if t[1] == question)
        reply = last.rsplit("Her reply", 1)[1]
        flawed = any(b[:20] in reply for b in BAD)
        good = "no" if bad == "yes" else "yes"
        return json.dumps({"evidence": "", "answer": bad if flawed else good})
    if "Exchange A" in last:
        a, b = last.split("Exchange B")
        fa, fb = (any(x[:20] in part for x in BAD) for part in (a, b))
        return json.dumps({"better": "same" if fa == fb else ("B" if fa else "A")})
    if last.rstrip().endswith("grade:"):
        return json.dumps({"grade": 2 if any(b[:20] in last.rsplit("her reply", 1)[1] for b in BAD) else 5})
    if "Grade replies 1 to" in last:
        n = int(last.rsplit("Grade replies 1 to ", 1)[1].split(".")[0])
        return json.dumps({"grades": [{"reply": i, "score": 2, "code": "in_voice"} for i in range(1, n + 1)]})
    return json.dumps({"problem": "none",
                       "grade": 2 if any(b[:20] in last.rsplit("her reply", 1)[1] for b in BAD) else 5})


def reasoner():
    llm = FakeLLM(script, models={"m"})
    return Reasoner(llm, "m"), llm


def test_a_judge_that_sees_the_defect_agrees_and_the_flat_rubric_does_not(tmp_path):
    folder = make_gold(tmp_path)
    r, llm = reasoner()
    data = asyncio.run(judge_eval.evaluate(r, folder, part="test", seed=3))
    assert data["gold"] == {"conversations": 10, "replies": {"train": 48, "dev": 16, "test": 16}}
    assert set(data["designs"]) == set(judge_eval.DESIGNS)
    tells = data["designs"]["tells"]
    assert tells["n"] == 16 and tells["failed"] == 0 and tells["spearman"] > 0.6
    assert tells["ci"][0] <= tells["spearman"] <= tells["ci"][1]
    assert tells["calls_per_reply"] == judge.CALLS["tells"]
    assert tells["defects"]["T"]["rho"] < -0.6           # it marks the gold's defect down
    assert sum(b["n"] for b in tells["bands"].values()) == 16
    rubric = data["designs"]["rubric"]
    assert rubric["spearman"] is None and rubric["passes"] is False   # one grade for everything
    assert rubric["calls_per_reply"] == 0.5
    assert data["designs"]["features"]["calls_per_reply"] == 0
    assert data["designs"]["pairwise"]["calls_per_reply"] == judge.CALLS["pairwise"]
    asked = [c for c in llm.calls if c["format"]["title"] != "lab_rubric"]
    assert asked and all(c["think"] is False and c["options"]["temperature"] == 0.0 for c in asked)
    assert len(llm.calls) - len(asked) == 8   # the rubric: one call for two replies
    text = judge_eval.render(data)
    assert SECRET not in text and SECRET not in json.dumps(data)
    assert "rho" in text and "arm agree" in text and "tells" in text


def items_of(sizes):
    """Invented graded replies: conversation k has sizes[k] of them."""
    return [gold.Item(f"c{k}", i, "his", "word " * (3 + i % 40), (), (), 1 + (i + k) % 5,
                      frozenset({"C"} if i % 5 == 0 else ()))
            for k, n in enumerate(sizes) for i in range(n)]


def test_the_table_counts_failures_and_passes_only_on_enough_gold():
    items = items_of([13] * 12)   # 156 replies in 12 conversations: just over the minimum
    perfect = {i.key: float(i.score) for i in items}
    t = judge_eval.table(items, perfect, calls=312, seconds=78.0)
    assert t["spearman"] == 1.0 and t["calls_per_reply"] == 2.0 and t["seconds_per_reply"] == 0.5
    assert t["conversations"] == 12 and t["ci"] == [1.0, 1.0]
    assert (t["verdict"], t["passes"]) == ("pass", True)
    assert t["defects"]["C"]["n"] == 36 and t["defects"]["C"]["rho"] < 0
    missing = dict(perfect)
    for i in items[:10]:
        missing[i.key] = None
    dropped = judge_eval.table(items, missing)
    assert dropped["failed"] == 10 and dropped["verdict"] == "not enough gold"   # 146 scored
    flat = judge_eval.table(items, {i.key: 3.0 for i in items})
    assert flat["spearman"] is None and flat["ci"] is None and flat["verdict"] == "fail"
    # train and dev chose the designs and fit the weights: only the test part can pass
    assert judge_eval.table(items, perfect, part="dev")["verdict"] == "not the test part"
    assert judge_eval.table(items, perfect, part="all")["passes"] is False
    # today's test part (82 replies in 6 conversations) is too small for any verdict
    small = items_of([14, 14, 14, 14, 13, 13])
    t = judge_eval.table(small, {i.key: float(i.score) for i in small})
    assert t["spearman"] == 1.0 and (t["verdict"], t["passes"]) == ("not enough gold", False)
    assert judge_eval.table(items_of([13] * 11), perfect)["verdict"] == "not enough gold"


def test_arm_agreement_is_reported_and_never_decides(monkeypatch):
    items = items_of([13] * 12)
    rng = random.Random(3)
    judges = {"perfect": {i.key: float(i.score) for i in items},
              "weak": {i.key: rng.random() for i in items}}
    seen = {}
    for agree in (0.0, 1.0):
        monkeypatch.setattr(gold, "arm_agreement",
                            lambda *a, agree=agree, **k: {"agree": agree, "pairs": 99, "draws": 1})
        for name, scores in judges.items():
            t = judge_eval.table(items, scores)
            assert t["arm"]["agree"] == agree   # reported as measured
            seen.setdefault(name, set()).add(t["verdict"])
    assert seen == {"perfect": {"pass"}, "weak": {"fail"}}


def synthetic(rng, convs, r, shared):
    """Gold 1-5 around 3.2 with a conversation effect (SD 0.45) and a judge with its own
    conversation effect (`shared` of it the gold's) and a reply-level correlation `r` with the
    gold (review of #369's simulation, synthetic numbers only)."""
    names = sorted(set(convs))
    gm = {c: rng.gauss(0, 0.45) for c in names}
    jm = {c: shared * gm[c] + (1 - shared ** 2) ** 0.5 * rng.gauss(0, 0.45) for c in names}
    g, j = [], []
    for c in convs:
        z, e = rng.gauss(0, 1), rng.gauss(0, 1)
        g.append(float(min(5, max(1, round(3.2 + gm[c] + z)))))
        j.append(3.0 + jm[c] + 0.4 * (r * z + (1 - r * r) ** 0.5 * e))
    return g, j


def pass_rate(r, shared, *, trials, sizes=(13,) * 12, seed=11):
    convs = [f"c{k}" for k, n in enumerate(sizes) for _ in range(n)]
    rng = random.Random(seed)
    passed = 0
    for k in range(trials):
        g, j = synthetic(rng, convs, r, shared)
        passed += judge_eval.verdict(convs, j, g, seed=k, draws=200)["passes"]
    return passed / trials


@pytest.mark.parametrize("r,shared,most", [(0.0, 0.0, 0.02), (0.25, 0.25, 0.05)])
def test_the_bar_is_rarely_met_by_chance(r, shared, most):
    """At the smallest gold the bar reads (12 conversations, 156 replies): a judge with no
    agreement passes under 2% of the time, one whose true agreement is 0.25 under 5%."""
    assert pass_rate(r, shared, trials=100) < most


def test_a_judge_that_agrees_well_does_pass():
    assert pass_rate(0.8, 0.8, trials=30) >= 0.9


def test_refit_makes_numbers_only_weights_from_train_and_dev(tmp_path):
    folder = make_gold(tmp_path)
    r, _ = reasoner()
    data = asyncio.run(judge_eval.refit(r, folder))
    assert data["version"] == judge.weights()["version"] + 1 and data["fit_on"] == 64
    judge.check_weights(data)
    assert set(data["modes"]) == set(judge.MODES)
    for mode in judge.MODES:
        assert set(data["modes"][mode]["weights"]) == set(judge.inputs_of(mode))
    assert data["modes"]["tells"]["weights"]["grade"] > 0
    assert data["modes"]["tells"]["weights"]["tell_context"] < 0
    assert SECRET not in json.dumps(data)


def test_the_command_prints_numbers_only_and_refuses_what_it_cannot_run(tmp_path, capsys):
    folder = make_gold(tmp_path)
    data_dir = tmp_path / "data"
    data_dir.mkdir()   # no mind.db and no mind.toml: the defaults
    made = []

    def make_llm(cfg):
        made.append(FakeLLM(script, models={cfg.models.live}))
        return made[-1]
    base = ["--data-dir", str(data_dir), "judge-eval", "--gold", str(folder)]
    assert lab_main(base + ["--judges", "tells,rubric", "--part", "dev"], make_llm=make_llm) == 0
    out = capsys.readouterr().out
    assert SECRET not in out and "part dev" in out and "tells" in out and "rubric" in out
    assert "features" not in out.split("\n")[2]
    # the live model at the live num_ctx, never the reflection preset's
    assert made and all(c["options"]["num_ctx"] == 4096 for c in made[0].calls)
    assert lab_main(base + ["--judges", "features", "--json"], make_llm=make_llm) == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["designs"]["features"]["n"] == 16 and SECRET not in json.dumps(parsed)
    assert lab_main(base + ["--fit"], make_llm=make_llm) == 0
    judge.check_weights(json.loads(capsys.readouterr().out))
    assert lab_main(base + ["--judges", "vibes"], make_llm=make_llm) == 2
    assert lab_main(["--data-dir", str(data_dir), "judge-eval", "--gold", str(tmp_path / "none")],
                    make_llm=make_llm) == 2
    empty = tmp_path / "empty"
    empty.mkdir()
    assert lab_main(["--data-dir", str(data_dir), "judge-eval", "--gold", str(empty)],
                    make_llm=make_llm) == 2
    err = capsys.readouterr().err
    assert SECRET not in err


def test_the_command_waits_for_a_paused_gpu(tmp_path, capsys):
    import sqlite3
    from leeroy_mind.gpucontrol import KEY
    folder = make_gold(tmp_path, convs=5, turns=2)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    conn = sqlite3.connect(data_dir / "mind.db")
    conn.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("INSERT INTO meta VALUES (?, '1')", (KEY,))
    conn.commit()
    conn.close()
    asked = []
    assert lab_main(["--data-dir", str(data_dir), "judge-eval", "--gold", str(folder)],
                    make_llm=lambda cfg: asked.append(cfg)) == 2
    assert not asked and "paused" in capsys.readouterr().err


def test_a_pause_set_while_it_runs_stops_it(tmp_path, capsys):
    """judge-eval is its own process, outside the mind's GPU gate: it reads Free GPU's pause
    again before every request and stops at it (review of #369)."""
    import sqlite3
    from leeroy_mind.gpucontrol import KEY
    folder = make_gold(tmp_path, convs=5, turns=2)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    conn = sqlite3.connect(data_dir / "mind.db")
    conn.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)")
    conn.commit()
    made = []

    def pausing(model, messages):
        if len(made[0].calls) == 3:   # the console pauses the GPU after the third request
            conn.execute("INSERT INTO meta VALUES (?, '1')", (KEY,))
            conn.commit()
        return script(model, messages)

    def make_llm(cfg):
        made.append(FakeLLM(pausing, models={cfg.models.live}))
        return made[-1]
    try:
        assert lab_main(["--data-dir", str(data_dir), "judge-eval", "--gold", str(folder),
                         "--judges", "tells", "--parallel", "1"], make_llm=make_llm) == 2
    finally:
        conn.close()
    assert "paused" in capsys.readouterr().err
    assert len(made[0].calls) == 3   # nothing was asked after the pause


def night_db(data_dir, status):
    """A mind.db whose night_run has one row in `status` (the columns the check reads)."""
    import sqlite3
    conn = sqlite3.connect(data_dir / "mind.db")
    conn.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("CREATE TABLE night_run(night TEXT PRIMARY KEY, status TEXT)")
    if status is not None:
        conn.execute("INSERT INTO night_run VALUES ('2026-11-18', ?)", (status,))
    conn.commit()
    return conn


@pytest.mark.parametrize("status,refused", [("running", True), ("done", False),
                                            ("interrupted", False), (None, False)])
def test_the_command_refuses_while_a_night_runs(tmp_path, capsys, status, refused):
    """Review of #369: a night loads the reflection model, which cannot share the GPU with the
    live model judge-eval asks."""
    folder = make_gold(tmp_path, convs=5, turns=2)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    night_db(data_dir, status).close()
    made = []

    def make_llm(cfg):
        made.append(FakeLLM(script, models={cfg.models.live}))
        return made[-1]
    code = lab_main(["--data-dir", str(data_dir), "judge-eval", "--gold", str(folder),
                     "--judges", "features"], make_llm=make_llm)
    err = capsys.readouterr().err
    assert (code, "a night is running" in err, not made) == ((2, True, True) if refused else (0, False, False))


def test_a_night_that_begins_while_it_runs_stops_it(tmp_path, capsys):
    folder = make_gold(tmp_path, convs=5, turns=2)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    conn = night_db(data_dir, None)
    made = []

    def starting(model, messages):
        if len(made[0].calls) == 2:   # the mind's night begins after the second request
            conn.execute("INSERT INTO night_run VALUES ('2026-11-18', 'running')")
            conn.commit()
        return script(model, messages)

    def make_llm(cfg):
        made.append(FakeLLM(starting, models={cfg.models.live}))
        return made[-1]
    try:
        assert lab_main(["--data-dir", str(data_dir), "judge-eval", "--gold", str(folder),
                         "--judges", "tells", "--parallel", "1"], make_llm=make_llm) == 2
    finally:
        conn.close()
    assert "a night began" in capsys.readouterr().err
    assert len(made[0].calls) == 2


def test_an_unreadable_mind_db_refuses_it(tmp_path, capsys):
    folder = make_gold(tmp_path, convs=5, turns=2)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "mind.db").write_bytes(b"not a database, " * 64)
    asked = []
    assert lab_main(["--data-dir", str(data_dir), "judge-eval", "--gold", str(folder)],
                    make_llm=lambda cfg: asked.append(cfg)) == 2
    assert not asked and "cannot read mind.db" in capsys.readouterr().err
