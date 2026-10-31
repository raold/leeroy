import dataclasses
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import _base_persona_dict, run
from leeroy_mind.human import HumanIdentity
from leeroy_mind.llm.presets import JSONJobFailed
from leeroy_mind.personas.io import load_persona
from leeroy_mind.personas.roster import parse_roster_tsv
from leeroy_mind.personas.schema import persona_from_dict, persona_to_dict
from leeroy_mind.personas import seed as seed_mod
from leeroy_mind.personas.seed import (_section_lines, apply_renames, assign_characters,
                                       convert_sketch, parse_sketches, parse_sketches_checked,
                                       seed_cast, seed_relationships, sketch_context)
from leeroy_mind.__main__ import main
from leeroy_mind.testing.fake_llm import FakeLLM
from leeroy_mind.wow import CLASSES, class_roles

FIX = Path(__file__).parent / "fixtures"
SAMPLE = (FIX / "sketches_sample.md").read_text(encoding="utf-8")
ROSTER = parse_roster_tsv((FIX / "roster.tsv").read_text(encoding="utf-8"))
HUMAN = 1
BRAINSTORM = Path(r"F:\azerothcore\private\2026-10-29-living-guild-brainstorm.md")
CFG = SimpleNamespace(models=SimpleNamespace(reflection="m"),
                      human=SimpleNamespace(nickname="", real_name=""))
TM = HumanIdentity(1, "Testmage", "", "", True)
by_name = {m.name: m for m in ROSTER}


def _llm(age=50, extra=None):
    def script(model, messages):
        d = _base_persona_dict(1, "Zed")
        d["identity"]["age"] = age
        d["opinions"] = ["Testra should lead more", "patch notes are worth reading", "tea wins"]
        d["play"]["guild_role"] = "social"
        if extra:
            extra(d)
        return json.dumps(d)
    return FakeLLM(script)


def test_parse_sketch_heading():
    s = parse_sketches(SAMPLE)[0]
    assert (s.name, s.real_name, s.age, s.race, s.spec, s.class_name, s.gender, s.region,
            s.group) == ("Testra", "Ann", 29, "Orc", "Arms", "Warrior", "female", "US", "Officers")
    assert "recruitment officer" in s.tags
    assert "check with Bolven" in s.body and "Bolven owes her a flask." in s.body


def test_parse_two_word_race_dk_and_parenthetical():
    s = parse_sketches(SAMPLE)
    assert (s[1].race, s[1].spec, s[1].class_name) == ("Tauren", "Balance", "Druid")
    assert (s[2].class_name, s[2].spec, s[2].group) == ("Death Knight", "Frost", "Raiders")
    assert s[2].tags == ("drawn to him", "bold")
    assert (s[3].race, s[3].tags) == ("Blood Elf", ("loud", "sulker"))


def test_parse_ignores_numbered_rules_outside_section_3():
    assert [s.name for s in parse_sketches(SAMPLE)] == ["Testra", "Bolven", "Cassa", "Dorn"]


def test_sketch_context_adds_other_mentions_within_cap():
    s = parse_sketches(SAMPLE)
    ctx = sketch_context(SAMPLE, s[0])
    assert "argue about consumables" in ctx and "checks his sums" in ctx
    assert "must be ignored" not in ctx


def test_assign_exact_race_class_prefers_level_80():
    a = assign_characters(parse_sketches(SAMPLE), ROSTER, HUMAN)
    assert a.mapping[1] == 101          # Gruk (guid 100) is level 79
    assert a.mapping[2] == 107 and a.mapping[4] == 103
    assert a.fallbacks == {}


def test_assign_pool_is_cast_only_by_default():
    s = parse_sketches(SAMPLE)
    only_gruk = [m for m in ROSTER if m.guid in (HUMAN, 100)]   # human plus a level 79
    assert assign_characters(s[:1], only_gruk, HUMAN).unmatched == [1]
    assert assign_characters(s[:1], only_gruk, HUMAN, cast_only=False).mapping == {1: 100}
    assert HUMAN not in assign_characters(s, ROSTER, HUMAN).mapping.values()


def test_assign_exact_matches_are_served_before_fallbacks():
    s = parse_sketches(SAMPLE)
    troll_druid = dataclasses.replace(s[1], index=1, race="Troll")     # only a fallback fits
    night_elf_druid = dataclasses.replace(s[1], index=2, race="Night Elf")  # Belra, exact
    a = assign_characters([troll_druid, night_elf_druid], ROSTER, HUMAN)
    assert a.mapping == {2: 102, 1: 107}
    assert list(a.fallbacks) == [1]
    assert a == assign_characters([troll_druid, night_elf_druid], ROSTER, HUMAN)


