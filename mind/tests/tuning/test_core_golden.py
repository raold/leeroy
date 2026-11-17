"""The core golden (human-likeness core plan Task 8): the tuning scenario with [life] availability 6,
[life] state, [talk] intent and [talk] situation on and the beat off, recorded once in
golden_core.json (scenario.write_golden_core). Its prompts carry the situation mode's hard rules,
"Right now:" (her clock, where she is and how she feels) just before the instruction, the
situation templates with their lengths from the pipeline, and the code's block (her want, and how
his line lands on her) after the instruction of every eligible turn.

Re-recorded on 2026-11-16 for DM quality (test_tuning_invariant.py): 14 of its prompts lose the
"Lately" hint that named the chat word "line" as a topic, and the first answer to his question
gains "Answer his question first, plainly, then whatever else."; nothing else moved."""
import pytest

from tuning.scenario import engine_clock, golden_core, record


@pytest.fixture
def clock():
    return engine_clock()


def test_core_switches_match_the_recorded_core_golden(store, clock, tmp_path):
    """`clock`: the store's own (record's contract), so his lines carry the scenario's time."""
    assert record(store, clock, tmp_path, core=True) == golden_core()
