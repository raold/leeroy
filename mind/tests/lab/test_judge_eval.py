"""`lab judge-eval`: the judges against invented gold conversations, numbers only (FakeLLM)."""
import asyncio
import json

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
    assert all(c["think"] is False and c["options"]["temperature"] == 0.0 for c in llm.calls)
    text = judge_eval.render(data)
    assert SECRET not in text and SECRET not in json.dumps(data)
    assert "rho" in text and "arm agree" in text and "tells" in text


def test_the_table_counts_failures_and_needs_both_bars_to_pass():
    items = [gold.Item(f"c{i % 4}", i, "his", "word " * (3 + i % 40), (), (), 1 + i % 5,
                       frozenset({"C"} if i % 5 == 0 else ())) for i in range(60)]
    perfect = {i.key: float(i.score) for i in items}
    t = judge_eval.table(items, perfect, calls=120, seconds=30.0)
    assert t["spearman"] == 1.0 and t["calls_per_reply"] == 2.0 and t["seconds_per_reply"] == 0.5
    assert t["defects"]["C"]["n"] == 12 and t["defects"]["C"]["rho"] < 0
    missing = dict(perfect)
    for i in items[:10]:
        missing[i.key] = None
    assert judge_eval.table(items, missing)["failed"] == 10
    flat = judge_eval.table(items, {i.key: 3.0 for i in items})
    assert flat["spearman"] is None and flat["passes"] is False
    weak = judge_eval.table(items, {i.key: float(i.score) if i.index % 3 else 3.0 for i in items})
    assert weak["passes"] == (weak["spearman"] >= judge_eval.PASS_RHO
                              and (weak["arm"]["agree"] or 0) >= judge_eval.PASS_ARM)


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
