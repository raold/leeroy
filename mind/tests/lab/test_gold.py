"""The gold loader, the fixed split and the agreement numbers, on invented conversations."""
import json

import pytest

from leeroy_mind.lab import gold


def conv(n, *, base=0, flags=""):
    turns = []
    for i in range(n):
        turns.append({"id": base + 2 * i, "who": "him", "text": f"line {i} from him"})
        turns.append({"id": base + 2 * i + 1, "who": "her", "answers": base + 2 * i,
                      "text": f"answer number {i}", "grade": {"score": 1 + i % 5, "flags": flags}})
    return {"guid": 1, "case": "invented", "card": "A card.", "turns": turns}


def write(tmp_path, name, doc):
    (tmp_path / name).write_bytes(json.dumps(doc).encode())


def test_a_conversation_reads_as_graded_replies_with_what_came_before():
    doc = {"turns": [
        {"id": 1, "who": "him", "text": "you there"},
        {"id": 2, "who": "her", "answers": 1, "text": "yeah", "grade": {"score": 5, "flags": ""}},
        {"id": 3, "who": "him", "text": "raid tonight?"},
        {"id": 4, "who": "him", "text": "or tomorrow"},
        {"id": 5, "who": "her", "answers": 3, "text": "tonight", "rewritten": True,
         "grade": {"score": 2, "flags": "C, R"}},
        {"id": 6, "who": "her", "text": "ungraded line"},
        {"id": 7, "who": "her", "text": "no answers id", "grade": {"score": 3, "flags": ["O"]}}]}
    a, b, c = gold.read_conversation("x", doc)
    assert (a.his, a.reply, a.score, a.earlier, a.context) == ("you there", "yeah", 5, (), ())
    assert b.his == "raid tonight?" and b.flags == {"C", "R"} and b.rewritten
    assert b.earlier == ("yeah",) and b.context == (("him", "you there"), ("her", "yeah"))
    # without an id to answer, the reply answers his last line
    assert c.his == "or tomorrow" and c.flags == {"O"} and c.earlier == ("yeah", "tonight", "ungraded line")
    assert a.key != b.key


def test_a_grade_outside_1_to_5_is_context_and_a_broken_file_is_refused(tmp_path):
    doc = conv(2)
    doc["turns"][1]["grade"]["score"] = 9
    assert [i.score for i in gold.read_conversation("x", doc)] == [2]
    with pytest.raises(gold.GoldError):
        gold.read_conversation("x", {"turns": [{"who": "them", "text": "?"}]})
    (tmp_path / "conv-bad.json").write_bytes(b"{")
    with pytest.raises(gold.GoldError):
        gold.load(tmp_path)
    with pytest.raises(gold.GoldError):
        gold.load(tmp_path / "nowhere")


def test_the_split_is_by_conversation_fixed_and_near_60_20_20(tmp_path):
    for n in range(10):
        write(tmp_path, f"s-conv-{n}.json", conv(10, base=100 * n))
    write(tmp_path, "notes.json", {"not": "a conversation"})
    write(tmp_path, "conv-empty.json", {"turns": [{"who": "him", "text": "hi"}]})
    convs = gold.load(tmp_path)
    assert len(convs) == 10
    parts = gold.split(convs)
    assert {p: len(v) for p, v in parts.items()} == {"train": 60, "dev": 20, "test": 20}
    names = {p: {i.conv for i in v} for p, v in parts.items()}
    assert not names["train"] & names["dev"] and not names["dev"] & names["test"]
    again = gold.split(gold.load(tmp_path))
    assert {p: [i.key for i in v] for p, v in again.items()} == {p: [i.key for i in v] for p, v in parts.items()}
    other = gold.split(convs, seed=1)
    assert {p: len(v) for p, v in other.items()} == {"train": 60, "dev": 20, "test": 20}


def test_spearman_handles_ties_and_no_spread():
    assert gold.spearman([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0)
    assert gold.spearman([1, 2, 3, 4], [4, 3, 2, 1]) == pytest.approx(-1.0)
    assert gold.spearman([1, 1, 2, 2], [1, 2, 1, 2]) == pytest.approx(0.0)
    assert gold.spearman([1, 2, 2, 3], [1, 2, 2, 5]) == pytest.approx(1.0)
    assert gold.spearman([1, 1, 1], [1, 2, 3]) is None
    assert gold.spearman([1, 2], [1, 2]) is None
    assert gold.ranks([5, 1, 5, 3]) == [3.5, 1.0, 3.5, 2.0]


def test_the_bootstrap_interval_holds_the_estimate_and_is_seeded():
    a = [float(i % 7) for i in range(40)]
    b = [x + (i % 3) for i, x in enumerate(a)]
    low, high = gold.bootstrap(a, b, draws=300)
    assert low <= gold.spearman(a, b) <= high and low > 0
    assert gold.bootstrap(a, b, draws=300) == (low, high)
    assert gold.bootstrap(a[:5], b[:5]) is None


def test_arm_agreement_is_high_for_the_gold_itself_and_low_for_its_opposite():
    convs = [f"c{i % 4}" for i in range(80)]
    scores = [1.0 + (i % 4) + (i % 3) * 0.3 for i in range(80)]  # the conversations differ
    same = gold.arm_agreement(convs, scores, scores, draws=300)
    assert same["agree"] == 1.0 and same["pairs"] > 50
    flipped = gold.arm_agreement(convs, scores, [-s for s in scores], draws=300)
    assert flipped["agree"] == 0.0
    flat = gold.arm_agreement(convs, scores, [3.0] * 80, draws=300)
    assert flat["agree"] == 0.0  # a judge that cannot tell the arms apart does not agree
    assert gold.arm_agreement(["only"] * 10, scores[:10], scores[:10])["agree"] is None


def test_bands_cover_every_length():
    assert [gold.band(n) for n in (0, 10, 11, 25, 26, 50, 51, 400)] == [
        "<=10", "<=10", "11-25", "11-25", "26-50", "26-50", ">50", ">50"]
