"""The rubric's prompt, schema and parse, and the blind judge's window (invented lines)."""
import pytest

from leeroy_mind.lab import grade
from leeroy_mind.nightly.llm import check_schema
from leeroy_mind.turing import judge

TURNS = [{"his": "you around tonight?", "reply": "yeah after 9", "lines": ["yeah", "after 9"]},
         {"his": "cool", "reply": "", "lines": []}]


def test_the_rubric_prompt_shows_her_card_and_numbers_her_replies():
    text = grade.rubric_prompt("Card: a nurse from Ohio.", TURNS)
    assert text.startswith("Her card:\nCard: a nurse from Ohio.")
    assert "him: you around tonight?\nher (reply 1): yeah after 9" in text
    assert "her (reply 2): (nothing)" in text and text.endswith("Grade replies 1 to 2.")
    long = grade.rubric_prompt("", [{"his": "x", "reply": "word " * 400}])
    assert "…" in long


def test_the_rubric_schema_and_parse():
    schema = grade.rubric_schema(2)
    good = {"grades": [{"reply": 2, "score": 0, "code": "empty"},
                       {"reply": 1, "score": 3, "code": "passes"}]}
    check_schema(schema, good)
    assert grade.rubric_parse(good, 2) == [(3, "passes"), (0, "empty")]
    for bad in ({"grades": [{"reply": 1, "score": 3, "code": "passes"}]},
                {"grades": [{"reply": 1, "score": 3, "code": "passes"},
                            {"reply": 1, "score": 2, "code": "in_voice"}]},
                {"grades": [{"reply": 1, "score": 4, "code": "passes"},
                            {"reply": 2, "score": 2, "code": "in_voice"}]},
                {"grades": [{"reply": 1, "score": 2, "code": "great"},
                            {"reply": 2, "score": 2, "code": "in_voice"}]},
                None, {"grades": "x"}):
        assert grade.rubric_parse(bad, 2) is None


def test_the_window_has_synthetic_times_and_his_lines_first():
    lines = grade.window(TURNS, human=1, guid=102, channel="dm")
    assert [line.speaker for line in lines] == [1, 102, 102, 1]
    assert [line.human for line in lines] == [True, False, False, True]
    assert lines[0].ts == 0 and lines[1].ts == pytest.approx(grade.READ_S + 4 / grade.TYPING_CPS)
    assert all(b.ts > a.ts for a, b in zip(lines, lines[1:]))
    assert lines[0].channel == "dm:102" and lines[0].family == "private"
    assert grade.window(TURNS, human=1, guid=102, channel="guild")[0].family == "game"


def test_judged_reads_her_verdict_and_his():
    lines = grade.window(TURNS, human=1, guid=102, channel="whisper")
    shown = judge.render(lines, by_guid={1: "Testmage", 102: "Belra"}, human=1)
    her = next(k for k, g in shown.letters.items() if g == 102)
    him = next(k for k, g in shown.letters.items() if g == 1)
    reply = {"speakers": [{"speaker": her, "human": True, "tells": []},
                          {"speaker": him, "human": False, "tells": []}]}
    assert grade.judged(reply, shown.letters, human=1) == {"her": 1, "him": 0}
    assert grade.judged({"speakers": []}, shown.letters, human=1) is None
