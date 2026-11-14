"""The playbook loader: the repo data parses, lookups resolve per difficulty, and invented
documents that break the schema are refused with the entry named."""

import pytest

from leeroy_mind import raidplay
from leeroy_mind.raidplay import KINDS, PlaybookError, load_files

# Every boss index each covered raid must have, from wow.BOSS_NAMES.
COVERED = {
    533: list(range(15)),   # Naxxramas: Patchwerk .. Kel'Thuzad
    631: list(range(13)),   # Icecrown Citadel: Marrowgar .. the Lich King, Svalna at 9
    649: list(range(5)),    # Trial of the Crusader: the Beasts .. Anub'arak
}


def test_coverage_lists_every_covered_boss():
    pb = raidplay.load()
    assert pb.covered_maps() == (533, 631, 649)
    assert {m: [b.index for b in pb.raids[m].bosses] for m in pb.raids} == COVERED
    assert [b.name for b in pb.raids[533].bosses] == [
        "Patchwerk", "Grobbulus", "Gluth", "Noth the Plaguebringer", "Heigan the Unclean", "Loatheb",
        "Anub'Rekhan", "Grand Widow Faerlina", "Maexxna", "Thaddius", "Instructor Razuvious",
        "Gothik the Harvester", "the Four Horsemen", "Sapphiron", "Kel'Thuzad"]
    assert [b.slug for b in pb.raids[631].bosses] == [
        "marrowgar", "deathwhisper", "gunship", "saurfang", "festergut", "rotface", "putricide",
        "council", "lanathel", "svalna", "valithria", "sindragosa", "lichking"]
    assert [b.slug for b in pb.raids[649].bosses] == ["beasts", "jaraxxus", "champions", "twins", "anubarak"]


def test_every_mechanic_is_well_formed():
    pb = raidplay.load()
    ids = [m.id for m in pb.mechanics()]
    assert len(ids) == len(set(ids)) and len(ids) >= 150
    for boss in pb.bosses():
        assert boss.mechanics and boss.phases
        raid = pb.raids[boss.map_id]
        for m in boss.mechanics:
            assert m.id.startswith(f"{raid.key}.{boss.slug}.") and m.kind in KINDS
            assert m.roles and set(m.roles) <= {"tank", "healer", "melee", "ranged"}
            assert set(m.only) <= set(raid.difficulties)
            assert all(m.spell_ids(d) or m.adds or m.kind in ("positional", "kill_order") for d in m.only)
            if m.kind in raidplay.MOVEMENT_KINDS:
                assert all(m.number("radius", d) or m.number("range", d) for d in m.only)
            assert "\n" not in m.explain and 0 < len(m.explain) <= 200
            assert (m.handled_by != "none") == bool(m.playerbots)
            assert (m.handled_by == "partial") == bool(m.handled_note) or m.handled_by == "playerbots"
    assert sum(m.handled_by == "playerbots" for m in pb.mechanics()) > 50
    # Playerbots has no Trial of the Crusader strategy: only generic stock behaviour applies there.
    assert all(m.handled_by != "playerbots" for b in pb.raids[649].bosses for m in b.mechanics)


def test_playbook_for_resolves_a_training_context():
    sindragosa = raidplay.playbook_for(631, 11, 3)
    assert sindragosa.context == "631:11:3" and sindragosa.difficulty == "25h"
    assert sindragosa.boss.name == "Sindragosa" and sindragosa.enrage == 600
    bomb = raidplay.mechanic("icc.sindragosa.frost_bomb")
    assert bomb in sindragosa.mechanics and bomb.kind == "los"
    assert bomb.spell_ids("10") == (69845,) and bomb.spell_ids("25h") == (71055,)
    # Dominate Mind is not cast on 10-man normal.
    assert "icc.deathwhisper.dominate_mind" not in [m.id for m in raidplay.playbook_for(631, 1, 0).mechanics]
    assert "icc.deathwhisper.dominate_mind" in [m.id for m in raidplay.playbook_for(631, 1, 1).mechanics]
    gluth = raidplay.playbook_for(533, 2, 1)
    assert gluth.enrage == 360 and raidplay.mechanic("naxx.gluth.decimate").number("every", "25") == 90
    assert raidplay.mechanic("naxx.patchwerk.hateful_strike").spell_ids("25") == (59192,)
    assert raidplay.playbook_for(533, 2, 2) is None          # Naxxramas has no heroic
    assert raidplay.playbook_for(603, 0, 0) is None          # Ulduar is not covered
    assert raidplay.playbook_for(631, 99, 0) is None
    assert raidplay.mechanic("nope") is None
    assert raidplay.load().check("icc.marrowgar.check_avoid_coldflame").tag == "coldflame"


