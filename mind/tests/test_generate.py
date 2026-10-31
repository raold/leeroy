import dataclasses
import itertools
import json
import logging
import random
import re
from collections import Counter
from types import SimpleNamespace

import pytest

from conftest import _base_persona_dict, make_persona, run, seed_test_cast
from leeroy_mind.config import CastCfg
from leeroy_mind.human import HumanIdentity
from leeroy_mind.llm.presets import JSONJobFailed
from leeroy_mind.personas.generate import (AGE_BANDS, GENERATION_RULES, GenNotes, GenSlot,
                                           NameCollision, SPECS, cast_summary, class_roles,
                                           generate_cast, generate_persona, mix_report,
                                           plan_slots, spec_for)
from leeroy_mind.personas.io import load_persona, save_persona
from leeroy_mind.personas.schema import persona_from_dict
from leeroy_mind.personas.stances import NONE_FIT_AGE, stance_targets
from leeroy_mind.prompts import load_prompt
from leeroy_mind.protocol import Member
from leeroy_mind.testing.fake_llm import FakeLLM, script_from
from leeroy_mind.wow import CAST_MIN_LEVEL

HUMAN = 1
H = HumanIdentity(HUMAN, "Testmage", "Testy", "Pat", True)


CLASS_IDS = [1, 2, 3, 4, 5, 6, 7, 8, 9, 11]  # every class, in a fixed cycle


def _member(guid, name, level=80, rank=3, class_=1):
    return Member(guid=guid, name=name, race=2, class_=class_, gender=0, level=level, rank=rank,
                  online=False, map=0, zone=0, spec_tab=-1)


def _roster(n_cast=96, alts=3):
    out = [_member(HUMAN, "Testmage", 83, 0)]
    out += [_member(1000 + i, f"Cast{i}", class_=CLASS_IDS[i % 10]) for i in range(n_cast)]
    out += [_member(2000 + i, f"Alt{i}", level=30) for i in range(alts)]
    return out


def _seed(guid, name, female, introvert, role="melee"):
    d = _base_persona_dict(guid, name)
    d["identity"]["player_gender"] = "female" if female else "male"
    d["big_five"]["extraversion"]["score"] = 3 if introvert else 7
    d["play"]["raid_role"] = role
    return persona_from_dict(d)


def _cast_members(roster):
    return [m for m in roster if m.guid != HUMAN and m.level >= CAST_MIN_LEVEL]


def _seeds(roster, n=20, women=13, introverts=15):
    """20 seeds, 13 women; the first is a woman tank (a seed may break the role rule), four
    more women are healers, and one man is a tank."""
    roles = {0: "tank", 1: "healer", 2: "healer", 3: "healer", 4: "healer", 15: "tank"}
    return [_seed(m.guid, m.name, i < women, i < introverts, roles.get(i, "melee"))
            for i, m in enumerate(_cast_members(roster)[:n])]


def _slot(**kw):
    base = dict(guid=201, player_gender="male", introvert=True, guild_role="raider",
                region="UK", drinker=False, romance_stance="none", jealousy_mode="sulker",
                thinks_as="lore", age_band=(33, 40))
    base.update(kw)
    return GenSlot(**base)


CFG70 = CastCfg(0.7, 0.7)


def _plan(seed=1, cfg=CFG70, roster=None):
    roster = roster or _roster()
    seeds = _seeds(roster)
    return roster, seeds, plan_slots(roster, seeds, cfg, HUMAN, random.Random(seed))


def test_plan_hits_mix_targets():
    roster, seeds, slots = _plan()
    cast_n = len(_cast_members(roster))
    assert len(slots) == cast_n - len(seeds)
    female = 13 + sum(s.player_gender == "female" for s in slots)
    assert abs(female / cast_n - 0.70) <= 0.01
    assert sum(s.introvert for s in slots) == round(0.7 * cast_n) - 15
    seeded = {p.guid for p in seeds}
    assert not ({s.guid for s in slots} & seeded) and HUMAN not in {s.guid for s in slots}
    assert all(1000 <= s.guid < 2000 for s in slots)  # no human, no low-level alts


def test_plan_roles_follow_the_amendment():
    roster, seeds, slots = _plan()
    by_guid = {m.guid: m for m in roster}
    cast_n = len(_cast_members(roster))
    assert all(s.player_gender == "female" for s in slots if s.combat_role == "healer")
    assert all(s.player_gender == "male" for s in slots if s.combat_role == "tank")
    for s in slots:  # a role is only given to a class that can fill it
        assert s.combat_role in class_roles(by_guid[s.guid].class_)
    tanks = 2 + sum(s.combat_role == "tank" for s in slots)
    healers = 4 + sum(s.combat_role == "healer" for s in slots)
    assert abs(tanks - round(0.10 * cast_n)) <= 1 and abs(healers - round(0.24 * cast_n)) <= 1
    assert any(s.combat_role == "damage" and s.player_gender == "female" for s in slots)
    assert any(s.combat_role == "damage" and s.player_gender == "male" for s in slots)


def test_fixture_mix_report():
    r = mix_report(_seeds(_roster()))
    assert r.roles == {"tank": 2, "healer": 4, "damage": 14}
    assert r.female_by_role["tank"] == 0.5 and r.female_by_role["healer"] == 1.0


def test_class_capability():
    from leeroy_mind.wow import CLASSES
    by_name = {CLASSES[i]: class_roles(i) for i in CLASSES}
    for c in ("Warrior", "Death Knight"):
        assert by_name[c] == {"tank", "damage"}
    for c in ("Priest", "Shaman"):
        assert by_name[c] == {"healer", "damage"}
    for c in ("Paladin", "Druid"):
        assert by_name[c] == {"tank", "healer", "damage"}
    for c in ("Mage", "Warlock", "Rogue", "Hunter"):
        assert by_name[c] == {"damage"}


def test_spec_matches_role_and_class():
    from leeroy_mind.wow import CLASSES
    ids = {v: k for k, v in CLASSES.items()}
    assert spec_for(ids["Paladin"], "tank", 1) == ("Protection", "tank")
    assert spec_for(ids["Druid"], "healer", 1) == ("Restoration", "healer")
    assert spec_for(ids["Druid"], "damage", 2)[0] in ("Balance", "Feral")
    assert spec_for(ids["Warrior"], "tank", 1) == ("Protection", "tank")
    assert spec_for(ids["Mage"], "tank", 1) is None
    for cname, roles in SPECS.items():
        for role, options in roles.items():
            assert role in class_roles(ids[cname])
            assert all(rr == role or (role == "damage" and rr in ("melee", "ranged"))
                       for _, rr in options)


def test_generate_forces_main_spec_and_raid_role_from_the_slot():
    d = json.loads(_persona_json())
    d["play"]["main_spec"] = "Wrong"
    d["play"]["raid_role"] = "ranged"
    llm = FakeLLM(script_from(["profile", json.dumps(d)]))
    healer = _member(201, "Testra", class_=11)  # a Druid
    p = run(generate_persona(llm, "m", _slot(combat_role="healer", player_gender="female",
                                             jealousy_mode=None),
                             healer, "", GENERATION_RULES))
    assert (p.play.main_spec, p.play.raid_role) == ("Restoration", "healer")
    llm = FakeLLM(script_from(["profile", json.dumps(d)]))
    p = run(generate_persona(llm, "m", _slot(combat_role="tank"), healer, "", GENERATION_RULES))
    assert (p.play.main_spec, p.play.raid_role) == ("Feral", "tank")
    assert "Feral as tank" in llm.calls[0]["messages"][-1]["content"]


