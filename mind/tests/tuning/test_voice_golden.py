"""The voice golden (Persona.speech, 2026-11-17): the tuning scenario with invented voice sheets
on two of its members (scenario.voiced_cast: Belra, 102, and Coros, 103), recorded in both prompt
modes in golden_voice.json ("plain": every switch off; "core": the core prompt's switches on, as
golden_core.json's). The four goldens recorded without a sheet are untouched
(test_tuning_invariant.py, test_core_golden.py): no sheet, no change.

In it, a member with a sheet reads her voice block as the last thing before her instruction
(after "Right now:" in the situation mode), her card has no trait sentences and no attachment or
humour line, and her system message has no "How you type" samples; the other three members'
prompts are the ones the other goldens hold."""
import pytest

from leeroy_mind.personas import speech as sp
from leeroy_mind.talk.prompt import SAMPLES_HEAD
from tuning.scenario import (VOICED, engine_clock, golden, golden_core, golden_voice,
                             record_voice)

VOICES = ("Your voice: bubbly, scattered, quick to laugh;", "Your voice: plain, clipped, a bit gruff;")


@pytest.fixture
def clock():
    return engine_clock()


@pytest.mark.parametrize("mode", ["plain", "core"])
def test_voice_sheets_match_the_recorded_voice_golden(store, clock, tmp_path, mode):
    assert record_voice(store, clock, tmp_path, core=mode == "core") == golden_voice()[mode]


@pytest.mark.parametrize("mode", ["plain", "core"])
def test_what_the_voice_golden_holds(mode):
    prompts = golden_voice()[mode]["prompts"]
    voiced = [p for p in prompts if any(v in p for v in VOICES)]
    assert voiced and len(voiced) < len(prompts) and len(VOICED) == 2
    for p in voiced:
        start = max(p.rfind(v) for v in VOICES)
        block = p[start:].split("\n\n")[0]
        assert p.count("Your voice:") == 1 and SAMPLES_HEAD not in p
        assert sp.SAMPLES_HEAD in block and "Your humour is" not in p
        assert "chats easily in guild" not in p and "pushes back on plans" not in p
        assert "\n\n" not in p[start + len(block):].strip()  # only her instruction follows
        if mode == "core":  # "Right now:" is the section just before her voice
            assert p[:start].rstrip().split("\n\n")[-1].startswith("Right now:")
    for p in prompts:
        if p not in voiced:
            assert "Your voice:" not in p and sp.SAMPLES_HEAD not in p


@pytest.mark.parametrize("mode,other", [("plain", golden), ("core", golden_core)])
def test_members_without_a_sheet_keep_the_prompts_of_the_other_goldens(mode, other):
    """Every prompt of a member without a sheet is word for word one the golden recorded without
    any sheet holds, the same turns are composed, and the same lines are sent."""
    theirs, ours = other(), golden_voice()[mode]
    unvoiced = [p for p in ours["prompts"] if "Your voice:" not in p]
    assert unvoiced and all(p in set(theirs["prompts"]) for p in unvoiced)
    assert len(ours["prompts"]) == len(theirs["prompts"]) and ours["sent"] == theirs["sent"]
