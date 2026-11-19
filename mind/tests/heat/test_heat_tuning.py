"""Heat in the tuning: stored in tuning_value with no migration, checked like the other dials,
undone and reverted with them; the tune op's arguments; the world romance dial derived from it
only while [heat] is on; and nothing of it folded into a persona."""
import pytest

from conftest import make_persona, seed_test_cast
from engine_rig import save_member
from leeroy_mind.config import load_config
from leeroy_mind.endpoint import BadRequest, tune_args
from leeroy_mind.heat import live as heat_live
from leeroy_mind.heat.config import HeatConfig
from leeroy_mind.heat.live import derived_dial, member_of, resolve_for, world_of
from leeroy_mind.personas.io import load_persona
from leeroy_mind.storage.db import latest_version
from leeroy_mind.tuning import changes as C
from leeroy_mind.tuning.dials import HEAT, MEMBER_DIALS, WORLD_DIALS
from leeroy_mind.tuning.state import Tuning
from leeroy_mind.tuning.world import file_values, merge

ON = HeatConfig(enabled=True)


def _pic(store):
    return C.picture(store, {g: load_persona(store, g) for g in store.persona_versions()})


def _cast(store):
    seed_test_cast(store)
    save_member(store, 102, player_gender="female", stance="drawn")
    save_member(store, 104, player_gender="female", stance="lust")
    store.set_meta("human_guid", "1")


def _set(store, *sets):
    return C.apply_sets(store, [C.Set(scope, target, HEAT, v) for scope, target, v in sets],
                        _pic(store), source="console", now=1000.0)


def test_heat_is_not_a_persona_dial_and_needs_no_migration(store):
    assert HEAT not in MEMBER_DIALS and HEAT not in WORLD_DIALS
    _cast(store)
    before = latest_version()
    _set(store, ("world", "", 7), ("member", "104", 2), ("group", "everyone", -1))
    t = Tuning.load(store.conn)
    assert (t.heat_world, dict(t.heat_pins), dict(t.heat_groups)) == (7, {104: 2},
                                                                      {"everyone": -1})
    assert latest_version() == before
    # never folded into a persona: the stored object itself comes back, and no dial moved
    p = load_persona(store, 104)
    assert t.apply(p) is p and not t.world and not t.pins and not t.offsets
    assert t.heat_of(p) == (2, -1) and t.heat_of(load_persona(store, 102)) == (None, -1)
    assert t.heat_of(None) == (None, 0)


def test_the_checks(store):
    _cast(store)
    pic, groups = _pic(store), {"everyone"}
    ok = lambda scope, target, v: C.check_set(C.Set(scope, target, HEAT, v), pic, groups)  # noqa: E731
    assert ok("world", "", 0).value == 0 and ok("world", "", 10).value == 10
    assert ok("member", "104", -5).value == -5 and ok("member", "104", 0).value == 0  # 0 is kept
    assert ok("group", "everyone", 0).value is None  # a group's 0 is a clear
    assert ok("member", "101", None).value is None  # clearing is always allowed
    for scope, target, v in (("world", "", 11), ("world", "", -1), ("world", "x", 5),
                             ("member", "104", 6), ("member", "104", -6), ("group", "everyone", 6),
                             ("group", "77", 1), ("member", "101", 1),  # a man
                             ("member", "1", 1), ("member", "999", 1), ("world", "", 2.5)):
        with pytest.raises(C.TuneError):
            ok(scope, target, v)


def test_undo_and_revert_carry_heat_like_any_value(store):
    _cast(store)
    first = _set(store, ("world", "", 6)).change
    second = _set(store, ("member", "104", 3)).change
    assert Tuning.load(store.conn).heat_pins == {104: 3}
    C.undo(store, second, _pic(store), source="console", now=1001.0)
    assert not Tuning.load(store.conn).heat_pins and Tuning.load(store.conn).heat_world == 6
    C.revert_to(store, 0, _pic(store), source="console", now=1002.0)
    assert Tuning.load(store.conn).heat_world is None
    C.revert_to(store, first, _pic(store), source="console", now=1003.0)
    assert Tuning.load(store.conn).heat_world == 6
    rows = [r for c in C.history(store.conn) for r in c["rows"]]
    assert {r["dial"] for r in rows} == {HEAT}