def test_plan_conflict_keeps_the_role_gender_rule_and_logs(caplog):
    roster = _roster()
    with caplog.at_level(logging.INFO):
        slots = plan_slots(roster, _seeds(roster), CastCfg(0.1, 0.1), HUMAN, random.Random(1))
    assert all(s.player_gender == "female" for s in slots if s.combat_role == "healer")
    assert all(s.player_gender == "male" for s in slots if s.combat_role == "tank")
    assert sum(s.player_gender == "female" for s in slots if s.combat_role == "damage") == 0
    assert "cast plan: female share" in caplog.text
    warned = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warned) == 1 and "off target" in warned[0] and "(target 0.10)" in warned[0]


def test_an_on_target_plan_does_not_warn(caplog):
    with caplog.at_level(logging.INFO, logger="leeroy_mind.generate"):
        _plan()
    assert "cast plan: female share" in caplog.text
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def _cli_store(tmp_path, members):
    from leeroy_mind.clock import SystemClock
    from leeroy_mind.storage.db import open_db
    from leeroy_mind.storage.store import Store
    s = Store(open_db(tmp_path / "mind.db"), clock=SystemClock())
    s.upsert_cast(members, 1.0)
    s.close()


def test_generate_cli_warns_when_the_plan_misses_a_role_target(tmp_path, caplog):
    from leeroy_mind.__main__ import main
    _cli_store(tmp_path, [_member(HUMAN, "Testmage", 83, 0)]
               + [_member(10 + i, f"Mage{i}", class_=8) for i in range(10)])  # no tank or healer
    with caplog.at_level(logging.WARNING, logger="leeroy_mind.generate"):
        assert main(["personas", "--data-dir", str(tmp_path), "generate", "--dry-run"]) == 0
    warned = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warned) == 1 and "off target" in warned[0]
    assert "tanks 0/1" in warned[0] and "healers 0/2" in warned[0]


def test_plan_is_deterministic():
    roster = _roster()
    seeds = _seeds(roster)
    a = plan_slots(roster, seeds, CFG70, HUMAN, random.Random(5))
    b = plan_slots(roster, seeds, CFG70, HUMAN, random.Random(5))
    c = plan_slots(roster, seeds, CFG70, HUMAN, random.Random(6))
    assert a == b and a != c


def test_plan_without_a_healer_class_gives_no_healers():
    roster = [_member(HUMAN, "Testmage", 83, 0)] + [_member(1000 + i, f"C{i}", class_=8)
                                                    for i in range(10)]
    slots = plan_slots(roster, [], CFG70, HUMAN, random.Random(1))
    assert {s.combat_role for s in slots} == {"damage"}
    assert sum(s.player_gender == "female" for s in slots) == 7


def test_plan_other_mixes():
    _, _, slots = _plan(2)
    n = len(slots)
    assert sum(s.region == "UK" for s in slots) == round(n / 3)
    assert sum(s.guild_role == "raider" for s in slots) == round(0.7 * n)
    assert {s.guild_role for s in slots} == {"raider", "social"}
    assert sum(s.drinker for s in slots) == round(0.45 * n)
    assert {s.thinks_as for s in slots} == {"theorycrafter", "lore", "social", "collector",
                                            "competitor"}
    assert {s.age_band for s in slots} <= {(21, 25), (26, 32), (33, 40), (41, 55),
                                         (41, NONE_FIT_AGE - 1)}  # a lust slot's


def _with_stances(seeds, stances):
    """Give the seed women these stances in order (seeds keep their stance)."""
    out, it = [], iter(stances)
    for p in seeds:
        if p.identity.player_gender == "female":
            p = dataclasses.replace(p, romance=dataclasses.replace(p.romance, stance=next(it)))
        out.append(p)
    return out


def test_women_stances_follow_the_mix_over_the_whole_cast():
    roster = _roster()
    seeds = _with_stances(_seeds(roster), ["lust"] + ["drawn"] * 2 + ["fond"] * 8
                          + ["none"] * 2)
    slots = plan_slots(roster, seeds, CFG70, HUMAN, random.Random(3))
    women = [s.romance_stance for s in slots if s.player_gender == "female"]
    women += [p.romance.stance for p in seeds if p.identity.player_gender == "female"]
    assert Counter(women) == stance_targets(len(women))  # 20/30/40/10 of every woman
    men = [s for s in slots if s.player_gender == "male"]
    assert all(s.jealousy_mode for s in men)
    assert {s.jealousy_mode for s in men} == {"good_sport", "sulker", "grumbler", "teaser",
                                              "play_by_play"}
    assert all(s.romance_stance == "none" for s in men)


def test_seed_stances_count_toward_the_mix():
    _, seeds, slots = _plan(3)  # the 13 seed women are all "none", over its 10%
    women = [s for s in slots if s.player_gender == "female"]
    assert not [s for s in women if s.romance_stance == "none"]
    got = Counter(s.romance_stance for s in women)
    assert got["fond"] > got["drawn"] > got["lust"] > 0


def test_lust_slot_is_written_as_the_most_forward():
    slot = _slot(introvert=False, player_gender="female", jealousy_mode=None,
                 romance_stance="lust", region="US", age_band=(26, 32))
    llm = FakeLLM(script_from(["profile", _persona_json()]))
    p = run(generate_persona(llm, "m", slot, _member_char(), "", GENERATION_RULES,
                             human=dataclasses.replace(H, pronouns="he")))
    assert p.romance.stance == "lust" and p.romance.style and p.romance.jealousy_mode is None
    prompt = llm.calls[0]["messages"][-1]["content"]
    assert "openly wants Testy" in prompt and "explicit" in prompt and "Pat" not in prompt


def test_lust_slots_are_outgoing_and_under_the_grandmother_age():
    forties = 0
    for seed in range(10):
        roster, seeds, slots = _plan(seed)
        lust = [s for s in slots if s.romance_stance == "lust"]
        assert lust and all(not s.introvert for s in lust)
        assert all(s.age_band[1] < NONE_FIT_AGE for s in lust)  # never 50 or over
        forties += sum(s.age_band == (41, NONE_FIT_AGE - 1) for s in lust)
        cast_n = len(_cast_members(roster))
        assert sum(s.introvert for s in slots) == round(0.7 * cast_n) - 15  # the share holds
        assert all(s.age_band in AGE_BANDS for s in slots if s.romance_stance != "lust")
    assert forties > 0  # the 41-55 band stays open to lust, capped at 49


@pytest.mark.parametrize("model_age,want", [(55, 49), (47, 47), (30, 41)])
def test_a_lust_woman_is_never_50(model_age, want):
    d = json.loads(_persona_json())
    d["identity"]["age"] = model_age
    slot = _slot(introvert=False, player_gender="female", jealousy_mode=None,
                 romance_stance="lust", age_band=(41, NONE_FIT_AGE - 1))
    p = run(generate_persona(FakeLLM(script_from(["profile", json.dumps(d)])), "m", slot,
                             _member_char(), "", GENERATION_RULES))
    assert p.identity.age == want


@pytest.mark.parametrize("given,want", [("shy", "bold"), ("guarded", "bold"), (None, "bold"),
                                        ("earnest", "bold"), ("theatrical", "theatrical"),
                                        ("competitive", "competitive"), ("bold", "bold")])
def test_lust_style_is_bold_theatrical_or_competitive(given, want):
    d = json.loads(_persona_json())
    d["romance"]["style"] = given
    slot = _slot(introvert=False, player_gender="female", jealousy_mode=None,
                 romance_stance="lust", age_band=(26, 32))
    p = run(generate_persona(FakeLLM(script_from(["profile", json.dumps(d)])), "m", slot,
                             _member_char(), "", GENERATION_RULES))
    assert p.romance.style == want


