"""The lab judge: prompts, pairwise aggregation, the linear model and the seeded, bounded calls
(a FakeLLM; invented lines only)."""
import asyncio
import json
import re

import pytest

from leeroy_mind.lab import features, judge
from leeroy_mind.nightly.llm import Reasoner, check_schema
from leeroy_mind.testing.fake_llm import FakeLLM

HIS, REPLY = "did you get the flasks for tonight", "got 4, need like 6 more. you have herbs?"


def reasoner(script):
    llm = FakeLLM(script, models={"m"})
    return Reasoner(llm, "m"), llm


def run(coro):
    return asyncio.run(coro)


# -- prompts and schemas ---------------------------------------------------------------------------

def test_an_exchange_shows_the_word_counts_and_clips_the_context():
    text = judge.exchange("lol", "ha yes", context=[("him", "x " * 400), ("her", "ok")])
    assert "His message (1 word): lol" in text and "Her reply (2 words): ha yes" in text
    assert text.startswith("Before:\nhim: x x") and "…" in text and "her: ok" in text
    assert "Her earlier messages" not in text
    assert judge.exchange("", "hi").startswith("His message (0 words): (nothing)")


def test_a_tell_prompt_asks_one_question_and_only_repeats_reads_her_earlier_lines():
    for ident, question, bad, view in judge.TELLS:
        text = judge.tell_prompt(ident, HIS, REPLY, context=(("him", "an old turn"),),
                                 earlier=("an earlier line of hers",), card="Card: a nurse.")
        assert text.endswith("Question: " + question) and bad in ("yes", "no")
        assert ("an earlier line of hers" in text) == (ident in judge.READS_EARLIER)
        assert ("Card: a nurse." in text) == (view == "card") and "an old turn" in text
        assert (judge.tell_system(ident) == judge.WIDE_SYSTEM) == (view in ("wide", "card"))
    assert judge.READS_EARLIER == {"repeats"}
    assert set(judge.CHECKLIST) <= set(judge.TELL_IDS)
    check_schema(judge.tell_schema(), {"evidence": "got 4", "answer": "yes"})
    with pytest.raises(ValueError):
        check_schema(judge.tell_schema(), {"evidence": "", "answer": "maybe"})


def test_a_tell_is_one_when_the_answer_is_the_defect():
    ident, _, bad, _ = judge.TELLS[0]
    good = "no" if bad == "yes" else "yes"
    assert judge.tell_value(ident, {"answer": bad}) == 1.0
    assert judge.tell_value(ident, {"answer": good}) == 0.0


def test_the_pair_prompt_names_both_exchanges_and_the_fewshot_prompt_the_lengths():
    text = judge.pair_prompt((HIS, REPLY), ("hey", "hey you", (("him", "earlier"),)))
    assert text.index("Exchange A") < text.index(REPLY) < text.index("Exchange B") < text.index("hey you")
    assert text.endswith("A, B or same?")
    assert set(judge.pair_schema()["properties"]["better"]["enum"]) == {"A", "B", "same"}
    shot = judge.fewshot_prompt("lol", "ha " * 30)
    assert "his line (1 word): lol" in shot and "her reply (30 words):" in shot
    assert "(2 words)" in judge.FEWSHOT_SYSTEM and "grade: 5" in judge.FEWSHOT_SYSTEM
    asked = judge.grade_prompt("lol", "ha " * 30, context=(("him", "before"),), earlier=("old line",))
    assert asked.startswith("Before:\nhim: before\n\nHer earlier messages:\n- old line\n\n"
                            "his line (1 word): lol")
    assert asked.endswith("her reply (30 words): " + ("ha " * 30).strip())
    check_schema(judge.grade_schema(), {"problem": "none", "grade": 5})
    with pytest.raises(ValueError):
        check_schema(judge.grade_schema(), {"problem": "vibes", "grade": 5})


# -- pairwise aggregation --------------------------------------------------------------------------

@pytest.mark.parametrize("first, second, share", [
    ("A", "B", 1.0),      # it won shown first and shown second
    ("B", "A", 0.0),      # it lost both ways
    ("A", "A", 0.5),      # the judge followed the position: a split
    ("B", "B", 0.5),
    ("same", "same", 0.5),
    ("A", "same", 0.75), ("same", "A", 0.25),
    ("A", None, 1.0), (None, "B", 1.0), (None, "same", 0.5),
    (None, None, None), ("C", "?", None)])
