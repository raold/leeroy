"""T2: the dial registry, groups, precedence, the effective persona and the tuning lines."""
import dataclasses
from types import SimpleNamespace

import pytest

from conftest import make_persona, seed_test_cast
from leeroy_mind.config import load_config
from leeroy_mind.personas.io import load_persona
from leeroy_mind.personas.schema import Persona, validate_persona
from leeroy_mind.tuning import changes as C
from leeroy_mind.tuning import groups as G
from leeroy_mind.tuning.dials import MEMBER_DIALS, WORLD_DIALS, applies, base_value
from leeroy_mind.tuning.effective import (NEUTRAL, EffectivePersona, base_of, dials_of,
                                          effective)
from leeroy_mind.tuning.lines import BUILTIN_LINES, tuned_card, tuning_lines
from leeroy_mind.tuning.state import Tuning
from leeroy_mind.tuning.world import merge, shadowed

HUMAN = 1
LATER = ("crude", "ego", "patience", "loot_greed", "raid_comms")


def woman(guid=102, name="Belra", **kw):
    return make_persona(guid, name, player_gender="female", stance="drawn", **kw)


@pytest.fixture
def tuned(store):
    seed_test_cast(store)
    store.set_meta("human_guid", str(HUMAN))
    store.conn.commit()
    return store


def pic(store):
    return C.picture(store, {g: load_persona(store, g) for g in store.persona_versions()})


def apply(store, scope, target, dial, value, **kw):
    return C.apply_sets(store, [C.Set(scope, str(target), dial, value)], pic(store),
                        source="console", now=2000.0, **kw)


def t(store) -> Tuning:
    return Tuning.load(store.conn)


# -- the registry -------------------------------------------------------------------------------

def test_the_eleven_built_dials_and_three_world_dials_and_none_of_the_later_ones():
    assert set(MEMBER_DIALS) == {"talkative", "argumentative", "forward", "humour", "theorycraft",
                                 "lore", "drama", "jealousy", "party", "openup", "reliability"}
    assert set(WORLD_DIALS) == {"chattiness", "romance", "jealousy"}
    assert not set(LATER) & (set(MEMBER_DIALS) | set(WORLD_DIALS))


def test_score_dials_start_at_the_persona_and_level_dials_at_five():
    p = make_persona(101, "Arkon", agreeableness={"score": 3, "behaviour": "x"})
    assert base_value("argumentative", p) == 8  # agreeableness the other way up
    assert base_value("reliability", p) == p.big_five.conscientiousness.score == 6
    assert all(base_value(d, p) == 5 for d in MEMBER_DIALS if MEMBER_DIALS[d].kind == "level")


def test_forward_is_for_women_and_party_only_for_drinkers_and_smokers():
    man = make_persona(101, "Arkon")
    assert not applies("forward", man) and applies("forward", woman())
    assert not applies("party", man)
    coffee = make_persona(101, "Arkon", habits=[{"substance": "caffeine", "when": "daily",
                                                 "odds": 1.0, "max_state": "wired"}])
    assert not applies("party", coffee)
    smoker = make_persona(101, "Arkon", habits=[{"substance": "tobacco", "when": "daily",
                                                 "odds": 0.5, "max_state": "wired"}])
    assert applies("party", smoker)


# -- groups -------------------------------------------------------------------------------------

def test_a_predicate_is_fields_from_a_fixed_list_with_known_values():
    assert G.parse_where({"gender": ["female"], "age": [18, 30]}) == {
        "age": [18, 30], "gender": ["female"]}
    for bad in ({}, {"name": ["x"]}, {"gender": ["other"]}, {"gender": "female"},
                {"age": [10, 30]}, {"age": [30, 20]}, {"rank": ["1"]}, {"core": [1]},
                {"secret": ["x"]}, [], "gender=female"):
        with pytest.raises(G.GroupError):
            G.parse_where(bad)
    with pytest.raises(G.GroupError):
        G.parse_name("everyone")


def test_membership_reads_the_persona_and_the_cast():
    p = woman()
    member = SimpleNamespace(class_=8, race=4, rank=2)
    assert G.matches({"gender": ["female"]}, p, None)
    assert not G.matches({"gender": ["male"]}, p, None)
    assert G.matches({"class": ["Mage"], "rank": [1, 2]}, p, member)
    assert not G.matches({"class": ["Mage"]}, p, None)  # a cast field with no cast row
    assert G.matches({"habit": ["weed", "alcohol"]}, make_persona(
        101, "Arkon", habits=[{"substance": "alcohol", "when": "daily", "odds": 0.5,
                               "max_state": "drunk"}]), None)
    assert G.matches({"age": [30, 40]}, p, None) and not G.matches({"age": [18, 20]}, p, None)


# -- precedence ---------------------------------------------------------------------------------

def test_no_rows_means_every_member_is_untouched(tuned):
    tn = t(tuned)
    assert tn.empty and tn.version == 0
    for g in tuned.persona_versions():
        p = load_persona(tuned, g)
        assert tn.member(p) is NEUTRAL
        assert tn.apply(p) is p  # the stored object itself