def test_plan_counts_only_cast_level_seeds():
    roster = _roster(n_cast=10, alts=1)
    seeds = [_seed(2000, "Alt0", True, True)]  # a low-level alt is not cast: it must not count
    slots = plan_slots(roster, seeds, CFG70, HUMAN, random.Random(1))
    assert len(slots) == 10 and sum(s.player_gender == "female" for s in slots) == 7


def test_mix_report_and_summary():
    ps = [_seed(1, "Aa", True, True), _seed(2, "Bb", False, False),
          _seed(3, "Cc", True, False), _seed(4, "Dd", True, True)]
    r = mix_report(ps)
    assert (r.total, r.female_share, r.introvert_share) == (4, 0.75, 0.5)
    assert mix_report([]).total == 0
    s = cast_summary(ps)
    assert "Aa" in s and "Dd" in s and len(s.splitlines()) == 4


def _persona_json(real_name="Rae Holt", **edits):
    d = _base_persona_dict(999, "Wrong")
    d["identity"]["real_name"] = real_name
    d["big_five"]["extraversion"]["score"] = 9
    d["game"]["years_played"] = 1
    d["core"] = True
    d["habits"] = [{"substance": "alcohol", "when": "weekends", "odds": 0.5, "max_state": "tipsy"}]
    d["bits"] = [dict(d["bits"][0], id=f"b{i}", min_days_between=1.0) for i in range(2)]
    d["sample_lines"] = [{"text": "hello there", "approved": True}]
    d["relationships"] = [{"other": "Testra", "kind": "stance", "note": "x"},
                          {"other": "@human", "kind": "stance", "note": "respects the GM"},
                          {"other": "Nobody", "kind": "friend", "note": "x"}]
    d.update(edits)
    return json.dumps(d)


def _names_json(*names):
    return json.dumps({"names": list(names)})


def _member_char():
    return _member(201, "Testra")


def test_generate_forces_slot_fields():
    llm = FakeLLM(script_from(["a profile of a person", _persona_json()]))
    p = run(generate_persona(llm, "m", _slot(), _member_char(), "", GENERATION_RULES))
    assert p.big_five.extraversion.score <= 4 and p.game.years_played == 3 and p.habits == ()
    assert len(p.bits) <= 1 and all(b.min_days_between >= 7 for b in p.bits) and not p.core
    assert (p.guid, p.character_name) == (201, "Testra")
    assert llm.calls[0]["think"] is True and llm.calls[1]["format"] is not None
    assert p.identity.player_gender == "male" and 33 <= p.identity.age <= 40
    assert p.identity.region == "UK" and p.identity.accent == "en-gb"
    assert p.game.thinks_as == "lore" and p.play.guild_role == "raider"
    assert p.romance.stance == "none" and p.romance.jealousy_mode == "sulker"
    assert [r.other for r in p.relationships] == ["@human"]
    assert all(not s.approved for s in p.sample_lines)


def test_generate_drops_a_dial_the_model_writes():
    """A member's dial is the owner's to set (fix wave G4), never the model's."""
    slot = _slot(player_gender="female", introvert=False, jealousy_mode=None,
                 romance_stance="lust", age_band=(26, 32))
    d = json.loads(_persona_json())
    d["romance"]["dial"] = 10
    llm = FakeLLM(script_from(["profile", json.dumps(d)]))
    p = run(generate_persona(llm, "m", slot, _member_char(), "", GENERATION_RULES))
    assert p.romance.stance == "lust" and p.romance.dial is None


def test_generate_extravert_drinker_and_drawn_slot():
    slot = _slot(introvert=False, drinker=True, player_gender="female", jealousy_mode=None,
                 romance_stance="drawn", region="US", age_band=(21, 25))
    d = json.loads(_persona_json())
    d["big_five"]["extraversion"]["score"] = 2
    llm = FakeLLM(script_from(["profile", json.dumps(d)]))
    p = run(generate_persona(llm, "m", slot, _member_char(), "", GENERATION_RULES))
    assert p.big_five.extraversion.score >= 6 and p.identity.accent == "en-us"
    assert p.romance.stance == "drawn" and p.romance.style and p.romance.jealousy_mode is None
    assert 21 <= p.identity.age <= 25
    assert len(p.habits) == 1  # a drinker keeps what the model wrote


def test_generate_retries_name_collision_once():
    llm = FakeLLM(script_from(["profile", _persona_json("Pat Reeve"),
                               _persona_json("Dale Finch")]))
    p = run(generate_persona(llm, "m", _slot(), _member_char(), "", GENERATION_RULES,
                             taken_names=frozenset({"pat reeve", "Sam Carter"})))
    assert p.identity.real_name == "Dale Finch" and len(llm.calls) == 3
    assert "already taken" not in llm.calls[1]["messages"][-1]["content"]
    retry = llm.calls[2]["messages"][-1]["content"]
    assert "Sam" in retry and "Sam Carter" not in retry  # first names only


def test_generate_rejects_a_real_name_equal_to_a_character_name():
    llm = FakeLLM(script_from(["profile", _persona_json("Arkon"), _persona_json("Dale Finch")]))
    p = run(generate_persona(llm, "m", _slot(), _member_char(), "", GENERATION_RULES,
                             taken_names=frozenset({"Arkon"})))
    assert p.identity.real_name == "Dale Finch"


def test_generate_never_truncates_and_condenses_on_retry():
    long = _persona_json(secret="x " * 400)
    ok = _persona_json()
    llm = FakeLLM(script_from(["profile", long, long, ok]))
    p = run(generate_persona(llm, "m", _slot(), _member_char(), "", GENERATION_RULES))
    assert len(p.secret) <= 300 and len(llm.calls) == 4
    assert "condense" in llm.calls[3]["messages"][-1]["content"]
    llm = FakeLLM(script_from(["profile", long, long, long, long]))
    with pytest.raises(JSONJobFailed):
        run(generate_persona(llm, "m", _slot(), _member_char(), "", GENERATION_RULES))


def test_generate_refuses_a_minor_age_band():
    llm = FakeLLM(script_from(["p"]))
    with pytest.raises(ValueError):
        run(generate_persona(llm, "m", _slot(age_band=(15, 17)), _member_char(), "",
                             GENERATION_RULES))
    assert llm.calls == []


def test_default_rules_are_not_explicit():
    low = GENERATION_RULES.lower()
    for word in ("explicit sex", "nude", "porn", "rape"):
        assert word not in low


def _gen_store(store):
    seed_test_cast(store)
    store.upsert_cast([_member(HUMAN, "Testmage", 83, 0)]
                      + [_member(110 + i, f"Gen{i}") for i in range(4)]
                      + [_member(300, "Lowalt", level=20)], 1000.0)
    return SimpleNamespace(cast=CastCfg(0.6, 0.7), models=SimpleNamespace(reflection="m"),
                           human=SimpleNamespace(nickname="Testy", real_name="Pat"))