def test_assign_falls_back_to_same_class_then_unmatched():
    s = parse_sketches(SAMPLE)
    troll_druid = dataclasses.replace(s[1], index=9, race="Troll")
    a = assign_characters([troll_druid, s[2]], ROSTER, HUMAN)
    assert a.mapping[9] == 102 and 9 in a.fallbacks
    assert a.unmatched == [s[2].index]


def test_assign_never_uses_human_or_a_character_twice():
    s = parse_sketches(SAMPLE)
    mages = [dataclasses.replace(s[3], index=i) for i in (1, 2, 3)]
    a = assign_characters(mages, ROSTER, HUMAN)
    assert HUMAN not in a.mapping.values()
    assert sorted(a.mapping.values()) == [103] and a.unmatched == [2, 3]


def test_apply_renames_word_boundary():
    assert apply_renames("Testra pulls; Testrax waits", {"Testra": "Gruk"}) == \
        "Gruk pulls; Testrax waits"
    assert apply_renames("A meets B", {"A": "B", "B": "A"}) == "B meets A"


def test_convert_forces_identity_and_approves_quoted_lines():
    s = parse_sketches(SAMPLE)[0]
    renames = {"Testra": "Gruk", "Bolven": "Fenn"}
    llm = _llm()
    p = run(convert_sketch(llm, "m", s, sketch_context(SAMPLE, s), by_name["Gruk"], renames,
                           TM))
    assert p.identity.age == s.age and p.character_name == "Gruk" and p.core
    assert p.guid == 100 and p.play.guild_role == "officer"
    assert (p.identity.region, p.identity.accent, p.identity.real_name) == ("US", "en-us", "Ann")
    assert [l.approved for l in p.sample_lines[:2]] == [True, True]
    assert p.sample_lines[1].text == "check with Fenn, he has the mats."
    assert p.sample_lines[2].approved is False
    assert "Testra" not in json.dumps(persona_to_dict(p))
    assert llm.calls[0]["format"]["properties"]["schema"] == {"const": 1}
    assert llm.calls[0]["think"] is False and llm.calls[0]["options"]["num_ctx"] == 8192


def test_convert_romance_only_when_sketch_says_drawn():
    s = parse_sketches(SAMPLE)
    drawn = run(convert_sketch(_llm(), "m", s[2], "ctx", by_name["Arkon"], {}, TM))
    assert (drawn.romance.stance, drawn.romance.style) == ("drawn", "bold")

    def says_drawn(d):
        d["romance"] = {"stance": "drawn", "style": "shy", "jealousy_mode": None}
    other = run(convert_sketch(_llm(extra=says_drawn), "m", s[0], "ctx", by_name["Gruk"], {},
                               TM))
    assert other.romance.stance == "fond" and other.romance.style is None

    def says_lust(d):
        d["romance"] = {"stance": "lust", "style": "bold", "jealousy_mode": None}
    other = run(convert_sketch(_llm(extra=says_lust), "m", s[0], "ctx", by_name["Gruk"], {},
                               TM))
    assert other.romance.stance == "fond" and other.romance.style is None


def test_convert_drops_a_dial_the_model_writes():
    """A member's dial is the owner's to set (fix wave G4), never the model's."""
    def says_ten(d):
        d["romance"] = {"stance": "drawn", "style": "bold", "jealousy_mode": None, "dial": 10}
    s = parse_sketches(SAMPLE)
    p = run(convert_sketch(_llm(extra=says_ten), "m", s[2], "ctx", by_name["Arkon"], {}, TM))
    assert p.romance.stance == "drawn" and p.romance.dial is None


def test_convert_fails_loudly_and_never_truncates():
    def too_many(d):
        d["likes"] = [f"thing {i}" for i in range(7)]
    llm = _llm(extra=too_many)
    s = parse_sketches(SAMPLE)[0]
    with pytest.raises(JSONJobFailed):
        run(convert_sketch(llm, "m", s, "ctx", by_name["Gruk"], {}, TM))
    last_user = llm.calls[-1]["messages"][-1]["content"]
    assert "condense" in last_user and "condense" not in llm.calls[0]["messages"][-1]["content"]


def test_convert_condenses_after_two_bad_replies_and_returns_valid_persona():
    replies = iter([True, True, False])

    def script(model, messages):
        d = _base_persona_dict(1, "Zed")
        if next(replies):
            d["likes"] = [f"thing {i}" for i in range(7)]
        return json.dumps(d)
    llm = FakeLLM(script)
    s = parse_sketches(SAMPLE)[0]
    p = run(convert_sketch(llm, "m", s, "ctx", by_name["Gruk"], {}, TM))
    assert len(p.likes) == 1 and persona_from_dict(persona_to_dict(p)) == p
    assert "condense" in llm.calls[-1]["messages"][-1]["content"]