def test_both_ways_votes(first, second, share):
    assert judge.pair_votes(first, second) == share


def test_a_score_against_anchors_moves_each_anchors_grade_by_the_comparison():
    n = len(judge.ANCHORS)
    grades = [g for _, _, g in judge.ANCHORS]
    assert judge.anchor_score([0.5] * n) == pytest.approx(sum(grades) / n)
    assert judge.anchor_score([1.0] * n) > judge.anchor_score([0.5] * n) > judge.anchor_score([0.0] * n)
    assert judge.anchor_score([None] * n) is None
    assert 1.0 <= judge.anchor_score([0.0] * n) and judge.anchor_score([1.0] * n) <= 5.0
    # an unanswered comparison is left out, not counted as a tie
    assert judge.anchor_score([1.0] + [None] * (n - 1)) == pytest.approx(min(5.0, grades[0] + 1.0))


def test_prefer_asks_both_ways_round_with_different_seeds():
    seen = []

    def script(model, messages):
        seen.append(messages[-1]["content"])
        first = messages[-1]["content"].index("good reply") < messages[-1]["content"].index("bad reply")
        return json.dumps({"better": "A" if first else "B"})
    r, llm = reasoner(script)
    share = run(judge.prefer(r, ("hi", "good reply"), ("hi", "bad reply"), seed=3))
    assert share == 1.0 and r.calls == 2
    assert seen[0].index("good reply") < seen[0].index("bad reply")
    assert seen[1].index("bad reply") < seen[1].index("good reply")
    a, b = (c["options"] for c in llm.calls)
    assert a["seed"] != b["seed"] and a["temperature"] == 0.0 == b["temperature"]
    assert all(c["think"] is False and c["format"]["title"] == "lab_pair" for c in llm.calls)
    # a judge that always says A is a split, whatever the replies
    r, _ = reasoner(lambda m, msgs: json.dumps({"better": "A"}))
    assert run(judge.prefer(r, ("hi", "x"), ("hi", "y"))) == 0.5


def test_an_invalid_pair_answer_is_asked_once_more_then_left_out():
    replies = iter(["not json", json.dumps({"better": "B"}), "{}", "[]"])
    r, llm = reasoner(lambda m, msgs: next(replies))
    assert run(judge.prefer(r, ("hi", "x"), ("hi", "y"))) == 0.0   # first way: B wins; second: none
    assert r.calls == 4
    assert llm.calls[0]["options"]["seed"] + 1 == llm.calls[1]["options"]["seed"]


# -- the linear model ------------------------------------------------------------------------------

def test_combine_is_bias_plus_weights_clipped_to_the_scale():
    model = {"bias": 4.0, "weights": {"over": -0.5, "tell_x": -1.0}}
    assert judge.combine({"over": 1.0, "tell_x": 1.0, "unused": 9.0}, model) == pytest.approx(2.5)
    assert judge.combine({}, model) == 4.0                       # a missing input counts 0
    assert judge.combine({"over": 40.0}, model) == 1.0
    assert judge.combine({"over": -40.0}, model) == 5.0


def test_fit_recovers_a_linear_rule_and_ridge_shrinks_it():
    rows = [{"a": float(i % 2), "b": float(i % 3) / 2, "noise": float((i * 7) % 5)} for i in range(60)]
    targets = [4.0 - 1.5 * r["a"] - 0.8 * r["b"] for r in rows]
    model = judge.fit(rows, targets, ["a", "b", "noise"], ridge=1e-6)
    assert model["bias"] == pytest.approx(4.0, abs=0.01)
    assert model["weights"]["a"] == pytest.approx(-1.5, abs=0.01)
    assert model["weights"]["b"] == pytest.approx(-0.8, abs=0.01)
    assert model["weights"]["noise"] == pytest.approx(0.0, abs=0.01)
    shrunk = judge.fit(rows, targets, ["a", "b", "noise"], ridge=200.0)
    assert abs(shrunk["weights"]["a"]) < 0.5
    assert judge.fit(rows, targets, ["a", "constant"], ridge=1.0)["weights"]["constant"] == 0.0
    with pytest.raises(ValueError):
        judge.fit(rows[:1], targets[:1], ["a"])


