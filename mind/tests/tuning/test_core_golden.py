"""The core golden (human-likeness core plan Task 8): the tuning scenario with [life] availability 6,
[life] state, [talk] intent and [talk] situation on and the beat off, recorded once in
golden_core.json (scenario.write_golden_core). Its prompts carry the situation mode's hard rules,
"Right now:" (her clock, where she is and how she feels) just before the instruction, the
situation templates with their lengths from the pipeline, and the code's block (her want, and how
his line lands on her) after the instruction of every eligible turn."""
import pytest

from tuning.scenario import engine_clock, golden_core, record


@pytest.fixture
def clock():
    return engine_clock()


def test_core_switches_match_the_recorded_core_golden(store, tmp_path):
    assert record(store, engine_clock(), tmp_path, core=True) == golden_core()
