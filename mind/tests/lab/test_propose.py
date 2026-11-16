"""The proposer's round-robin, prompt, screen and diff (no model)."""
import re

import pytest

from leeroy_mind.lab import propose
from leeroy_mind.nightly.llm import check_schema
from leeroy_mind.talk import pieces

ORIGINAL = pieces.builtin("reply_human")


def variant(text=ORIGINAL, old="Respond to what", new="Plainly respond to what"):
    return text.replace(old, new, 1)


def test_the_round_robin_goes_through_every_piece_and_passes_over_skipped_ones():
    order = list(pieces.NAMES)
    assert propose.pick_piece([]) == order[0]
    assert propose.pick_piece([order[0]]) == order[1]
    assert propose.pick_piece([order[-1]]) == order[0]
    assert propose.pick_piece([order[0]], skip={order[1]}) == order[2]
    assert propose.pick_piece(["gone"]) == order[0]
    assert propose.pick_piece([], skip=set(order)) is None


@pytest.mark.parametrize("text, code", [
    (variant(), None),
    (ORIGINAL, "same"),
    ("  " + ORIGINAL.upper() + " ", "shape"),  # upper-cased placeholders are other fields
    (ORIGINAL.replace("{they}", "they"), "shape"),                      # a placeholder dropped
    (ORIGINAL.replace("{extra}", "{extra} {mood}"), "shape"),           # one added
    (ORIGINAL.replace("in one short line", "briefly"), "shape"),        # a kept phrase lost
    (ORIGINAL.replace(". ", ".\n", 1), "same"),      # whitespace is folded into one paragraph
    (variant(new="Respond | to what"), "shape"),
    (ORIGINAL + " Keep it light, warm and specific to the moment you are in, always.", "length"),
    ("Answer {addressee}'s last line, as {they} said it, in one short line. {extra}", "length"),
    (variant(new="Respond like Arkon to what"), "names"),
    (variant(new="Respond @here to what"), "names"),
    (variant(new="Respond sexy to what"), "explicit"),
    (variant(new="Make him feel guilty and respond to what"), "explicit"),
    (variant(new="As an AI respond to what"), "meta"),
    (variant(new="Respond (not a bot) to what"), "meta"),
    (None, "shape"),
])
def test_the_screen(text, code):
    names = re.compile(r"(?<!\w)(arkon|belra|testmage)(?!\w)", re.I)
    assert propose.screen("reply_human", text, names_rx=names) == code


def test_accept_keeps_k_deduplicates_and_names_refusals():
    reply = {"variants": [{"text": variant(), "idea": "a"},
                          {"text": variant(), "idea": "same again"},
                          {"text": "no", "idea": "short"},
                          {"text": variant(new="Simply respond to what"), "idea": "b"},
                          {"text": variant(new="Honestly respond to what"), "idea": "c"}]}
    kept, refused = propose.accept("reply_human", reply, k=2,
                                   earlier=[variant(new="Simply respond to what")])
    assert [k["text"] for k in kept] == [variant(), variant(new="Honestly respond to what")]
    assert refused == ["same", "shape", "same"]
    assert kept[0]["diff"].count("{+Plainly respond+}") == 1 or "{+" in kept[0]["diff"]
    assert propose.accept("reply_human", {"oops": 1}, k=3) == ([], [])


def test_the_diff_marks_removals_and_additions():
    assert propose.diff("a b c d", "a x c d e") == "a [-b-] {+x+} c d {+e+}"
    assert propose.diff("same words", "same words") == "same words"


def test_the_prompt_and_schema_carry_numbers_and_the_repo_wording_only():
    feedback = {"control": {"rubric": 1.42, "n": 24, "codes": [("generic", 9), ("in_voice", 6)],
                            "flags": {"wordy": 0.25, "echo": 0.0}},
                "tried": [{"text": variant(), "outcome": "lost", "delta": -0.1}]}
    text = propose.prompt("reply_human", k=3, feedback=feedback)
    assert ORIGINAL in text and "{addressee}" in text and '"in one short line"' in text
    assert "mean 1.42 over 24" in text and "wordy 25%" in text and "echo" not in text
    assert "-0.10 (lost)" in text and "Write 6 different variants." in text
    schema = propose.response_schema(3)
    check_schema(schema, {"variants": [{"text": "x", "idea": "y"}]})
    with pytest.raises(Exception):
        check_schema(schema, {"variants": [{"text": "x"}]})


@pytest.mark.parametrize("name", pieces.NAMES)
def test_every_piece_has_a_screenable_wording(name):
    original = pieces.builtin(name)
    assert pieces.fits(name, original)
    assert propose.screen(name, original) == "same"
    assert len(pieces.piece(name).what) > 10