def test_the_shipped_weights_are_numbers_for_every_mode():
    data = judge.weights()
    assert isinstance(data["version"], int) and set(data["modes"]) == set(judge.MODES)
    known = set(features.NAMES) | {f"tell_{t}" for t in judge.TELL_IDS} | {"fewshot", "anchors", "grade"}
    for mode, model in data["modes"].items():
        assert set(model["weights"]) <= known, mode
        assert set(model["weights"]) == set(judge.inputs_of(mode)), mode
    def walk(node):   # keys and numbers: no sentence in it
        for key, value in node.items():
            assert re.fullmatch(r"[a-z_0-9]+", key), key
            if isinstance(value, dict):
                walk(value)
            else:
                assert type(value) in (int, float), key
    walk(json.loads(judge.WEIGHTS_FILE.read_text(encoding="utf-8")))
    for bad in ({}, {"version": 1, "modes": {"x": {"bias": "1", "weights": {}}}},
                {"version": 1, "modes": {"x": {"bias": 1.0, "weights": {"a": float("nan")}}}}):
        with pytest.raises(ValueError):
            judge.check_weights(bad)


# -- scoring ---------------------------------------------------------------------------------------

def tell_script(defects=(), grade=5):
    def script(model, messages):
        if "Question: " not in messages[-1]["content"]:
            return json.dumps({"problem": "none", "grade": grade})
        question = messages[-1]["content"].rsplit("Question: ", 1)[1]
        ident, _, bad, _ = next(t for t in judge.TELLS if t[1] == question)
        good = "no" if bad == "yes" else "yes"
        return json.dumps({"evidence": "", "answer": bad if ident in defects else good})
    return script


def test_a_tells_score_is_bounded_seeded_and_lower_with_defects():
    r, llm = reasoner(tell_script())
    clean = run(judge.score(r, HIS, REPLY, mode="tells", seed=5))
    assert clean["calls"] == 1 + len(judge.tells_of("tells")) == judge.CALLS["tells"]
    assert 1.0 <= clean["score"] <= 5.0
    assert all(c["think"] is False and c["options"]["temperature"] == 0.0 for c in llm.calls)
    assert all(c["options"]["num_predict"] == judge.PREDICT for c in llm.calls)
    assert {c["format"]["title"] for c in llm.calls} == {"lab_tell", "lab_grade"}
    seeds = [c["options"]["seed"] for c in llm.calls]
    assert len(set(seeds)) == len(seeds)
    r2, llm2 = reasoner(tell_script())
    again = run(judge.score(r2, HIS, REPLY, mode="tells", seed=5))
    assert again == clean and [c["options"]["seed"] for c in llm2.calls] == seeds
    r3, llm3 = reasoner(tell_script())
    run(judge.score(r3, HIS, REPLY, mode="tells", seed=6))
    assert [c["options"]["seed"] for c in llm3.calls] != seeds
    for tell in judge.tells_of("tells"):   # every tell the mode asks marks the reply down
        r4, _ = reasoner(tell_script(defects=(tell,)))
        flawed = run(judge.score(r4, HIS, REPLY, mode="tells", seed=5))
        assert flawed["score"] < clean["score"] and flawed["inputs"][f"tell_{tell}"] == 1.0
    r5, _ = reasoner(tell_script(grade=2))
    assert run(judge.score(r5, HIS, REPLY, mode="tells", seed=5))["score"] < clean["score"]


def test_the_checklist_asks_one_tell_at_a_time_and_skips_what_it_cannot_read():
    r, llm = reasoner(tell_script())
    got = run(judge.score(r, HIS, REPLY, mode="checklist"))
    assert got["calls"] == len(judge.CHECKLIST) - 1     # no earlier line: `repeats` not asked
    assert got["inputs"]["tell_repeats"] == 0.0
    r, llm = reasoner(tell_script())
    got = run(judge.score(r, HIS, REPLY, mode="checklist", earlier=("an old line",)))
    assert got["calls"] == len(judge.CHECKLIST) == judge.CALLS["checklist"]
    r, llm = reasoner(tell_script())
    assert run(judge.tells(r, HIS, REPLY, only=("persona",))) == {"persona": 0.0} and not llm.calls
    assert run(judge.tells(r, HIS, REPLY, only=("persona",), card="A card.")) == {"persona": 0.0}
    assert len(llm.calls) == 1 and "A card." in llm.calls[0]["messages"][-1]["content"]