def test_lookup_by_spell_covers_every_difficulty():
    found = raidplay.mechanics_for_spell(71055)              # 25-man heroic Frost Bomb
    assert [m.id for m in found] == ["icc.sindragosa.frost_bomb"]
    assert [m.id for m in raidplay.mechanics_for_spell(74297)] == ["icc.lichking.harvest_soul"]
    assert raidplay.mechanic("icc.lichking.harvest_soul").spell_ids("25h") == (74297,)
    assert raidplay.mechanics_for_spell(1) == ()
    defile = raidplay.mechanic("icc.lichking.defile")
    assert defile.handled_by == "playerbots" and defile.phase == ("two", "three")
    assert raidplay.mechanic("icc.lichking.shadow_trap").handled_by == "partial"


def test_hits_fall_back_to_spells():
    m = raidplay.mechanic("icc.marrowgar.coldflame")
    assert m.hit_ids("25") == (70823,) and m.spell_ids("25") == (69140, 72705)
    plain = raidplay.mechanic("icc.deathwhisper.frostbolt")
    assert plain.hit_ids("10") == plain.spell_ids("10") == (71420,)


RAID = '''
map = 649
key = "toc"
difficulties = ["10", "25", "10h", "25h"]
script_dir = "x"
[[boss]]
index = 4
name = "Anub'arak"
slug = "anubarak"
script = "y.cpp"
phase = [{ id = "main", name = "Main" }]
[[boss.mechanic]]
id = "toc.anubarak.thing"
name = "Thing"
kind = "KIND"
roles = ["tank"]
spells = SPELLS
handled_by = "none"
explain = "Do the thing."
check = "CHECK"
radius = 3
EXTRA
'''


def _doc(kind="avoid_ground", spells="[1]", check="none", extra=""):
    return {"toc.toml": RAID.replace("KIND", kind).replace("SPELLS", spells)
            .replace("CHECK", check).replace("EXTRA", extra).encode()}


def test_an_invented_document_loads():
    pb = load_files(_doc(spells='{ "10" = [1], "25" = [2] }'))
    m = pb.mechanic("toc.anubarak.thing")
    assert m.spell_ids("10h") == (1,) and m.spell_ids("25h") == (2,)   # heroic falls back
    assert pb.playbook_for(649, 4, 3).mechanics == (m,)


@pytest.mark.parametrize("doc, why", [
    (_doc(kind="dance"), "kind"),
    (_doc(spells="[]"), "no spell ids"),
    (_doc(spells='{ "40" = [1] }'), "difficulty 40"),
    (_doc(check="toc.anubarak.check_missing"), "not one of this boss's checks"),
    (_doc(extra='[[boss.check]]\nid = "toc.anubarak.check_x"\nkind = "spread"\nsince = 2'), "not mapped"),
    (_doc(extra='[[boss.check]]\nid = "toc.anubarak.check_x"\nkind = "avoid"\nsince = 3'), "since"),
    (_doc(extra='[[boss.mechanic]]\nid = "toc.anubarak.thing"\nname = "T"\nkind = "kite"\nroles = ["tank"]\n'
                'spells = [1]\nhandled_by = "none"\nexplain = "x"'), "duplicate"),
    ({"toc.toml": _doc()["toc.toml"].replace(b"name = \"Anub'arak\"", b'name = "Anubarak"')}, "BOSS_NAMES"),
    (_doc().copy() | {"bad.toml": b"map = ["}, "not valid TOML"),
])
def test_schema_errors_name_the_problem(doc, why):
    with pytest.raises(PlaybookError, match=why):
        load_files(doc)


def test_movement_needs_a_distance_and_partial_needs_a_note():
    with pytest.raises(PlaybookError, match="radius or range"):
        load_files({"toc.toml": _doc()["toc.toml"].replace(b"radius = 3\n", b"")})
    partial = _doc()["toc.toml"].replace(b'handled_by = "none"', b'handled_by = "partial"\nplayerbots = ["x"]')
    with pytest.raises(PlaybookError, match="handled_note"):
        load_files({"toc.toml": partial})
    ok = load_files({"toc.toml": partial.replace(b'playerbots = ["x"]', b'playerbots = ["x"]\nhandled_note = "only tanks"')})
    assert ok.mechanic("toc.anubarak.thing").handled_note == "only tanks"
    rule = _doc(kind="positional", spells="[]")["toc.toml"].replace(b"radius = 3\n", b"")
    assert load_files({"toc.toml": rule}).mechanic("toc.anubarak.thing").spell_ids("10") == ()


def test_roles_and_handlers_are_closed():
    with pytest.raises(PlaybookError, match="roles"):
        load_files({"toc.toml": _doc()["toc.toml"].replace(b'["tank"]', b'["dps"]')})
    with pytest.raises(PlaybookError, match="playerbots names"):
        load_files({"toc.toml": _doc()["toc.toml"].replace(b'handled_by = "none"', b'handled_by = "playerbots"')})
    with pytest.raises(PlaybookError, match="explain|one line"):
        load_files({"toc.toml": _doc()["toc.toml"].replace(b'"Do the thing."', b'"' + b"x" * 300 + b'"')})
