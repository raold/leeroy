"""The first invariant: with no tuning rows (or only rows that reach nobody), every prompt, weight
and decision is byte-for-byte what it was before tuning existed. golden_prompts.json was recorded
by scenario.py with the code from before tuning touched talk."""
import pytest

from conftest import seed_test_cast
from leeroy_mind.personas.io import PersonaCache, load_persona
from leeroy_mind.personas.schema import Persona
from leeroy_mind.tuning import changes as C
from tuning.scenario import cast, engine_clock, golden, record

HUMAN = 1


@pytest.fixture
def clock():
    return engine_clock()


def _pic(store):
    return C.picture(store, {g: load_persona(store, g) for g in store.persona_versions()})


def test_no_tuning_rows_means_identical_prompts_weights_and_lines(store, clock, tmp_path):
    assert record(store, clock, tmp_path) == golden()


def test_rows_that_reach_nobody_change_nothing(store, clock, tmp_path):
    seed_test_cast(store)
    cast(store)
    store.set_meta("human_guid", str(HUMAN))
    store.conn.commit()
    pic = _pic(store)
    C.save_group(store, None, "nobody", {"gender": ["male"], "stance": ["lust"]},
                 source="cli", now=1.0)
    C.apply_sets(store, [C.Set("group", "1", "talkative", 4),
                         C.Set("member", "101", "talkative", 5),
                         C.Set("member", "102", "argumentative", 8),  # her own value
                         C.Set("member", "103", "humour", 5)], pic, source="cli", now=2.0)
    assert record(store, clock, tmp_path) == golden()


def test_the_cache_hands_out_the_stored_objects_when_nothing_is_set(store):
    seed_test_cast(store)
    cache = PersonaCache(store)
    for g in store.persona_versions():
        assert type(cache.get(g)) is Persona
        assert cache.get(g) is cache.base(g)


def test_a_real_setting_does_change_the_prompts(store, clock, tmp_path):
    """The control: the golden comparison is sensitive to tuning."""
    seed_test_cast(store)
    cast(store)
    store.set_meta("human_guid", str(HUMAN))
    store.conn.commit()
    C.apply_sets(store, [C.Set("group", "everyone", "humour", 4)], _pic(store), source="cli",
                 now=2.0)
    assert record(store, clock, tmp_path)["prompts"] != golden()["prompts"]