def test_an_empty_reply_scores_one_without_a_call_and_features_ask_nothing():
    r, llm = reasoner(lambda m, msgs: pytest.fail("no call expected"))
    assert run(judge.score(r, HIS, "  ", mode="tells")) == {"score": 1.0, "inputs": {}, "calls": 0}
    got = run(judge.score(r, HIS, REPLY, mode="features"))
    assert got["calls"] == 0 and set(got["inputs"]) == set(features.NAMES) and not llm.calls
    with pytest.raises(ValueError):
        run(judge.score(r, HIS, REPLY, mode="vibes"))


def test_fewshot_and_pairwise_take_their_bounded_calls():
    r, llm = reasoner(lambda m, msgs: json.dumps({"grade": 4}))
    got = run(judge.score(r, HIS, REPLY, mode="fewshot"))
    assert got["calls"] == 1 == judge.CALLS["fewshot"] and got["inputs"]["fewshot"] == 4.0
    assert llm.calls[0]["messages"][0]["content"] == judge.FEWSHOT_SYSTEM
    r, llm = reasoner(lambda m, msgs: json.dumps({"better": "same"}))
    got = run(judge.score(r, HIS, REPLY, mode="pairwise"))
    assert got["calls"] == 2 * len(judge.ANCHORS) == judge.CALLS["pairwise"]
    assert got["inputs"]["anchors"] == pytest.approx(sum(g for _, _, g in judge.ANCHORS) / len(judge.ANCHORS))


def test_a_tell_that_fails_twice_is_a_judge_error_and_nothing_private_is_in_it():
    r, _ = reasoner(lambda m, msgs: "no json here")
    with pytest.raises(judge.JudgeError) as err:
        run(judge.score(r, HIS, REPLY, mode="tells"))
    assert HIS not in str(err.value) and REPLY not in str(err.value)


def test_a_seed_is_stable_and_depends_on_its_parts():
    assert judge.seed_of(1, "tell", "echo") == judge.seed_of(1, "tell", "echo")
    assert judge.seed_of(1, "tell", "echo") != judge.seed_of(1, "tell", "adds")
    assert judge.seed_of(1, 2, 3) != judge.seed_of(2, 2, 3)
    assert 0 <= judge.seed_of(9, "x") < 2 ** 31


def test_a_window_of_lines_is_scored_reply_by_reply_for_turing_and_the_bakeoff():
    from leeroy_mind.turing.corpus import Line

    def line(n, speaker, human, text):
        return Line(f"c:{n}", float(n), "private", "w", "whisper", speaker, human, text)
    window = [line(1, 1, True, "you up?"), line(2, 7, False, "yeah"), line(3, 7, False, "whats up"),
              line(4, 8, False, "an aside from someone else"),
              line(5, 1, True, "raid tonight?"), line(6, 7, False, "sure, 8?")]
    seen = []

    def script(model, messages):
        seen.append(messages[-1]["content"])
        return tell_script()(model, messages)
    r, _ = reasoner(script)
    got = run(judge.score_lines(r, window, seed=2))
    # two replies to him (her two lines are one reply); the aside answers nobody of his
    assert got["replies"] == 2 and got["calls"] == 2 * judge.CALLS["tells"]
    assert got["mean"] == pytest.approx(sum(got["scores"]) / 2) and 1.0 <= got["mean"] <= 5.0
    grades = [s for s in seen if "Question: " not in s]
    assert "her reply (3 words): yeah whats up" in grades[0] and "an aside" not in grades[0]
    assert "Her earlier messages:\n- yeah whats up" in grades[1]
    assert "her: an aside from someone else" in grades[1]
    assert run(judge.score_lines(r, [line(1, 7, False, "nobody asked")])) == {
        "mean": None, "replies": 0, "calls": 0, "scores": []}