def test_a_member_pin_wins_over_groups_and_offsets_add_and_clamp(tuned):
    C.save_group(tuned, None, "tanks", {"raid_role": ["tank"]}, source="console", now=1.0)
    apply(tuned, "group", "everyone", "talkative", 2)
    apply(tuned, "group", "1", "talkative", 4)
    tn = t(tuned)
    p = load_persona(tuned, 101)  # every test persona is a tank
    assert tn.member(p).values["talkative"] == 10  # 5 + 2 + 4, clamped
    apply(tuned, "member", 101, "talkative", 3)
    tn = t(tuned)
    assert tn.member(p).values["talkative"] == 3 and "talkative" in tn.member(p).pinned
    assert tn.member(load_persona(tuned, 102)).values["talkative"] == 10
    apply(tuned, "member", 101, "talkative", 5)  # a pin at neutral is still a pin: as written
    assert "talkative" not in t(tuned).member(p).values


def test_score_dials_offset_from_the_persona_and_clamp_to_one_to_ten(tuned):
    apply(tuned, "group", "everyone", "argumentative", 9)
    p = load_persona(tuned, 101)  # agreeableness 5: argumentative 6
    eff = t(tuned).apply(p)
    assert dials_of(eff).values["argumentative"] == 10
    assert eff.big_five.agreeableness.score == 1


def test_the_owner_and_men_are_never_reached_by_what_is_not_theirs(tuned):
    apply(tuned, "group", "everyone", "forward", 3)
    tn = t(tuned)
    assert "forward" not in tn.member(load_persona(tuned, 101)).values  # a man
    human = make_persona(HUMAN, "Testmage", player_gender="female", stance="lust")
    assert tn.member(human) is NEUTRAL


# -- the effective persona ----------------------------------------------------------------------

def test_the_effective_persona_is_a_persona_with_the_same_fields_and_its_base():
    p = make_persona(101, "Arkon", habits=[
        {"substance": "alcohol", "when": "daily", "odds": 0.4, "max_state": "drunk"},
        {"substance": "caffeine", "when": "daily", "odds": 0.4, "max_state": "wired"}])
    tn = Tuning().with_value("member", "101", "party", 10).with_value(
        "member", "101", "argumentative", 9)
    eff = tn.apply(p)
    assert isinstance(eff, Persona) and type(eff) is EffectivePersona
    assert [f.name for f in dataclasses.fields(eff)] == [f.name for f in dataclasses.fields(p)]
    assert base_of(eff) is p and base_of(p) is p
    assert eff.big_five.agreeableness.score == 2
    assert eff.big_five.agreeableness.behaviour == ""  # far from her own: the line says it
    assert [h.odds for h in eff.habits] == [0.8, 0.4]  # never coffee
    assert validate_persona(eff) == []
    assert effective(eff, NEUTRAL) is eff
    assert dials_of(p) is NEUTRAL and dials_of(None) is NEUTRAL


def test_a_near_score_keeps_the_persona_s_own_words_and_party_never_passes_one():
    p = make_persona(101, "Arkon", habits=[
        {"substance": "alcohol", "when": "daily", "odds": 0.7, "max_state": "drunk"}])
    eff = Tuning().with_value("member", "101", "argumentative", 7).with_value(
        "member", "101", "party", 10).apply(p)
    assert eff.big_five.agreeableness.behaviour == p.big_five.agreeableness.behaviour
    assert eff.habits[0].odds == 1.0


# -- tuning lines -------------------------------------------------------------------------------

def test_lines_come_two_from_neutral_and_never_for_a_stored_persona(tmp_path):
    p = make_persona(101, "Arkon")
    assert tuning_lines(p) == []
    near = Tuning().with_value("member", "101", "humour", 6).apply(p)
    assert tuning_lines(near) == []
    far = Tuning().with_value("member", "101", "humour", 8).with_value(
        "member", "101", "lore", 2).with_value("member", "101", "talkative", 10).apply(p)
    assert tuning_lines(far) == [BUILTIN_LINES[("humour", "high")], BUILTIN_LINES[("lore", "low")]]
    assert tuned_card("CARD", p) == "CARD"
    assert tuned_card("CARD", far).startswith("CARD\n")


def test_a_local_overlay_replaces_a_built_in_line_capped(tmp_path):
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "tuning_humour_high.txt").write_text("LOCAL " * 400, encoding="utf-8")
    far = Tuning().with_value("member", "101", "humour", 9).apply(make_persona(101, "Arkon"))
    line = tuning_lines(far, tmp_path)[0]
    assert line.startswith("LOCAL") and len(line) <= 200 * 4
    assert tuning_lines(far) == [BUILTIN_LINES[("humour", "high")]]  # no data dir: built-in


def test_built_in_lines_are_plain_and_non_explicit():
    words = " ".join(BUILTIN_LINES.values()).casefold()
    for w in ("sex", "naked", "fuck", "horny", "explicit"):
        assert w not in words


# -- world --------------------------------------------------------------------------------------

def test_world_overrides_replace_mind_toml_values_without_writing_it(tmp_path):
    path = tmp_path / "mind.toml"
    cfg = load_config(path)
    assert merge(cfg, Tuning()) is cfg
    tn = Tuning().with_value("world", "", "chattiness", 8).with_value("world", "", "romance", 2)
    out = merge(cfg, tn)
    assert (out.talk.chattiness, out.romance.dial, out.romance.jealousy) == (
        8, 2, cfg.romance.jealousy)
    assert shadowed(cfg, tn) == [("chattiness", 4, 8), ("romance", 3, 2)]
    assert not path.exists()