def test_quoted_lines_dedupe_then_cut_and_report():
    s = parse_sketches(SAMPLE)[0]
    body = "\n".join(f'"line {i}" / "line {i}"' for i in range(14))
    big = dataclasses.replace(s, body=body)
    notes = []
    p = run(convert_sketch(_llm(), "m", big, "ctx", by_name["Gruk"], {}, TM, notes))
    assert [l.text for l in p.sample_lines] == [f"line {i}" for i in range(12)]
    assert all(l.approved for l in p.sample_lines)
    assert notes and int(notes[-1]) >= 3  # two quoted lines plus the model's own


def test_convert_recovers_on_second_reply():
    replies = iter([True, False])

    def script(model, messages):
        d = _base_persona_dict(1, "Zed")
        if next(replies):
            d["likes"] = [f"thing {i}" for i in range(7)]
        return json.dumps(d)
    s = parse_sketches(SAMPLE)[0]
    p = run(convert_sketch(FakeLLM(script), "m", s, "ctx", by_name["Gruk"], {}, TM))
    assert len(p.likes) == 1


def _rel_persona(guid, name, stance, rels=()):
    d = _base_persona_dict(guid, name)
    d["romance"] = {"stance": stance, "style": "bold" if stance in ("drawn", "lust") else None,
                    "jealousy_mode": None}
    d["relationships"] = [{"other": o, "kind": k, "note": "n"} for o, k in rels]
    return persona_from_dict(d)


def test_seed_relationships_values(store):
    ps = [_rel_persona(101, "Arkon", "drawn", [("Belra", "friend"), ("Coros", "friction"),
                                               ("@human", "stance"), ("Nobody", "friend")]),
          _rel_persona(102, "Belra", "fond"), _rel_persona(103, "Coros", "none")]
    n = seed_relationships(store, ps, {"Arkon": 101, "Belra": 102, "Coros": 103}, HUMAN, 5000.0)
    assert n == 5
    r = store.get_relationship(101, 102)
    assert (r.affinity, r.trust, r.familiarity) == (40, 40, 60)
    r = store.get_relationship(101, 103)
    assert (r.affinity, r.familiarity) == (-20, 50)
    r = store.get_relationship(101, HUMAN)
    assert (r.affinity, r.attraction, r.familiarity, r.romance_stage) == (30, 40, 30, "notices")
    r = store.get_relationship(102, HUMAN)
    assert (r.affinity, r.attraction, r.familiarity, r.romance_stage) == (30, 0, 30, "none")
    r = store.get_relationship(103, HUMAN)
    assert (r.affinity, r.attraction, r.familiarity) == (10, 0, 20)


def test_seed_relationships_for_lust(store):
    seed_relationships(store, [_rel_persona(104, "Dunna", "lust")], {}, HUMAN, 5000.0)
    r = store.get_relationship(104, HUMAN)
    assert (r.affinity, r.attraction, r.familiarity, r.romance_stage) == (30, 60, 30, "notices")


def test_seed_relationships_leaves_lived_pairs_alone(store):
    store.put_relationship(dataclasses.replace(store.get_relationship(102, HUMAN),
                                               affinity=77, interactions=4))
    seed_relationships(store, [_rel_persona(102, "Belra", "fond")], {}, HUMAN, 5000.0)
    assert store.get_relationship(102, HUMAN).affinity == 77


def _rel_llm():
    def add(d):
        d["relationships"] = [{"other": "Fenn", "kind": "friend", "note": "n"},
                              {"other": "Nobody", "kind": "friction", "note": "n"}]
    return _llm(extra=add)


@pytest.fixture
def md_file(tmp_path):
    p = tmp_path / "sketches.md"
    p.write_text(SAMPLE, encoding="utf-8")
    return p


def test_seed_cast_dry_run_makes_no_calls_or_writes(store, md_file):
    store.upsert_cast(ROSTER, 1000.0)
    llm = _llm()
    r = run(seed_cast(store, llm, CFG, md_file, human_guid=HUMAN, dry_run=True))
    assert llm.calls == [] and store.persona_versions() == {}
    assert (r.assigned, r.unmatched) == (3, [3]) and len(r.table) == 4
    assert "UNMATCHED" in r.table[2]


def test_seed_cast_creates_then_skips_then_force(store, md_file):
    store.upsert_cast(ROSTER, 1000.0)
    llm = _llm()
    r = run(seed_cast(store, llm, CFG, md_file, human_guid=HUMAN))
    assert sorted(r.created) == [101, 103, 107] and r.unmatched == [3] and r.failed == {}
    assert load_persona(store, 101).identity.age == 29
    assert store.get_relationship(101, HUMAN).familiarity == 20
    n = len(llm.calls)
    r2 = run(seed_cast(store, llm, CFG, md_file, human_guid=HUMAN))
    assert r2.created == [] and sorted(r2.skipped) == [101, 103, 107] and len(llm.calls) == n
    r3 = run(seed_cast(store, llm, CFG, md_file, human_guid=HUMAN, force=True))
    assert len(r3.created) == 3
    assert set(store.persona_versions().values()) == {1}  # identical personas keep their version


