"""T2: the change log. One owner action is one change; undo is one more; revert folds the log."""
import pytest

from conftest import seed_test_cast
from leeroy_mind.personas.io import load_persona
from leeroy_mind.tuning import changes as C
from leeroy_mind.tuning.state import Tuning

HUMAN = 1


@pytest.fixture
def tuned(store):
    seed_test_cast(store)
    store.set_meta("human_guid", str(HUMAN))
    store.conn.commit()
    return store


def pic(store):
    return C.picture(store, {g: load_persona(store, g) for g in store.persona_versions()})


def put(store, scope, target, dial, value, now=2000.0):
    return C.apply_sets(store, [C.Set(scope, str(target), dial, value)], pic(store),
                        source="console", now=now)


def values(store):
    return {(r[0], r[1], r[2]): r[3] for r in store.conn.execute(
        "SELECT scope,target,dial,value FROM tuning_value")}


def test_a_set_is_one_change_with_its_row_and_the_version_stamp(tuned):
    r = put(tuned, "member", 102, "talkative", 8)
    assert r.change == 1 and r.rows == 1
    assert values(tuned) == {("member", "102", "talkative"): 8}
    row = tuned.conn.execute("SELECT before,after,persona_version FROM tuning_row").fetchone()
    assert tuple(row) == (None, 8, 1)
    assert tuned.get_meta("tuning_version") == "1"
    assert put(tuned, "member", 102, "talkative", 8).change is None  # nothing moved
    assert tuned.conn.execute("SELECT count(*) FROM tuning_change").fetchone()[0] == 1


def test_a_world_change_is_one_row_however_many_it_reaches(tuned):
    put(tuned, "world", "", "chattiness", 7)
    put(tuned, "group", "everyone", "drama", 3)
    assert tuned.conn.execute("SELECT count(*) FROM tuning_row").fetchone()[0] == 2
    assert Tuning.load(tuned.conn).world == {"chattiness": 7}


@pytest.mark.parametrize("scope,target,dial,value,words", [
    ("member", 1, "talkative", 6, "own character"),
    ("member", 999, "talkative", 6, "cast members"),
    ("member", 101, "forward", 6, "Men stay friends"),
    ("member", 101, "party", 6, "no drinking or smoking"),
    ("member", 101, "talkative", 11, "runs 0-10"),
    ("member", 101, "argumentative", 0, "runs 1-10"),
    ("member", 101, "crude", 6, "member dials"),
    ("member", 101, "loot_greed", 6, "member dials"),
    ("world", "", "raid_comms", 6, "world dials"),
    ("world", "", "romance", 0, "runs 1-10"),
    ("group", "7", "talkative", 2, "no such group"),
    ("group", "everyone", "talkative", 11, "offset"),
    ("member", 101, "talkative", 6.5, "whole number"),
    ("member", 101, "talkative", True, "whole number"),
])
def test_what_tuning_refuses(tuned, scope, target, dial, value, words):
    with pytest.raises(C.TuneError, match=words):
        put(tuned, scope, target, dial, value)
    assert tuned.conn.execute("SELECT count(*) FROM tuning_change").fetchone()[0] == 0


def test_an_offset_of_zero_clears_it(tuned):
    put(tuned, "group", "everyone", "talkative", 2)
    put(tuned, "group", "everyone", "talkative", 0)
    assert values(tuned) == {}


def test_undo_reverses_and_undoing_the_undo_redoes(tuned):
    put(tuned, "member", 102, "talkative", 8)
    u = C.undo(tuned, 1, pic(tuned), source="console", now=2100.0)
    assert values(tuned) == {} and u.change == 2
    assert tuned.conn.execute("SELECT undoes FROM tuning_change WHERE id=2").fetchone()[0] == 1
    C.undo(tuned, 2, pic(tuned), source="console", now=2200.0)
    assert values(tuned) == {("member", "102", "talkative"): 8}
    hist = C.history(tuned.conn)
    assert [h["id"] for h in hist] == [3, 2, 1]
    assert hist[2]["undone_by"] == 2 and hist[1]["undone_by"] == 3


def test_undo_refuses_when_a_later_change_moved_it_and_names_it(tuned):
    put(tuned, "member", 102, "talkative", 8)
    put(tuned, "member", 102, "talkative", 9)
    with pytest.raises(C.TuneError, match="#2"):
        C.undo(tuned, 1, pic(tuned), source="console", now=2100.0)
    assert values(tuned) == {("member", "102", "talkative"): 9}


def test_revert_folds_the_log_to_right_after_a_change(tuned):
    put(tuned, "member", 102, "talkative", 8)
    put(tuned, "world", "", "romance", 6)
    put(tuned, "member", 102, "talkative", 2)
    r = C.revert_to(tuned, 1, pic(tuned), source="console", now=3000.0)
    assert values(tuned) == {("member", "102", "talkative"): 8}
    assert r.rows == 2
    assert C.revert_to(tuned, 1, pic(tuned), source="console", now=3001.0).change is None
    C.revert_to(tuned, 0, pic(tuned), source="console", now=3002.0)
    assert values(tuned) == {}


def test_groups_are_logged_and_undone_like_values(tuned):
    g = C.save_group(tuned, None, "women", {"gender": ["female"]}, source="console", now=1.0)
    put(tuned, "group", "1", "forward", 2)
    assert "1" in Tuning.load(tuned.conn).offsets
    C.retire_group(tuned, 1, source="console", now=2.0)
    assert "1" not in Tuning.load(tuned.conn).groups  # its offsets reach nobody
    with pytest.raises(C.TuneError, match="no such group"):
        put(tuned, "group", "1", "forward", 3)
    C.undo(tuned, 3, pic(tuned), source="console", now=3.0)
    assert Tuning.load(tuned.conn).groups["1"].where == {"gender": ["female"]}
    assert g.change == 1
    with pytest.raises(C.TuneError, match="already called"):
        C.save_group(tuned, None, "women", {"gender": ["male"]}, source="console", now=4.0)


def test_the_log_carries_no_persona_text(tuned):
    put(tuned, "member", 102, "talkative", 8)
    dump = "\n".join(" ".join(map(str, r)) for t in ("tuning_change", "tuning_row", "tuning_value")
                     for r in tuned.conn.execute(f"SELECT * FROM {t}"))
    p = load_persona(tuned, 102)
    assert p.character_name not in dump and p.identity.real_name not in dump
