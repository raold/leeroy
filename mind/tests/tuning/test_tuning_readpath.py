"""T3: who reads the effective persona and who the stored one. Readers of behaviour get the
effective persona through PersonaCache.get and load_effective; every writer keeps the stored
one, and an effective persona can never be saved."""
import json

import pytest

from conftest import seed_test_cast
from leeroy_mind.personas.io import (PersonaCache, canonical_json, export_personas,
                                     load_effective, load_persona, save_persona)
from leeroy_mind.personas.schema import Persona
from leeroy_mind.tuning import changes as C
from leeroy_mind.tuning.effective import EffectivePersona, base_of, dials_of
from leeroy_mind.tuning.state import Tuning

HUMAN = 1


@pytest.fixture
def tuned(store):
    seed_test_cast(store)
    store.set_meta("human_guid", str(HUMAN))
    store.conn.commit()
    return store


def put(store, *sets):
    pic = C.picture(store, {g: load_persona(store, g) for g in store.persona_versions()})
    return C.apply_sets(store, [C.Set(*s) for s in sets], pic, source="cli", now=2000.0)


def test_the_cache_gives_readers_the_effective_persona_and_writers_the_stored_one(tuned):
    put(tuned, ("member", "101", "argumentative", 9))
    cache = PersonaCache(tuned)
    eff, base = cache.get(101), cache.base(101)
    assert type(eff) is EffectivePersona and type(base) is Persona
    assert base_of(eff) is base
    assert eff.big_five.agreeableness.score == 2 and base.big_five.agreeableness.score == 5
    assert cache.get(102) is cache.base(102)  # untouched member: the stored object
    assert cache.get(101) is eff  # cached


def test_a_change_reaches_the_cache_through_set_tuning_and_a_full_invalidate(tuned):
    cache = PersonaCache(tuned)
    assert type(cache.get(101)) is Persona
    put(tuned, ("member", "101", "talkative", 9))
    assert type(cache.get(101)) is Persona  # not until it is told
    cache.set_tuning(Tuning.load(tuned.conn))
    assert dials_of(cache.get(101)).values == {"talkative": 9}
    put(tuned, ("member", "101", "talkative", None))
    cache.invalidate()  # the session start: the tuning is read again
    assert type(cache.get(101)) is Persona


def test_an_effective_persona_is_never_saved(tuned):
    put(tuned, ("member", "101", "argumentative", 9))
    eff = PersonaCache(tuned).get(101)
    with pytest.raises(TypeError, match="never saved"):
        save_persona(tuned, eff, source="owner", ts=3000.0, race="", class_name="")
    assert load_persona(tuned, 101).big_five.agreeableness.score == 5


def test_load_effective_reads_the_stored_tuning_or_the_one_given(tuned):
    put(tuned, ("member", "101", "talkative", 9))
    assert dials_of(load_effective(tuned, 101)).values == {"talkative": 9}
    assert type(load_effective(tuned, 101, tuning=Tuning())) is Persona
    assert load_effective(tuned, 999) is None


def test_export_and_import_carry_no_tuning(tuned, tmp_path):
    put(tuned, ("member", "101", "argumentative", 9), ("member", "101", "talkative", 9))
    export_personas(tuned, tmp_path / "out", [101])
    data = json.loads((tmp_path / "out" / "101.json").read_text(encoding="utf-8"))
    assert data["big_five"]["agreeableness"]["score"] == 5
    assert "tuned" not in json.dumps(data) and tuned.persona_versions()[101] == 1
    assert canonical_json(load_persona(tuned, 101)) == tuned.get_persona_json(101)


def test_a_stance_move_on_a_tuned_member_saves_the_stored_persona_and_keeps_her_pin(tuned):
    from leeroy_mind.nightly.attraction import set_stance
    put(tuned, ("member", "102", "talkative", 9))
    with tuned.transaction():
        set_stance(tuned, 102, "fond", now=3000.0, reason="test", evidence=[], score_before=0)
    assert load_persona(tuned, 102).romance.stance == "fond"
    assert Tuning.load(tuned.conn).pins == {102: {"talkative": 9}}
    assert dials_of(PersonaCache(tuned).get(102)).values == {"talkative": 9}