def test_seed_cast_records_failures_and_continues(store, md_file):
    store.upsert_cast(ROSTER, 1000.0)
    r = run(seed_cast(store, FakeLLM(lambda model, messages: "not json"), CFG, md_file,
                      human_guid=HUMAN))
    assert r.created == [] and sorted(r.failed) == [101, 103, 107]
    assert store.persona_versions() == {}


def test_real_brainstorm_parses_if_present():
    if not BRAINSTORM.exists():
        pytest.skip("private file absent")
    md = BRAINSTORM.read_text(encoding="utf-8")
    sketches, unparsed = parse_sketches_checked(md)
    items = [l for l in _section_lines(md, 3) if seed_mod._ITEM_START.match(l)]
    assert unparsed == [] and len(sketches) == len(items) >= 20
    for x in sketches:
        assert x.name and x.real_name and x.age >= 18 and x.race and x.spec and x.class_name
        assert x.gender in ("female", "male") and x.region in ("UK", "US")
        assert x.group in ("Officers", "Raiders", "Socials") and x.body
    assert len({x.index for x in sketches}) == len(sketches)


def test_relationships_survive_a_run_that_died_after_saving(store, md_file, monkeypatch):
    store.upsert_cast(ROSTER, 1000.0)

    def boom(*a, **k):
        raise RuntimeError("died between saving and seeding")
    real = seed_mod.seed_relationships
    monkeypatch.setattr(seed_mod, "seed_relationships", boom)
    with pytest.raises(RuntimeError):
        run(seed_cast(store, _rel_llm(), CFG, md_file, human_guid=HUMAN))
    assert sorted(store.persona_versions()) == [101, 103, 107]
    assert store.get_relationship(101, 107).familiarity == 0
    monkeypatch.setattr(seed_mod, "seed_relationships", real)
    llm = _rel_llm()
    r = run(seed_cast(store, llm, CFG, md_file, human_guid=HUMAN))
    assert llm.calls == [] and sorted(r.skipped) == [101, 103, 107]
    assert store.get_relationship(101, 107).familiarity == 60
    assert store.get_relationship(101, HUMAN).familiarity == 20
    assert r.unknown_relations and all("Nobody" in u for u in r.unknown_relations)
    store.put_relationship(dataclasses.replace(store.get_relationship(101, 107), affinity=5,
                                               interactions=2))
    run(seed_cast(store, llm, CFG, md_file, human_guid=HUMAN))
    assert store.get_relationship(101, 107).affinity == 5


def test_seed_cast_reports_non_cast_members(store, md_file):
    store.upsert_cast(ROSTER, 1000.0)
    r = run(seed_cast(store, None, CFG, md_file, human_guid=HUMAN, dry_run=True))
    assert r.not_cast == ["Gruk (level 79)"]


BAD_SKETCH = SAMPLE.replace("(Ray, 47) · Tauren Balance", "(Ray, 47) · Zorblax Balance")


def test_unparseable_heading_is_reported_not_glued_on():
    ok, bad = parse_sketches_checked(SAMPLE)
    assert bad == []
    sketches, unparsed = parse_sketches_checked(BAD_SKETCH)
    assert [s.name for s in sketches] == ["Testra", "Cassa", "Dorn"]
    assert len(unparsed) == 1 and unparsed[0].startswith("item 2")
    assert sketches[0].body == ok[0].body and "feeding the dog" not in sketches[0].body


