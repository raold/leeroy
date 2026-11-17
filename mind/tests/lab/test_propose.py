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
    ("  " + ORIGINAL.upper() + " ", "placeholders"),  # upper-cased ones are other fields
    (ORIGINAL.replace("{they}", "they"), "placeholders"),               # a placeholder dropped
    (ORIGINAL.replace("{extra}", "{extra} {mood}"), "placeholders"),    # one added
    (ORIGINAL.replace("{they}", "{they!r}"), "placeholders"),           # not a plain name
    (ORIGINAL.replace("in one short line", "briefly"), "kept"),         # a kept phrase lost
    (ORIGINAL.replace("in one short line", "In one short line"), "kept"),
    ("   ", "shape"),
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
    assert refused == ["same", "placeholders", "same"]
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
    # the locked phrase is explained, a valid variant is shown, and the checklist comes last
    assert 'Locked phrases: "in one short line". Copy each into every variant letter' in text
    assert propose.EXAMPLES["reply_human"] in text
    assert text.endswith('check each one: it has {addressee}, {extra}, {they} and no other '
                         '{placeholder}; it has "in one short line" exactly; it is one paragraph.')
    assert "keep every part of it, reworded" not in text
    assert "Locked phrases" not in propose.prompt("beat_ask", k=1, feedback={})
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


@pytest.mark.parametrize("name", pieces.NAMES)
def test_every_piece_has_an_example_the_screen_accepts_and_never_tries(name):
    example = propose.EXAMPLES[name]
    assert propose.screen(name, example) is None
    kept, codes = propose.judge(name, {"variants": [{"text": example, "idea": "the example"}]}, k=3)
    assert kept == [] and codes == ["same"]
    assert example in propose.prompt(name, k=2, feedback={})


def test_the_first_live_night_s_proposals_are_refused_for_the_lost_phrase():
    """2026-11-17: all six candidates turned "in one short line" into "Keep it brief."."""
    lost = ("Answer {addressee}'s last line. React to what {they} actually said, staying true to "
            "yourself. Keep it brief. Use a memory or joke only if it fits. {extra}")
    reply = {"variants": [{"text": lost, "idea": "react"}, {"text": variant(), "idea": "plainer"},
                          {"text": lost.replace("{they}", "they"), "idea": "no pronoun"}]}
    kept, codes = propose.judge("reply_human", reply, k=3)
    assert [k["text"] for k in kept] == [variant()] and codes == ["kept", None, "placeholders"]
    assert propose.problem("reply_human", lost, "kept") == (
        'it lost the locked phrase "in one short line": copy it letter for letter')
    assert propose.problem("reply_human", lost.replace("{they}", "{you}"), "placeholders") == (
        "it lost {they} and it adds {you}")
    assert "braces do not parse" in propose.problem("reply_human", "a } b", "placeholders")
    assert "keep it within 102-202" in propose.problem("reply_human", "x" * 40, "length")
    assert propose.problem("reply_human", lost, "extra") is None
    first = propose.prompt("reply_human", k=2, feedback={})
    again = propose.retry_prompt("reply_human", first, reply, codes, k=2)
    assert again.startswith(first) and "Your earlier variants were refused by the code:" in again
    assert again.count("it lost the locked phrase") == 1 and "it lost {they}" in again
    assert "Write 4 new variants without those faults." in again
    assert again.endswith("it is one paragraph.")


def test_a_short_piece_has_room_and_a_hand_written_variant_its_own_band():
    assert propose.length_band("reply_human") == (102, 202)           # 169: -40%, +20%
    assert propose.length_band("situation_reply_human") == (24, 84)   # 54 characters, +-30
    low, high = propose.length_band("situation_reply_human", authored=True)
    assert (low, high) == (1, 174)
    short = "{addressee}: {extra}"
    assert propose.screen("situation_reply_human", short) == "length"
    assert propose.screen("situation_reply_human", short, authored=True) is None
    long = "Answer {addressee}, " + "and say it the way you would say it, " * 3 + "{extra}"
    assert propose.screen("situation_reply_human", long) == "length"
    assert propose.screen("situation_reply_human", long, authored=True) is None
    assert propose.screen("situation_reply_human", long * 2, authored=True) == "length"
    # everything else is as strict for a hand-written one
    assert propose.screen("reply_human", ORIGINAL.replace("in one short line", "briefly"),
                          authored=True) == "kept"
    assert propose.screen("reply_human", variant(new="As an AI respond to what"), authored=True) == "meta"


def test_the_round_robin_prefers_waiting_hand_written_pieces_and_skips_unrendered_ones():
    order = list(pieces.NAMES)
    assert propose.pick_piece([], prefer={order[3]}) == order[3]
    assert propose.pick_piece([order[3]], prefer={order[3], order[5]}) == order[5]
    assert propose.pick_piece([order[5]], prefer={order[3], order[5]}) == order[3]
    assert propose.pick_piece([], prefer={order[3]}, skip={order[3]}) == order[0]
    live = pieces.unrendered(situation=True, realism=True)
    assert live == {"reply_human", "continue", "romance_hook"}
    assert propose.pick_piece([], skip=live) == "goodnight"
    assert pieces.unrendered(situation=False, realism=False) == {"situation_reply_human",
                                                                 "romance_lead"}