def test_generate_cast_is_resumable(store, tmp_path):
    cfg = _gen_store(store)
    llm = _cast_llm()
    r1 = run(generate_cast(store, llm, cfg, human_guid=HUMAN, data_dir=tmp_path, limit=2,
                           rng=random.Random(1)))
    assert len(r1.created) == 2 and r1.planned == 4 and r1.failed == {}
    first = {g: store.persona_versions()[g] for g in r1.created}
    r2 = run(generate_cast(store, llm, cfg, human_guid=HUMAN, data_dir=tmp_path,
                           rng=random.Random(1)))
    assert len(r2.created) == 2 and set(r2.created).isdisjoint(r1.created)
    assert all(store.persona_versions()[g] == v == 1 for g, v in first.items())
    assert 300 not in store.persona_versions() and HUMAN not in store.persona_versions()
    r3 = run(generate_cast(store, llm, cfg, human_guid=HUMAN, data_dir=tmp_path,
                           rng=random.Random(1)))
    assert r3.planned == 0 and r3.created == []
    names = [load_persona(store, g).identity.real_name for g in store.persona_versions()]
    assert len(names) == len({n.casefold() for n in names})  # taken names kept them unique
    gen = [load_persona(store, g) for g in r1.created + r2.created]
    assert all(not p.core for p in gen)
    assert len({p.identity.real_name.casefold() for p in gen}) == 4
    mix = mix_report([load_persona(store, g) for g in store.persona_versions()])
    assert mix.total == 9
    assert all(store.get_relationship(g, HUMAN).familiarity > 0 for g in r1.created + r2.created)


def test_generate_cast_dry_run_makes_no_calls_or_writes(store, tmp_path):
    cfg = _gen_store(store)
    llm = _cast_llm()
    before = dict(store.persona_versions())
    r = run(generate_cast(store, llm, cfg, human_guid=HUMAN, data_dir=tmp_path, dry_run=True,
                          rng=random.Random(1)))
    assert llm.calls == [] and store.persona_versions() == before
    assert r.planned == 4 and len(r.slots) == 4 and r.created == []


def test_generate_cast_records_failures_and_continues(store, tmp_path):
    cfg = _gen_store(store)
    llm = FakeLLM(lambda model, messages: "not json")
    r = run(generate_cast(store, llm, cfg, human_guid=HUMAN, data_dir=tmp_path, limit=2,
                          rng=random.Random(1)))
    assert r.created == [] and len(r.failed) == 2


def test_load_prompt_overlay(tmp_path):
    assert load_prompt("persona_rules", tmp_path, "DEFAULT") == "DEFAULT"
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "persona_rules.txt").write_text("PRIVATE WORDING\n", encoding="utf-8")
    assert load_prompt("persona_rules", tmp_path, "DEFAULT") == "PRIVATE WORDING"
    (tmp_path / "prompts" / "empty.txt").write_text("  \n", encoding="utf-8")
    assert load_prompt("empty", tmp_path, "DEFAULT") == "DEFAULT"


def test_load_prompt_rejects_path_tricks(tmp_path):
    with pytest.raises(ValueError):
        load_prompt("../mind", tmp_path, "d")


def test_mix_cli_makes_no_model_calls(tmp_path, capsys):
    from leeroy_mind.__main__ import main
    from leeroy_mind.clock import SystemClock
    from leeroy_mind.storage.db import open_db
    from leeroy_mind.storage.store import Store
    s = Store(open_db(tmp_path / "mind.db"), clock=SystemClock())
    s.upsert_cast([_member(5, "Aaa")], 1.0)
    save_persona(s, make_persona(5, "Aaa"), source="seed", ts=1.0, race="Orc",
                 class_name="Warrior")
    s.close()
    assert main(["personas", "--data-dir", str(tmp_path), "mix"]) == 0
    out = capsys.readouterr().out
    assert "female_share" in out and "introvert_share" in out and "total 1" in out
    assert "tank" in out and "healer" in out and "damage" in out