def test_cli_dry_run_fails_on_unparsed_and_needs_no_ollama(tmp_path, capsys, monkeypatch):
    import leeroy_mind.llm.client as client

    def no_client(*a, **k):
        pytest.fail("no client for a dry run")
    monkeypatch.setattr(client, "OllamaClient", no_client)
    d = str(tmp_path / "d")
    roster = [{"guid": m.guid, "name": m.name, "race": m.race, "class": m.class_,
               "gender": m.gender, "level": m.level, "rank": m.rank, "online": m.online,
               "map": m.map, "zone": m.zone} for m in ROSTER]
    rf = tmp_path / "r.json"
    rf.write_text(json.dumps(roster))
    good, bad = tmp_path / "good.md", tmp_path / "bad.md"
    good.write_text(SAMPLE, encoding="utf-8")
    bad.write_text(BAD_SKETCH, encoding="utf-8")
    assert main(["init", "--data-dir", d]) == 0
    assert main(["roster", "--data-dir", d, "--file", str(rf)]) == 0
    capsys.readouterr()
    assert main(["personas", "--data-dir", d, "seed", "--brainstorm", str(good), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "human: Testmage (guid 1)" in out and "Gruk (level 79)" in out
    assert main(["personas", "--data-dir", d, "seed", "--brainstorm", str(bad), "--dry-run"]) == 1
    assert "UNPARSED item 2" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        main(["personas", "--data-dir", d, "seed", "--brainstorm", str(good), "--dry-run",
              "--human-guid", "9999"])


def test_seed_prompt_names_the_human_by_called_and_describe():
    h = HumanIdentity(1, "Testmage", "Testy", "Pat", True)
    s = parse_sketches(SAMPLE)[0]
    llm = _llm()
    run(convert_sketch(llm, "m", s, "ctx", by_name["Gruk"], {}, h))
    user = llm.calls[0]["messages"][-1]["content"]
    assert h.describe(include_real_name=False) in user and "in running text" in user
    assert "Testy" in user and "real name" not in user and "Pat" not in user


def test_seed_cast_builds_the_identity_from_config(store, md_file):
    store.upsert_cast(ROSTER, 1000.0)
    cfg = SimpleNamespace(models=CFG.models, human=SimpleNamespace(nickname="Testy", real_name="Pat"))
    llm = _llm()
    run(seed_cast(store, llm, cfg, md_file, human_guid=HUMAN))
    user = llm.calls[0]["messages"][-1]["content"]
    assert "Testy (character Testmage)" in user and "real name" not in user


def test_seed_rejects_a_record_with_the_owners_real_name():
    h = HumanIdentity(1, "Testmage", "Testy", "Pat", True)
    s = parse_sketches(SAMPLE)[0]
    llm = _llm(extra=lambda d: d.update(secret="once told Pat a secret"))
    with pytest.raises(JSONJobFailed):
        run(convert_sketch(llm, "m", s, "ctx", by_name["Gruk"], {}, h))


def test_seed_rejects_romance_between_members_but_not_toward_the_human():
    s = parse_sketches(SAMPLE)[0]
    bad = _llm(extra=lambda d: d.update(relationships=[
        {"other": "Fenn", "kind": "friend", "note": "has a crush on Fenn"}]))
    with pytest.raises(JSONJobFailed):
        run(convert_sketch(bad, "m", s, "ctx", by_name["Gruk"], {}, TM))
    ok = _llm(extra=lambda d: d.update(relationships=[
        {"other": "@human", "kind": "stance", "note": "has a crush on the GM"}]))
    p = run(convert_sketch(ok, "m", s, "ctx", by_name["Gruk"], {}, TM))
    assert p.relationships[0].other == "@human"


def test_seed_allows_rivalry_over_the_human_in_a_note_about_a_member():
    s = parse_sketches(SAMPLE)[0]
    note = "always flirting with Testmage, it's annoying"
    ok = _llm(extra=lambda d: d.update(relationships=[
        {"other": "Fenn", "kind": "friction", "note": note}]))
    p = run(convert_sketch(ok, "m", s, "ctx", by_name["Gruk"], {}, TM))
    assert p.relationships[0].other == "Fenn"


_CLASS_ID = {v: k for k, v in CLASSES.items()}


def _seed_play(spec, cls, **play):
    s = dataclasses.replace(parse_sketches(SAMPLE)[0], spec=spec, class_name=cls)
    char = dataclasses.replace(by_name["Arkon"], class_=_CLASS_ID[cls])
    llm = _llm(extra=lambda d: d["play"].update(play))
    return run(convert_sketch(llm, "m", s, "ctx", char, {}, TM)).play


MODEL = "Model Spec"  # what the model wrote: not a spec of any class


@pytest.mark.parametrize("spec,cls,model_role,expected,main", [
    ("Unholy", "Death Knight", "melee", "melee", "Unholy"),  # "unholy" is not "holy"
    ("Unholy", "Death Knight", "healer", "melee", "Unholy"),
    ("Blood", "Death Knight", "tank", "melee", "Blood"),     # seed tanks are Prot only
    ("Frost", "Death Knight", "ranged", "melee", "Frost"),
    ("Holy", "Paladin", "melee", "healer", "Holy"),
    ("Ret", "Paladin", "healer", "melee", "Retribution"),
    ("Prot", "Warrior", "melee", "tank", "Protection"),
    ("Fury", "Warrior", "tank", "melee", "Fury"),
    ("Protection", "Paladin", "healer", "tank", "Protection"),
    ("Feral", "Druid", "tank", "melee", "Feral"),
    ("Balance", "Druid", "melee", "ranged", "Balance"),
    ("Resto", "Druid", "melee", "healer", "Restoration"),
    ("Elemental", "Shaman", "melee", "ranged", "Elemental"),
    ("Enhancement", "Shaman", "ranged", "melee", "Enhancement"),
    ("Resto", "Shaman", "melee", "healer", "Restoration"),
    ("Shadow", "Priest", "healer", "ranged", "Shadow"),
    ("Disc", "Priest", "ranged", "healer", "Discipline"),
    ("Frost", "Mage", "melee", "ranged", "Frost"),
    ("Arcane", "Mage", "melee", "ranged", "Arcane"),
    ("Destruction", "Warlock", "melee", "ranged", "Destruction"),
    ("BM", "Hunter", "melee", "ranged", "Beast Mastery"),
    ("Beast Mastery", "Hunter", "melee", "ranged", "Beast Mastery"),
    ("MM", "Hunter", "melee", "ranged", "Marksmanship"),
    ("Combat", "Rogue", "ranged", "melee", "Combat"),
    ("Holy", "Warrior", "healer", "melee", "Arms"),      # never a role the class cannot fill
    ("Prot", "Mage", "tank", "ranged", "Arcane"),        # and a spec that plays the role
    ("", "Warrior", "tank", "melee", "Arms"),             # no spec in the sketch
])
def test_seed_role_and_main_spec_follow_the_spec_words_and_the_class(spec, cls, model_role,
                                                                     expected, main):
    play = _seed_play(spec, cls, raid_role=model_role, main_spec=MODEL)
    assert (play.raid_role, play.main_spec) == (expected, main)


def test_seed_main_spec_overrides_the_model_and_clears_a_matching_off_spec():
    play = _seed_play("Holy", "Paladin", main_spec="Retribution", off_spec="Holy")
    assert (play.main_spec, play.raid_role, play.off_spec) == ("Holy", "healer", "")
    play = _seed_play("Holy", "Paladin", main_spec="Retribution", off_spec="Protection")
    assert play.off_spec == "Protection"


def test_every_seed_spec_fits_its_class_and_role():
    for cls, specs in seed_mod.SEED_SPECS.items():
        for main, role in specs.items():
            assert len(main) <= 30
            assert (role if role in ("tank", "healer") else "damage") in class_roles(cls)
            assert role != "tank" or main == "Protection"


def test_seed_fails_fast_on_sketch_fields_without_model_calls(store, md_file):
    store.upsert_cast(ROSTER, 1000.0)
    for real in ("Ann", "Fenn"):  # a sketch's real name, then a word in its quoted lines
        cfg = SimpleNamespace(models=CFG.models,
                              human=SimpleNamespace(nickname="Testy", real_name=real))
        llm = _llm()
        r = run(seed_cast(store, llm, cfg, md_file, human_guid=HUMAN))
        assert r.failed, real
        msg = " ".join(r.failed.values())
        assert real not in msg and "owner's real name; use the nickname" in msg
        assert "sketch " in msg
        for guid in r.failed:
            assert guid not in r.created
        store.conn.execute("DELETE FROM persona_core")
        store.conn.commit()


def test_convert_sketch_checks_forced_fields_before_calling_the_model():
    h = HumanIdentity(1, "Testmage", "Testy", "Ann", True)
    s = parse_sketches(SAMPLE)[0]
    llm = _llm()
    with pytest.raises(ValueError, match="use the nickname") as e:
        run(convert_sketch(llm, "m", s, "ctx", by_name["Gruk"], {}, h))
    assert "Ann" not in str(e.value) and llm.calls == []


def test_seed_context_has_the_real_name_replaced_by_the_nickname():
    h = HumanIdentity(1, "Testmage", "Testy", "Zoltan", True)
    s = parse_sketches(SAMPLE)[0]
    llm = _llm()
    run(convert_sketch(llm, "m", s, "Zoltan asked and Zoltan's mate said get zoltan", by_name["Gruk"],
                       {}, h))
    user = llm.calls[0]["messages"][-1]["content"]
    assert "Zoltan" not in user and "Testy asked and Testy's mate" in user
    assert "zoltan" in user  # lowercase use is left alone


def test_seed_prompts_carry_the_humans_pronouns(store, md_file):
    h = HumanIdentity(1, "Testmage", "Testy", "Pat", True, "she")
    llm = _llm()
    run(convert_sketch(llm, "m", parse_sketches(SAMPLE)[0], "ctx", by_name["Gruk"], {}, h))
    assert "Testy (character Testmage), the guild master, she/her" \
        in llm.calls[0]["messages"][-1]["content"]
    store.upsert_cast(ROSTER, 1000.0)
    cfg = SimpleNamespace(models=CFG.models,
                          human=SimpleNamespace(nickname="Testy", real_name="", pronouns="he"))
    llm = _llm()
    run(seed_cast(store, llm, cfg, md_file, human_guid=HUMAN))
    assert "Testy (character Testmage), the guild master, he/him" \
        in llm.calls[0]["messages"][-1]["content"]


# -- the human's relationships (controller ruling "final I2") ---------------------------------

HG = HumanIdentity(HUMAN, "Testmage", "Testy", "", True, "he")


def _declares(guid, name, stance, decls):
    """A persona whose relationships are these (other, kind, note) declarations."""
    p = _rel_persona(guid, name, stance)
    d = persona_to_dict(p)
    d["relationships"] = [{"other": o, "kind": k, "note": n} for o, k, n in decls]
    return persona_from_dict(d)


def test_a_friend_declaration_toward_the_human_starts_close(store):
    from leeroy_mind.talk.prompt import is_close
    ps = [_declares(101, "Arkon", "lust", [("@human", "friend", "has had his back for years")]),
          _declares(102, "Belra", "fond", [("@human", "friction", "thinks he plays favourites")]),
          _declares(103, "Coros", "none", [("@human", "stance", "rates his calls")])]
    seed_relationships(store, ps, {"Arkon": 101, "Belra": 102, "Coros": 103}, HUMAN, 5000.0)
    r = store.get_relationship(101, HUMAN)
    assert (r.affinity, r.trust, r.familiarity) == (40, 40, 60)
    assert (r.attraction, r.romance_stage, r.label) == (60, "notices", "friend")  # the stance stays
    assert r.notes == "has had his back for years" and is_close(r)
    r = store.get_relationship(102, HUMAN)
    assert (r.affinity, r.familiarity, r.label, r.attraction) == (-20, 50, "friction", 0)
    assert r.notes == "thinks he plays favourites" and not is_close(r)
    r = store.get_relationship(103, HUMAN)
    assert (r.affinity, r.familiarity, r.label, r.notes) == (10, 20, "", "rates his calls")


@pytest.mark.parametrize("other", ["Testy", "testmage", "GM", "the GM", "guild master"])
def test_the_humans_names_in_a_declaration_mean_the_human(store, other):
    p = _declares(104, "Dunna", "drawn", [(other, "friend", "old raid buddy")])
    seed_relationships(store, [p], {"Testmage": HUMAN, "Dunna": 104}, HUMAN, 5000.0, human=HG)
    r = store.get_relationship(104, HUMAN)
    assert (r.affinity, r.label, r.notes, r.attraction) == (40, "friend", "old raid buddy", 40)


def test_gm_means_the_human_only_when_he_is_the_guild_master(store):
    p = _declares(104, "Dunna", "drawn", [("GM", "friend", "old raid buddy")])
    seed_relationships(store, [p], {}, HUMAN, 5000.0,
                       human=dataclasses.replace(HG, is_guild_master=False))
    assert store.get_relationship(104, HUMAN).label == ""


def test_a_friend_beats_a_friction_and_a_friction_beats_a_stance(store):
    p = _declares(105, "Ebbo", "none", [("@human", "stance", "a stance note"),
                                        ("@human", "friction", "a friction note")])
    seed_relationships(store, [p], {}, HUMAN, 5000.0, human=HG)
    assert store.get_relationship(105, HUMAN).notes == "a friction note"


def test_member_targets_are_matched_in_any_case(store):
    p = _rel_persona(101, "Arkon", "none", [("belra", "friend")])
    seed_relationships(store, [p], {"Arkon": 101, "Belra": 102}, HUMAN, 5000.0)
    assert store.get_relationship(101, 102).label == "friend"


def test_seed_conversion_writes_the_humans_names_as_at_human():
    def rels(d):
        d["relationships"] = [{"other": "Testy", "kind": "friend", "note": "old raid buddy"},
                              {"other": "GM", "kind": "stance", "note": "rates his calls"},
                              {"other": "fenn", "kind": "friend", "note": "levels with him"}]
    s = parse_sketches(SAMPLE)[0]
    p = run(convert_sketch(_llm(extra=rels), "m", s, "ctx", by_name["Gruk"], {}, HG))
    assert [r.other for r in p.relationships] == ["@human", "@human", "fenn"]


def test_unknown_relations_know_the_human_and_flag_members_who_are_not_cast():
    ps = [_rel_persona(101, "Arkon", "none", [("Testy", "friend"), ("Gruk", "friend"),
                                              ("Nobody", "friend"), ("belra", "friction")])]
    names = {m.name: m.guid for m in ROSTER}
    out = seed_mod.unknown_relations(ps, names, human=HG, cast={101, 102, 103, 104, 105, 107})
    assert out == ["Arkon -> Gruk (on the roster, not cast)", "Arkon -> Nobody"]


# -- typing: full form -> chat form (rehearsal 1) ----------------------------------------------

def test_seed_conversion_turns_a_backwards_typing_map_round_and_says_which_way():
    from leeroy_mind.personas.schema import TYPING_RULE

    def typing(d):
        d["typing"]["abbreviations"] = {"lol": "laughing out loud"}
        d["typing"]["emoticons"] = [chr(0x2728), "xD"]
    llm = _llm(extra=typing)
    p = run(convert_sketch(llm, "m", parse_sketches(SAMPLE)[0], "ctx", by_name["Gruk"], {}, TM))
    assert p.typing.abbreviations == {"laughing out loud": "lol"}
    assert p.typing.emoticons == ("xD",) and len(llm.calls) == 1
    assert TYPING_RULE in llm.calls[0]["messages"][-1]["content"]


# -- carried minors (final-wave-carry.md, Tasks 7 and 8; spec Minor 5) --------------------------

def test_the_cut_lines_note_comes_from_the_attempt_that_was_kept():
    s = dataclasses.replace(parse_sketches(SAMPLE)[0],
                            body="\n".join(f'"line {i}"' for i in range(10)))
    own = iter([15, 1])

    def script(model, messages):
        d = _base_persona_dict(1, "Zed")
        n = next(own)
        d["sample_lines"] = [{"text": f"own {i}", "approved": False} for i in range(n)]
        if n == 15:
            d["likes"] = [f"thing {i}" for i in range(7)]  # refused: this attempt is not kept
        return json.dumps(d)
    notes = []
    p = run(convert_sketch(FakeLLM(script), "m", s, "ctx", by_name["Gruk"], {}, TM, notes))
    assert len(p.sample_lines) == 11 and notes == []


D = chr(0xB7)
MINI = "\n".join([
    "## 3. Sketches", "", "### Raiders", "",
    f"1. **Quillo** (Ann, 30) {D} Orc Arms Warrior {D} M {D} UK.",
    "   - Works nights.",
    "   1. **Bit:** a dry speech before a new boss.",
    "   2. **Habit:** smokes on breaks.",
    f"2. **Mirel** (Joy, 27) {D} Undead Frost Mage {D} F {D} US.",
    "   - Student.", ""])


def test_numbered_bold_sub_items_are_not_taken_for_sketches():
    sketches, unparsed = parse_sketches_checked(MINI)
    assert [s.name for s in sketches] == ["Quillo", "Mirel"] and unparsed == []
    assert "a dry speech" in sketches[0].body


def test_without_a_spec_word_the_main_spec_is_one_the_class_plays_in_that_role():
    assert _seed_play("Holy", "Warrior", main_spec=MODEL).main_spec == "Arms"
    assert _seed_play("Prot", "Mage", main_spec=MODEL).main_spec == "Arcane"
    assert _seed_play("", "Priest", main_spec="Holy").main_spec == "Shadow"  # ranged, not healer
    assert _seed_play("", "Warrior", main_spec="Fury").main_spec == "Fury"   # fits: kept
    play = _seed_play("", "Warrior", main_spec="Protection", off_spec="Arms")
    assert (play.main_spec, play.raid_role, play.off_spec) == ("Arms", "melee", "")


def test_a_seed_man_gets_a_jealousy_mode_from_his_tags_else_from_his_guid():
    from leeroy_mind.personas.schema import JEALOUSY_MODES
    man = dataclasses.replace(parse_sketches(SAMPLE)[0], gender="male", tags=("loud", "Sulker"))
    p = run(convert_sketch(_llm(), "m", man, "ctx", by_name["Gruk"], {}, TM))
    assert p.romance.jealousy_mode == "sulker"
    man = dataclasses.replace(man, tags=("play-by-play",))
    p = run(convert_sketch(_llm(), "m", man, "ctx", by_name["Gruk"], {}, TM))
    assert p.romance.jealousy_mode == "play_by_play"
    p = run(convert_sketch(_llm(), "m", dataclasses.replace(man, tags=()), "ctx",
                           by_name["Gruk"], {}, TM))
    assert p.romance.jealousy_mode == JEALOUSY_MODES[by_name["Gruk"].guid % len(JEALOUSY_MODES)]
    woman = dataclasses.replace(man, gender="female", tags=())
    p = run(convert_sketch(_llm(), "m", woman, "ctx", by_name["Gruk"], {}, TM))
    assert p.romance.jealousy_mode is None


def test_sample_lines_take_the_renames_in_any_case_and_keep_it():
    s = dataclasses.replace(parse_sketches(SAMPLE)[0], body='"ask quillo, then QUILLO"')

    def lines(d):
        d["sample_lines"] = [{"text": "quillo pulls, i heal", "approved": False}]
        d["secret"] = "Quillo once owed quillo money"
    p = run(convert_sketch(_llm(extra=lines), "m", s, "ctx", by_name["Gruk"],
                           {"Quillo": "Arkon"}, TM))
    assert [x.text for x in p.sample_lines] == ["ask arkon, then ARKON", "arkon pulls, i heal"]
    assert p.secret == "Arkon once owed quillo money"  # prose: as written only