def test_candidates_for_a_preview(store):
    _cast(store)
    t = Tuning.load(store.conn)
    c = t.with_value("world", "", HEAT, 4).with_value("member", "104", HEAT, 2) \
         .with_value("group", "everyone", HEAT, 1)
    assert (c.heat_world, c.value_of("member", "104", HEAT), c.value_of("group", "everyone",
                                                                        HEAT)) == (4, 2, 1)
    assert c.with_value("member", "104", HEAT, None).heat_pins == {}
    assert t.heat_world is None and c.value_of("world", "", HEAT) == 4


def test_the_world_follows_the_romance_dial_until_heat_is_set(store):
    _cast(store)
    t = Tuning.load(store.conn)
    assert world_of(t, 9) == (9, 9) and world_of(t, 6) == (5, 6) and world_of(t, 3) == (3, 3)
    _set(store, ("world", "", 4))
    assert world_of(Tuning.load(store.conn), 9) == (4, None)


def test_the_romance_dial_is_derived_only_while_heat_is_on_and_set(store, tmp_path):
    _cast(store)
    cfg = load_config(tmp_path / "absent.toml")
    files = file_values(cfg)
    _set(store, ("world", "", 6))
    t = Tuning.load(store.conn)
    assert derived_dial(t, False) is None and derived_dial(Tuning(), True) is None
    assert merge(cfg, t) is cfg  # [heat] off: stored rows do nothing at all
    heat_live._LIVE = object()  # a mind with [heat] on
    merged = merge(cfg, t)
    assert merged.romance.dial == 7 and derived_dial(t, True) == 7  # heat 6: suggestive
    assert file_values(cfg) == files and cfg.romance.dial == files["romance"]  # never written
    assert merge(cfg, Tuning()) is cfg
    _set(store, ("world", "", 9))
    assert merge(cfg, Tuning.load(store.conn)).romance.dial == 9


def test_resolving_a_member_from_the_stored_rows(store):
    _cast(store)
    _set(store, ("world", "", 7), ("member", "104", 2), ("group", "everyone", -1))
    t = Tuning.load(store.conn)
    lust, drawn, man = (load_persona(store, g) for g in (104, 102, 101))
    r = resolve_for(lust, t, 9, ON)
    assert (r.heat, r.world, r.offset, r.groups, r.tier) == (8, 7, 2, -1, "frank")
    assert resolve_for(drawn, t, 9, ON).heat == 6
    assert not resolve_for(man, t, 9, ON).applies
    assert member_of(man, t).offset is None and member_of(man, t).groups == 0  # women only
    own = make_persona(105, "Ebbo", player_gender="female", stance="lust", dial=10)
    assert resolve_for(own, Tuning(), 9, ON).heat == 10  # her own dial, shown as +1


@pytest.mark.parametrize("args,out", [
    ({"action": "heat", "session": "clean"}, {"action": "heat", "session": "clean", "guid": None}),
    ({"action": "heat", "session": "open", "guid": 104},
     {"action": "heat", "session": "open", "guid": 104}),
    ({"action": "heat", "session": "clear", "guid": None},
     {"action": "heat", "session": "clear", "guid": None}),
])
def test_the_tune_ops_heat_action(args, out):
    assert tune_args(args) == out


@pytest.mark.parametrize("args", [
    {"action": "heat"}, {"action": "heat", "session": "wild"},
    {"action": "heat", "session": "open", "guid": 0}, {"action": "heat", "session": "open",
                                                       "guid": "104"},
    {"action": "heat", "session": "clean", "words": "keep it clean"},
])
def test_the_heat_action_refuses_anything_else(args):
    with pytest.raises(BadRequest):
        tune_args(args)


def test_set_and_preview_take_heat_as_a_dial_name():
    args = {"action": "set", "scope": "world", "target": "", "dial": "heat", "value": 6,
            "previewed": False}
    assert tune_args(args)["dial"] == "heat"
    assert tune_args({"action": "preview", "scope": "member", "target": "104", "dial": "heat",
                      "value": -2})["value"] == -2