def test_generate_dry_run_cli_prints_plan_and_needs_no_ollama(tmp_path, capsys):
    from leeroy_mind.__main__ import main
    from leeroy_mind.clock import SystemClock
    from leeroy_mind.storage.db import open_db
    from leeroy_mind.storage.store import Store
    s = Store(open_db(tmp_path / "mind.db"), clock=SystemClock())
    s.upsert_cast([_member(HUMAN, "Testmage", 83, 0), _member(5, "Aaa"), _member(6, "Bbb")], 1.0)
    save_persona(s, make_persona(5, "Aaa"), source="seed", ts=1.0, race="Orc",
                 class_name="Warrior")
    s.close()
    assert main(["personas", "--data-dir", str(tmp_path), "generate", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "Bbb:" in out and "projected: total 2" in out and "planned 1" in out


def _cast_llm():
    """Every record starts with a taken name (a seed's) and, once told, a fresh first name."""
    firsts = itertools.cycle(["Alba", "Bram", "Cora", "Dirk", "Edda", "Finn", "Gwen", "Hugo",
                              "Iona", "Joss", "Kira", "Lars"])

    def script(model, messages):
        last = messages[-1]["content"]
        if "JSON object" not in last:
            return "profile"
        return _persona_json("Sam Carter" if "already taken" not in last
                             else f"{next(firsts)} Ward")
    return FakeLLM(script)


def test_prompts_carry_called_and_describe_but_not_the_real_name():
    llm = FakeLLM(script_from(["profile", _persona_json()]))
    run(generate_persona(llm, "m", _slot(romance_stance="drawn", player_gender="female",
                                         jealousy_mode=None),
                         _member_char(), "- Aa (Sam, 30)", GENERATION_RULES, human=H))
    for call in llm.calls:
        text = "\n".join(m["content"] for m in call["messages"])
        assert H.describe(include_real_name=False) in text and "Testy" in text
        assert "real name" not in text and "Pat" not in text
    first = "\n".join(m["content"] for m in llm.calls[0]["messages"])
    assert "Aa" in first and "Testra" in first
    low = GENERATION_RULES.lower()
    assert "18" in low and "guild master" in low and "never between" in low


def test_generate_rejects_a_record_containing_the_real_name():
    leaky = _persona_json(secret="once covered a shift for Pat at work")
    llm = FakeLLM(script_from(["profile", leaky, _persona_json("Dale Finch")]))
    p = run(generate_persona(llm, "m", _slot(), _member_char(), "", GENERATION_RULES, human=H))
    assert "Pat" not in p.secret and len(llm.calls) == 3
    assert "real name" in llm.calls[2]["messages"][-1]["content"]  # told why it was refused


def test_second_collision_with_no_free_candidate_is_a_failure(caplog):
    llm = FakeLLM(script_from(["profile", _persona_json("Pat Reeve"), _persona_json("Pat Reeve"),
                               _names_json("Pat Reeve", "pat moss", "Sam Carter")]))
    notes = GenNotes()
    with caplog.at_level(logging.WARNING), pytest.raises(NameCollision):
        run(generate_persona(llm, "m", _slot(), _member_char(), "", GENERATION_RULES,
                             taken_names=frozenset({"Pat Reeve", "Sam Carter"}), notes=notes))
    assert "name collision" in caplog.text and notes.collisions == ["Pat Reeve"]
    assert notes.renames == [] and len(llm.calls) == 4  # the rename was asked once


def _self_naming_json():
    """A record that uses its own name in its free text, the way a model does."""
    d = json.loads(_persona_json("Sam Reed"))
    d["secret"] = "Sam Reed once covered a shift and Sam never lived it down"
    d["real_life"]["life_thread"] = "Sam's sister moved to Leeds"
    d["opinions"] = ["Samuel and MacSam are wrong about gear", "pulls should be planned",
                    "tea beats coffee"]
    d["sample_lines"] = [{"text": "its Sam Reed from the depot", "approved": True}]
    d["relationships"] = [{"other": "Sam", "kind": "friend", "note": "raids together"}]
    return json.dumps(d)


def test_a_second_collision_is_renamed_to_the_first_free_candidate(caplog):
    llm = FakeLLM(script_from(["profile", _persona_json("Sam Young"), _self_naming_json(),
                               _names_json("Sam Webb", "sam carter", "Dale Finch", "Iona Park")]))
    notes = GenNotes()
    with caplog.at_level(logging.WARNING):
        p = run(generate_persona(llm, "m", _slot(), _member_char(), "", GENERATION_RULES,
                                 taken_names=frozenset({"Sam Carter"}),
                                 member_names=frozenset({"Sam", "Testra"}), notes=notes))
    assert p.identity.real_name == "Dale Finch" and len(llm.calls) == 4
    # the old full name becomes the new full name and the old first name the new first name
    assert p.secret == "Dale Finch once covered a shift and Dale never lived it down"
    assert p.real_life.life_thread == "Dale's sister moved to Leeds"
    assert [s.text for s in p.sample_lines] == ["its Dale Finch from the depot"]
    assert p.opinions[0] == "Samuel and MacSam are wrong about gear"  # whole words only
    # a name field is not free text: the relationship still points at the member
    assert [r.other for r in p.relationships] == ["Sam"] and p.character_name == "Testra"
    assert notes.collisions == ["Sam Young"] and notes.renames == [("Sam Reed", "Dale Finch")]
    assert "Sam Reed -> Dale Finch" in caplog.text
    ask = llm.calls[3]
    assert ask["think"] is False and ask["format"]["required"] == ["names"]
    assert ask["options"]["num_ctx"] == llm.calls[1]["options"]["num_ctx"]  # STRUCTURE: no reload
    text = ask["messages"][-1]["content"]
    assert "Player: male, aged 34, from the UK" in text and "warehouse supervisor" in text
    assert "Sam" in text and "Sam Carter" not in text and "Sam Reed" not in text  # first names


def test_a_rename_candidate_that_overflows_a_field_is_skipped():
    d = json.loads(_persona_json("Sam Reed"))
    d["secret"] = " ".join(["Sam"] * 60)  # 239 characters; a longer first name breaks the 300 limit
    llm = FakeLLM(script_from(["profile", _persona_json("Sam Young"), json.dumps(d),
                               _names_json("Bartholomew Finch", "Al Finch")]))
    p = run(generate_persona(llm, "m", _slot(), _member_char(), "", GENERATION_RULES,
                             taken_names=frozenset({"Sam Carter"})))
    assert p.identity.real_name == "Al Finch" and p.secret == " ".join(["Al"] * 60)


def test_a_rename_candidate_the_screens_refuse_is_skipped():
    llm = FakeLLM(script_from(["profile", _persona_json("Sam Young"), _persona_json("Sam Reed"),
                               _names_json("Pat Holloway", "Dale Finch")]))
    # the owner's name is not among the taken names, so only the screens can refuse it
    p = run(generate_persona(llm, "m", _slot(), _member_char(), "", GENERATION_RULES, human=H,
                             taken_names=frozenset({"Sam Carter"})))
    assert p.identity.real_name == "Dale Finch"


def test_the_owners_name_as_a_rename_candidate_is_skipped_and_never_sent(caplog):
    taken = frozenset({"Sam Carter", "Testy", "Pat", "Testmage"})  # as generate_cast builds it
    llm = FakeLLM(script_from(["profile", _persona_json("Sam Young"), _persona_json("Sam Reed"),
                               _names_json("Pat", "Pat Holloway", "PAT", "Dale Finch")]))
    with caplog.at_level(logging.INFO):
        p = run(generate_persona(llm, "m", _slot(), _member_char(), "", GENERATION_RULES, human=H,
                                 taken_names=taken))
    assert p.identity.real_name == "Dale Finch" and len(llm.calls) == 4
    owner = re.compile(r"(?i)\bpat\b")
    for c in llm.calls:
        assert not owner.search("\n".join(m["content"] for m in c["messages"]))
    assert "renamed" in caplog.text and not owner.search(caplog.text)


def test_a_malformed_rename_reply_is_asked_again():
    llm = FakeLLM(script_from(["profile", _persona_json("Sam Young"), _persona_json("Sam Reed"),
                               json.dumps({"names": [" ", 7]}), _names_json("Dale Finch")]))
    p = run(generate_persona(llm, "m", _slot(), _member_char(), "", GENERATION_RULES,
                             taken_names=frozenset({"Sam Carter"})))
    assert p.identity.real_name == "Dale Finch" and len(llm.calls) == 5
    assert "give at least one real name" in llm.calls[4]["messages"][-1]["content"]


def test_an_llm_error_on_the_rename_surfaces_as_itself():
    from leeroy_mind.llm.client import LLMError
    llm = FakeLLM(script_from(["profile", _persona_json("Sam Young"), _persona_json("Sam Reed"),
                               _names_json("Dale Finch")]))
    orig = llm.chat
    n = {"i": 0}

    async def chat(*a, **k):
        n["i"] += 1
        if n["i"] == 4:
            raise LLMError("down")
        return await orig(*a, **k)
    llm.chat = chat
    with pytest.raises(LLMError):
        run(generate_persona(llm, "m", _slot(), _member_char(), "", GENERATION_RULES,
                             taken_names=frozenset({"Sam Carter"})))


def test_failed_name_retry_surfaces_its_own_error():
    llm = FakeLLM(script_from(["profile", _persona_json("Pat Reeve"), "not json", "still not", "x", "y"]))
    with pytest.raises(JSONJobFailed):
        run(generate_persona(llm, "m", _slot(), _member_char(), "", GENERATION_RULES,
                             taken_names=frozenset({"Pat Reeve"})))


def test_first_name_collision_is_caught():
    llm = FakeLLM(script_from(["profile", _persona_json("Sam Young"), _persona_json("Dale Finch")]))
    p = run(generate_persona(llm, "m", _slot(), _member_char(), "", GENERATION_RULES,
                             taken_names=frozenset({"Sam Carter"})))
    assert p.identity.real_name == "Dale Finch"
    assert "first names" in llm.calls[2]["messages"][-1]["content"]


@pytest.mark.parametrize("clash", ["Testy Jones", "Testmage Jones"])
def test_collision_with_the_humans_names_counts(clash):
    llm = FakeLLM(script_from(["profile", _persona_json(clash), _persona_json("Dale Finch")]))
    p = run(generate_persona(llm, "m", _slot(), _member_char(), "", GENERATION_RULES, human=H,
                             taken_names=frozenset({"Testy", "Pat", "Testmage"})))
    assert p.identity.real_name == "Dale Finch"


def test_relationship_targets_use_character_names_and_the_human_aliases():
    rels = [{"other": "Arkon", "kind": "friend", "note": "raids together"},
            {"other": "Testy", "kind": "stance", "note": "respects the GM"},
            {"other": "Sam Carter", "kind": "friend", "note": "a real name, not a character"},
            {"other": "Stranger", "kind": "friend", "note": "unknown"}]
    llm = FakeLLM(script_from(["profile", _persona_json(relationships=rels)]))
    notes = GenNotes()
    p = run(generate_persona(llm, "m", _slot(), _member_char(), "", GENERATION_RULES, human=H,
                             taken_names=frozenset({"Arkon", "Sam Carter"}),
                             member_names=frozenset({"Arkon", "Testra"}), notes=notes))
    assert [r.other for r in p.relationships] == ["Arkon", "@human"]
    assert notes.dropped == ["Sam Carter", "Stranger"]
    assert "character name" in llm.calls[1]["messages"][-1]["content"]


def _gen_with(secret=None, rels=None, wants=None, **kw):
    d = json.loads(_persona_json())
    if secret is not None:
        d["secret"] = secret
    if rels is not None:
        d["relationships"] = rels
    if wants is not None:
        d["game"]["wants"] = wants
    llm = FakeLLM(script_from(["profile", json.dumps(d), _persona_json("Dale Finch")]))
    p = run(generate_persona(llm, "m", _slot(), _member_char(), "", GENERATION_RULES, human=H,
                             member_names=frozenset({"Arkon"}), **kw))
    return p, llm


def test_romance_between_members_is_refused_and_asked_again():
    for kw in ({"secret": "has a crush on Arkon"},
               {"wants": ["to kiss Arkon at the raid party"]},
               {"rels": [{"other": "Arkon", "kind": "friend", "note": "secretly dating"}]}):
        p, llm = _gen_with(**kw)
        assert len(llm.calls) == 3 and p.identity.real_name == "Dale Finch"
        assert "romance" in llm.calls[2]["messages"][-1]["content"]


def test_rivalry_over_the_human_naming_a_member_is_accepted():
    p, llm = _gen_with(secret="jealous when Arkon flirts with Testy")
    assert len(llm.calls) == 2 and "Arkon flirts" in p.secret
    p, llm = _gen_with(rels=[{"other": "Arkon", "kind": "friction",
                              "note": "always flirting with Testy, it's annoying"}])
    assert len(llm.calls) == 2 and p.relationships[0].other == "Arkon"
    p, llm = _gen_with(secret="jealous when Arkon flirts with the GM")
    assert len(llm.calls) == 2 and "the GM" in p.secret


def test_romance_at_a_member_is_refused_even_when_the_human_is_named():
    for kw in ({"secret": "flirts with Arkon to make Testy jealous"},
               {"wants": ["to kiss Arkon in front of the guild master"]}):
        p, llm = _gen_with(**kw)
        assert len(llm.calls) == 3 and p.identity.real_name == "Dale Finch"
        assert "romance" in llm.calls[2]["messages"][-1]["content"]


def test_romance_toward_the_human_is_allowed():
    p, llm = _gen_with(secret="has a crush on Testy and would never say so",
                       rels=[{"other": "@human", "kind": "stance", "note": "flirts with the GM"}])
    assert len(llm.calls) == 2 and "crush" in p.secret
    p, llm = _gen_with(rels=[{"other": "Testy", "kind": "stance", "note": "in love with him"}])
    assert len(llm.calls) == 2 and p.relationships[0].other == "@human"


def test_drinker_slot_needs_a_drinking_habit():
    d = json.loads(_persona_json())
    d["habits"] = []
    llm = FakeLLM(script_from(["profile", json.dumps(d), _persona_json()]))
    p = run(generate_persona(llm, "m", _slot(drinker=True), _member_char(), "",
                             GENERATION_RULES))
    assert len(llm.calls) == 3 and p.habits[0].substance == "alcohol"
    assert "alcohol or smoking" in llm.calls[1]["messages"][-1]["content"]


def test_an_empty_profile_is_asked_for_again_once(caplog):
    llm = FakeLLM(script_from(["  ", "a profile of a person", _persona_json()]))
    with caplog.at_level(logging.WARNING):
        p = run(generate_persona(llm, "m", _slot(), _member_char(), "", GENERATION_RULES))
    assert p.identity.real_name == "Rae Holt" and len(llm.calls) == 3
    assert "profile_empty_retry" in caplog.text
    first, again = llm.calls[:2]
    assert again["messages"] == first["messages"] and first["think"] is True is again["think"]
    assert "a profile of a person" in llm.calls[2]["messages"][-1]["content"]


def test_an_empty_profile_twice_is_rejected():
    llm = FakeLLM(script_from(["   ", ""]))
    with pytest.raises(ValueError, match="the profile pass returned nothing"):
        run(generate_persona(llm, "m", _slot(), _member_char(), "", GENERATION_RULES))
    assert len(llm.calls) == 2  # asked once more, then given up on


def test_cast_summary_is_capped_to_the_newest_lines():
    ps = [_seed(i, f"Name{i:03d}", True, True) for i in range(1, 200)]
    lines = cast_summary(ps, max_tokens=200).splitlines()
    assert 0 < len(lines) < 40 and lines[-1].startswith("- Name199")


def test_generate_cast_prompts_and_overlay_cap(store, tmp_path):
    cfg = _gen_store(store)
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "persona_rules.txt").write_text(
        "OVERLAY-MARKER " + "filler " * 5000, encoding="utf-8")
    llm = _cast_llm()
    run(generate_cast(store, llm, cfg, human_guid=HUMAN, data_dir=tmp_path, limit=1,
                      rng=random.Random(1)))
    first = "\n".join(m["content"] for m in llm.calls[0]["messages"])
    assert "OVERLAY-MARKER" in first and len(first) < 12000
    assert "Testy (character Testmage), the guild master" in first and "real name" not in first


def test_generate_cast_blocks_the_humans_own_real_name_and_saves_the_retry(store, tmp_path):
    cfg = _gen_store(store)

    def script(model, messages):
        last = messages[-1]["content"]
        if "JSON object" not in last:
            return "profile"
        return _persona_json("Testy Jones" if "already taken" not in last else "Gen Person")
    r = run(generate_cast(store, FakeLLM(script), cfg, human_guid=HUMAN, data_dir=tmp_path,
                          limit=1, rng=random.Random(1)))
    assert r.collisions == ["Testy Jones"] and len(r.created) == 1
    assert load_persona(store, r.created[0]).identity.real_name == "Gen Person"


def test_twice_colliding_slot_fails_unsaved_and_a_rerun_retries_it(store, tmp_path):
    cfg = _gen_store(store)
    before = set(store.persona_versions())

    def script(model, messages):
        last = messages[-1]["content"]
        if '"names"' in last:  # the rename: only taken names on offer
            return _names_json("Sam Carter", "Sam Young")
        return "profile" if "JSON object" not in last else _persona_json("Sam Carter")
    always = FakeLLM(script)
    r = run(generate_cast(store, always, cfg, human_guid=HUMAN, data_dir=tmp_path, limit=1,
                          rng=random.Random(1)))
    assert r.created == [] and len(r.failed) == 1 and len(r.collisions) == 1
    assert next(iter(r.failed.values())).startswith("NameCollision")
    assert set(store.persona_versions()) == before
    r2 = run(generate_cast(store, _cast_llm(), cfg, human_guid=HUMAN, data_dir=tmp_path,
                           limit=1, rng=random.Random(1)))
    assert len(r2.created) == 1


def test_generate_cast_saves_renamed_personas_and_keeps_their_names_unique(store, tmp_path):
    cfg = _gen_store(store)
    cfg.human = SimpleNamespace(nickname="Testy", real_name="Zoltan")
    offered = ["Zoltan Ward", "Alba Ward", "Bram Ward", "Cora Ward", "Dirk Ward", "Edda Ward"]

    def script(model, messages):
        last = messages[-1]["content"]
        if '"names"' in last:  # every record takes a seed's name, every rename gets the same list
            return _names_json(*offered)
        return "profile" if "JSON object" not in last else _persona_json("Sam Carter")
    llm = FakeLLM(script)
    r = run(generate_cast(store, llm, cfg, human_guid=HUMAN, data_dir=tmp_path,
                          rng=random.Random(1)))
    assert r.failed == {} and len(r.created) == 4 and len(r.collisions) == 4
    # never the owner's name, and each new name is taken for the slots after it
    assert [load_persona(store, g).identity.real_name for g in r.created] == offered[1:5]
    assert not any("Zoltan" in "\n".join(m["content"] for m in c["messages"]) for c in llm.calls)


def test_failure_in_one_slot_does_not_abort_the_run(store, tmp_path, monkeypatch):
    from leeroy_mind.personas import generate as G
    cfg = _gen_store(store)
    real = G.seed_relationships
    calls = []

    def boom(*a, **k):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("db hiccup")
        return real(*a, **k)
    monkeypatch.setattr(G, "seed_relationships", boom)
    r = run(generate_cast(store, _cast_llm(), cfg, human_guid=HUMAN, data_dir=tmp_path,
                          rng=random.Random(1)))
    assert len(r.failed) == 1 and len(r.created) == 3
    assert all(g not in store.persona_versions() for g in r.failed)  # rolled back


def test_mix_counts_cast_personas_only(tmp_path, capsys):
    from leeroy_mind.__main__ import main
    from leeroy_mind.clock import SystemClock
    from leeroy_mind.storage.db import open_db
    from leeroy_mind.storage.store import Store
    s = Store(open_db(tmp_path / "mind.db"), clock=SystemClock())
    s.upsert_cast([_member(5, "Aaa"), _member(6, "Low", level=20)], 1.0)
    for g, n in ((5, "Aaa"), (6, "Low")):
        save_persona(s, make_persona(g, n), source="seed", ts=1.0, race="Orc",
                     class_name="Warrior")
    s.close()
    assert main(["personas", "--data-dir", str(tmp_path), "mix"]) == 0
    assert "total 1" in capsys.readouterr().out


def test_leak_re_ask_names_the_field_not_the_name():
    leaky = _persona_json(secret="once covered a shift for Pat at work")
    llm = FakeLLM(script_from(["profile", leaky, _persona_json("Dale Finch")]))
    run(generate_persona(llm, "m", _slot(), _member_char(), "", GENERATION_RULES, human=H))
    ask = llm.calls[2]["messages"][-1]["content"]
    assert "field secret" in ask and "Pat" not in ask.replace("Pat Reeve", "")


def test_condense_attempt_carries_the_specific_error():
    leaky = _persona_json(secret="told Pat so")
    llm = FakeLLM(script_from(["profile", leaky, leaky, _persona_json("Dale Finch")]))
    run(generate_persona(llm, "m", _slot(), _member_char(), "", GENERATION_RULES, human=H))
    assert "field secret" in llm.calls[3]["messages"][-1]["content"]


def test_the_real_name_is_never_in_any_prompt_even_on_a_forced_retry(store, tmp_path):
    cfg = _gen_store(store)
    cfg.human = SimpleNamespace(nickname="Testy", real_name="Zoltan")
    llm = _cast_llm()
    r = run(generate_cast(store, llm, cfg, human_guid=HUMAN, data_dir=tmp_path, limit=1,
                          rng=random.Random(1)))
    assert r.collisions == ["Sam Carter"] and len(r.created) == 1  # a retry did happen
    assert any("already taken" in c["messages"][-1]["content"] for c in llm.calls)
    for c in llm.calls:
        assert "Zoltan" not in "\n".join(m["content"] for m in c["messages"])


def test_a_real_name_in_the_name_field_is_refused_not_a_collision():
    llm = FakeLLM(script_from(["profile", _persona_json("Pat Jones"), _persona_json("Dale Finch")]))
    p = run(generate_persona(llm, "m", _slot(), _member_char(), "", GENERATION_RULES, human=H))
    assert p.identity.real_name == "Dale Finch" and len(llm.calls) == 3


def test_an_llm_error_on_the_retry_surfaces_as_itself():
    from leeroy_mind.llm.client import LLMError
    llm = FakeLLM(script_from(["profile", _persona_json("Sam Young"), _persona_json("Dale Finch")]))
    orig = llm.chat
    n = {"i": 0}

    async def chat(*a, **k):
        n["i"] += 1
        if n["i"] == 3:
            raise LLMError("down")
        return await orig(*a, **k)
    llm.chat = chat
    with pytest.raises(LLMError):
        run(generate_persona(llm, "m", _slot(), _member_char(), "", GENERATION_RULES,
                             taken_names=frozenset({"Sam Carter"})))


def test_retry_name_list_is_first_names_deduplicated_and_capped(caplog):
    from leeroy_mind.personas.generate import _printable_first_names
    taken = {f"Name{i:04d} Surname" for i in range(2000)} | {"Sam Carter", "Sam Young", "sam"}
    with caplog.at_level(logging.INFO):
        out = _printable_first_names(taken, HumanIdentity(1, "T", "", "Name0001", True))
    assert "Name0001" not in out and len(out) < 200 and "cut to" in caplog.text


def test_retry_name_list_deduplicates_first_names():
    from leeroy_mind.personas.generate import _printable_first_names
    out = _printable_first_names({"Sam Carter", "Sam Young", "sam", "Arkon"},
                                 HumanIdentity(1, "T", "", "", True))
    assert sorted(n.casefold() for n in out) == ["arkon", "sam"]


def test_a_members_first_name_is_watched_by_the_romance_screen():
    d = json.loads(_persona_json(secret="has a crush on Sam"))
    llm = FakeLLM(script_from(["profile", json.dumps(d), _persona_json("Dale Finch")]))
    p = run(generate_persona(llm, "m", _slot(), _member_char(), "", GENERATION_RULES,
                             taken_names=frozenset({"Sam Carter"})))
    assert len(llm.calls) == 3 and "crush" not in p.secret


def test_a_lowercase_configured_name_is_still_screened():
    h = HumanIdentity(HUMAN, "Testmage", "Testy", "bill", True)
    llm = FakeLLM(script_from(["profile", _persona_json(secret="told Bill so"),
                               _persona_json(secret="split the bill")]))
    p = run(generate_persona(llm, "m", _slot(), _member_char(), "", GENERATION_RULES, human=h))
    assert p.secret == "split the bill"


def test_cast_summary_skips_lines_with_the_real_name():
    ps = [_seed(1, "Aa", True, True), _seed(2, "Bb", False, False)]
    h = HumanIdentity(1, "T", "", "Jo", True)  # seed 1 is "Jo Mercer"
    out = cast_summary(ps, human=h)
    assert "Aa" not in out and "Jo" not in out and "Bb" in out


def test_retry_names_list_persona_first_names_before_character_names():
    from leeroy_mind.personas.generate import _printable_first_names
    out = _printable_first_names({"Zed Smith", "Arkon", "Belra"}, HumanIdentity(1, "T", "", "", True),
                                 frozenset({"Arkon", "Belra"}))
    assert out == ["Zed", "Arkon", "Belra"]


def test_dropped_targets_and_collisions_are_redacted(store, tmp_path):
    from leeroy_mind.personas.generate import _redact
    h = HumanIdentity(1, "T", "Testy", "Zoltan", True)
    assert _redact(["Zoltan", "Arkon"], h) == ["<redacted>", "Arkon"]


def test_generation_prompts_carry_the_humans_pronouns():
    h = HumanIdentity(HUMAN, "Testmage", "Testy", "", True, "he")
    llm = FakeLLM(script_from(["profile", _persona_json()]))
    run(generate_persona(llm, "m", _slot(combat_role="tank"), _member(201, "Testra", class_=11),
                         "", GENERATION_RULES, human=h))
    for call in llm.calls:
        assert "Testy (character Testmage), the guild master, he/him" \
            in call["messages"][-1]["content"]


# -- the human's relationships (controller ruling "final I2") ---------------------------------

def test_relationship_targets_resolve_gm_and_any_case_to_the_roster():
    rels = [{"other": "GM", "kind": "friend", "note": "old raid buddy"},
            {"other": "arkon", "kind": "friend", "note": "raids together"},
            {"other": "the guild master", "kind": "stance", "note": "rates his calls"},
            {"other": "testra", "kind": "friend", "note": "that is the persona itself"}]
    llm = FakeLLM(script_from(["profile", _persona_json(relationships=rels)]))
    p = run(generate_persona(llm, "m", _slot(), _member_char(), "", GENERATION_RULES, human=H,
                             member_names=frozenset({"Arkon", "Testra"})))
    assert [r.other for r in p.relationships] == ["@human", "Arkon", "@human"]


def test_generate_cast_seeds_a_declared_friendship_with_the_human_as_close(store, tmp_path):
    from leeroy_mind.talk.prompt import is_close
    cfg = _gen_store(store)
    rels = [{"other": "Testy", "kind": "friend", "note": "has had his back for years"}]

    def script(model, messages):
        if "JSON object" not in messages[-1]["content"]:
            return "profile"
        return _persona_json("Gen Person", relationships=rels)
    r = run(generate_cast(store, FakeLLM(script), cfg, human_guid=HUMAN, data_dir=tmp_path,
                          limit=1, rng=random.Random(1)))
    row = store.get_relationship(r.created[0], HUMAN)
    assert (row.label, row.notes, row.affinity) == ("friend", "has had his back for years", 40)
    assert is_close(row)


# -- typing: full form -> chat form (rehearsal 1) ----------------------------------------------

def test_a_backwards_typing_map_is_turned_round_without_asking_again():
    from leeroy_mind.personas.schema import TYPING_RULE
    d = json.loads(_persona_json())
    d["typing"]["abbreviations"] = {"lol": "laughing out loud", "brb": "sometimes"}
    d["typing"]["emoticons"] = [":)", chr(0x2615)]
    llm = FakeLLM(script_from(["profile", json.dumps(d)]))
    p = run(generate_persona(llm, "m", _slot(), _member_char(), "", GENERATION_RULES))
    assert p.typing.abbreviations == {"laughing out loud": "lol"}
    assert p.typing.emoticons == (":)",) and len(llm.calls) == 2
    assert TYPING_RULE in llm.calls[1]["messages"][-1]["content"]


# -- the rename path (side-generate-fix-review.md) -----------------------------------------------

H2 = HumanIdentity(HUMAN, "Testmage", "Testy", "Pat Holloway", True)  # a two-word real name
TAKEN2 = frozenset({"Wim Tolley", "Testy", "Pat Holloway", "Testmage"})
OWNER2 = re.compile(r"(?i)\bpat\b|holloway")


def test_a_name_sharing_a_word_with_a_two_word_real_name_is_never_printed(caplog):
    llm = FakeLLM(script_from(["profile", _persona_json("Pat Moss"), _persona_json("Pat Moss"),
                               _names_json("Odo Pike")]))
    notes = GenNotes()
    with caplog.at_level(logging.INFO):
        p = run(generate_persona(llm, "m", _slot(), _member_char(), "", GENERATION_RULES,
                                 human=H2, taken_names=TAKEN2, notes=notes))
    assert p.identity.real_name == "Odo Pike" and "renamed Testra" in caplog.text
    assert not OWNER2.search(caplog.text)
    for c in llm.calls:
        assert not OWNER2.search("\n".join(m["content"] for m in c["messages"]))
    assert notes.renames == [("Pat Moss", "Odo Pike")]  # kept raw, like notes.collisions


def test_a_two_word_real_name_is_masked_when_no_candidate_fits(caplog):
    from leeroy_mind.personas.generate import _redact
    llm = FakeLLM(script_from(["profile", _persona_json("Pat Moss"), _persona_json("Pat Moss"),
                               _names_json("Pat Reeve", "Wim Tolley")]))
    with caplog.at_level(logging.INFO), pytest.raises(NameCollision) as e:
        run(generate_persona(llm, "m", _slot(), _member_char(), "", GENERATION_RULES, human=H2,
                             taken_names=TAKEN2))
    assert "name collision" in caplog.text and not OWNER2.search(caplog.text)
    assert "collides with a taken name" in str(e.value) and not OWNER2.search(str(e.value))
    assert _redact(["Pat Moss", "Holloway", "Ann Moss"], H2) == ["<redacted>", "<redacted>",
                                                                  "Ann Moss"]


def test_a_note_about_a_member_who_shares_the_old_first_name_is_left_alone():
    d = json.loads(_persona_json("Wim Morrow"))
    d["relationships"] = [{"other": "wim", "kind": "friend", "note": "Wim keeps stealing my loot"},
                          {"other": "Arkon", "kind": "friend", "note": "Wim owes Arkon a flask"}]
    llm = FakeLLM(script_from(["profile", _persona_json("Wim Hask"), json.dumps(d),
                               _names_json("Odo Pike")]))
    p = run(generate_persona(llm, "m", _slot(), _member_char(), "", GENERATION_RULES,
                             taken_names=frozenset({"Wim Tolley"}),
                             member_names=frozenset({"Wim", "Arkon", "Testra"})))
    assert [(r.other, r.note) for r in p.relationships] == [
        ("Wim", "Wim keeps stealing my loot"), ("Arkon", "Odo owes Arkon a flask")]


def test_the_character_name_survives_a_rename_of_the_same_first_name():
    d = json.loads(_persona_json("Nesta Vane"))
    d["secret"] = "Nesta Vane once covered a shift"
    llm = FakeLLM(script_from(["profile", json.dumps(d), json.dumps(d),
                               _names_json("Odo Pike")]))
    p = run(generate_persona(llm, "m", _slot(), _member(201, "Nesta"), "", GENERATION_RULES,
                             taken_names=frozenset({"Nesta"})))
    assert (p.character_name, p.identity.real_name) == ("Nesta", "Odo Pike")
    assert p.secret == "Odo Pike once covered a shift"


def test_a_leftover_of_the_old_name_in_another_case_is_logged_without_it(caplog):
    d = json.loads(_persona_json("Wim Morrow"))
    d["sample_lines"] = [{"text": "wim here, pulling in 10", "approved": False},
                         {"text": "its Wim from the depot", "approved": False}]
    llm = FakeLLM(script_from(["profile", _persona_json("Wim Hask"), json.dumps(d),
                               _names_json("Odo Pike")]))
    with caplog.at_level(logging.INFO, logger="leeroy_mind.generate"):
        p = run(generate_persona(llm, "m", _slot(), _member_char(), "", GENERATION_RULES,
                                 taken_names=frozenset({"Wim Tolley"})))
    assert [s.text for s in p.sample_lines] == ["wim here, pulling in 10",
                                                "its Odo from the depot"]
    left = [r.getMessage() for r in caplog.records if "another case" in r.getMessage()]
    assert left == ["renamed Testra: the old first name is still in 1 field, in another case"]


def test_rename_candidates_must_look_like_real_names():
    llm = FakeLLM(script_from(["profile", _persona_json("Wim Hask"), _persona_json("Wim Morrow"),
                               _names_json("Dr Finch", "Mr. Smith", "R2 Dee", "Ann Marie Lee Jones",
                                           "Mrs Pike", "Jo-Ann O'Neil")]))
    p = run(generate_persona(llm, "m", _slot(), _member_char(), "", GENERATION_RULES,
                             taken_names=frozenset({"Wim Tolley"})))
    assert p.identity.real_name == "Jo-Ann O'Neil"


def test_the_cast_summary_leaves_out_a_name_sharing_a_word_with_the_real_name():
    d = _base_persona_dict(1, "Aa")
    d["identity"]["real_name"] = "Pat Moss"
    out = cast_summary([persona_from_dict(d), _seed(2, "Bb", False, False)], human=H2)
    assert "Aa" not in out and not OWNER2.search(out) and "Bb" in out


def test_a_lower_case_member_name_next_to_a_romance_word_is_refused():
    p, llm = _gen_with(secret="has a crush on arkon")
    assert len(llm.calls) == 3 and "crush" not in p.secret
    p, llm = _gen_with(rels=[{"other": "Arkon", "kind": "friend",
                              "note": "laughs when belra flirts with Testy"}])
    assert len(llm.calls) == 2  # rivalry over him stays
