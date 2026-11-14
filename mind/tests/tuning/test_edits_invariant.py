"""Godmode keeps tuning's first invariant: edits undone or retired, and Discord intents waiting,
leave every game prompt, weight and line byte-for-byte as golden_prompts.json recorded them."""
import pytest

from conftest import seed_test_cast
from leeroy_mind.godmode import intents as I
from leeroy_mind.human import HumanIdentity
from leeroy_mind.personas.io import load_persona
from leeroy_mind.tuning import changes as C
from leeroy_mind.tuning import edits as E
from tuning.scenario import cast, engine_clock, golden, record

HUMAN = HumanIdentity(1, "Testmage", "Zan", "Quillon", True)


@pytest.fixture
def clock():
    return engine_clock()


def _pic(store):
    return C.picture(store, {g: load_persona(store, g) for g in store.persona_versions()})


def _edit(store, kind, **a):
    return E.set_edit(store, {"kind": kind, "args": a}, human=HUMAN, source="cli",
                      now=store.clock.now(), pic=_pic(store))


def test_undone_and_retired_edits_and_waiting_intents_change_nothing(store, clock, tmp_path):
    seed_test_cast(store)
    cast(store)
    store.set_meta("human_guid", "1")
    store.conn.commit()
    r = _edit(store, "trait", member=101, path="likes", value=["loud raids"])
    C.undo(store, r["change"], _pic(store), source="cli", now=store.clock.now())
    r = _edit(store, "feeling", member=103, other=1, affinity=50)
    E.retire(store, r["id"], source="cli", now=store.clock.now(), human=1)
    r = _edit(store, "memory_implant", member=105, text="Won a fishing contest once.")
    C.undo(store, r["change"], _pic(store), source="cli", now=store.clock.now())
    I.create(store, member=102, kind="post", args={"channel": "irl", "about": "her new flat"},
             human=HUMAN, now=store.clock.now(), pic=_pic(store))
    store.reload_edits()
    assert record(store, clock, tmp_path) == golden()


def test_an_edit_in_force_does_change_the_prompts(store, clock, tmp_path):
    """The control: the golden comparison is sensitive to godmode."""
    seed_test_cast(store)
    cast(store)
    store.set_meta("human_guid", "1")
    store.conn.commit()
    _edit(store, "trait", member=103, path="humour", value="absurd")
    assert record(store, clock, tmp_path)["prompts"] != golden()["prompts"]
