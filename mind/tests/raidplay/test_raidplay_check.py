"""`raidplay check` against the repo data with a fake spell book, the DBM extractor on a made-up
Lua fixture (no DBM text or value is ever committed), the CLI and the console's read-only panel."""

import asyncio
import json
from pathlib import Path

import pytest

from leeroy_mind import raidplay
from leeroy_mind.raidplay import check, dbm
from leeroy_mind.raidplay.cli import raidplay_main


class FakeBook:
    """Knows every id and name the repo's playbooks use, minus any it is told to forget."""

    def __init__(self, playbooks, *, forget_id=None, forget_name=None):
        self.known = {i for m in playbooks.mechanics() for i in m.all_ids()} - {forget_id}
        self.names = {n for b in playbooks.bosses() for c in b.checks for n in (*c.names, *c.exempt)} - {forget_name}
        self.by_check = {}
        for boss in playbooks.bosses():
            for c in boss.checks:
                ids = set().union(*(m.all_ids() for m in boss.mechanics if m.check == c.id))
                for n in c.names:
                    self.by_check.setdefault(n, set()).update(ids)

    def info(self, id):
        return object() if id in self.known else None

    def ids(self, name):
        return frozenset(self.by_check.get(name) or {999999}) if name in self.names else frozenset()


def test_the_repo_data_passes_the_check():
    pb = raidplay.load()
    stock = frozenset(a for m in pb.mechanics() for a in m.playerbots)
    assert check.validate(pb, FakeBook(pb), stock=stock) == []
    missing = check.validate(pb, stock=stock - {"icc spike"})
    assert [(f.code, f.ref, f.detail) for f in missing] == [
        ("stock_action_unknown", "icc.marrowgar.bone_spike", "icc spike")]


def test_registered_names_read_the_playerbots_source(tmp_path):
    (tmp_path / "Ai").mkdir()
    (tmp_path / "Ai" / "A.h").write_bytes(b'creators["made up"] = &X::y;\n')
    (tmp_path / "Ai" / "B.cpp").write_bytes(b'creators["other thing"] = &X::z;\n')
    assert check.registered_names(tmp_path) == {"made up", "other thing"}


def test_an_unknown_spell_or_name_is_an_error():
    pb = raidplay.load()
    found = check.validate(pb, FakeBook(pb, forget_id=69845, forget_name="Instability"))
    assert ("error", "spell_unknown", "icc.sindragosa.frost_bomb", "69845") in [
        (f.level, f.code, f.ref, f.detail) for f in found]
    assert ("error", "check_name_unresolved", "icc.sindragosa.check_instability") in [
        (f.level, f.code, f.ref) for f in found]


def test_a_covered_raid_missing_a_boss_is_an_error():
    doc = Path(raidplay.__file__).with_name("data").joinpath("toc.toml").read_bytes()
    cut = doc[:doc.index(b"[[boss]]\nindex = 4")]
    found = check.validate(raidplay.load_files({"toc.toml": cut}))
    assert [(f.code, f.ref) for f in found] == [("boss_missing", "649:4")]


# A boss mod written from scratch in the shape the extractor reads: only the call names it must
# recognise (NewMod, SetCreatureID, New<Kind>, IsSpellID, spellId ==); every id, number, key and
# kind name below is made up. No value comes from any installed addon.
LUA = """local m = DBM:NewMod("Quillbeast", "DBM-Madeup", 1)
m:SetCreatureID(900100, 900101)
local ping = m:NewWobbleAnnounce(900001, 7)
local pong = m:NewHonkAnnounce("Madeup1", 9, 900002)
local tick = m:NewSquiggleTimer(77.5, 900003)
local tock = m:NewDoodleTimer(12, "Madeup2", "path/to/nothing")
local stage = m:NewWobbleAnnounce(3)
function m:Madeup3(a)
  if a:IsSpellID(900004, 900005) then ping:Show() end
  if a.spellId == 900006 then tick:Start() end
end
"""


@pytest.fixture
def addons(tmp_path):
    folder = tmp_path / "AddOns" / "DBM-Madeup" / "Wing"
    folder.mkdir(parents=True)
    (folder / "Quillbeast.lua").write_bytes(LUA.encode())
    (folder.parent / "localization.xx.lua").write_bytes(b'Q.Madeup1 = "never kept"')
    (tmp_path / "AddOns" / "NotDbm").mkdir()
    (tmp_path / "AddOns" / "NotDbm" / "x.lua").write_bytes(LUA.encode())
    return tmp_path / "AddOns"


def test_the_extractor_keeps_numbers_only(addons):
    facts = dbm.extract(addons)
    assert facts["format"] == 1 and len(facts["mods"]) == 1
    mod = facts["mods"][0]
    assert mod["creatures"] == [900100, 900101] and mod["file"] == "Wing/Quillbeast.lua"
    assert mod["warnings"] == [{"kind": "wobble_announce", "spell": 900001},
                               {"kind": "honk_announce", "spell": 900002}]
    assert mod["timers"] == [{"kind": "squiggle_timer", "seconds": 77.5, "spell": 900003},
                             {"kind": "doodle_timer", "seconds": 12.0, "spell": None}]
    assert mod["spells"] == [900001, 900002, 900003, 900004, 900005, 900006]
    assert "never kept" not in json.dumps(facts) and "Madeup1" not in json.dumps(facts)


def test_the_cross_check_is_notes_only():
    pb = raidplay.load()
    boss = pb.boss(533, 2)
    mine = next(iter(boss.mechanics[0].all_ids()))
    facts = {"format": 1, "mods": [{"creatures": [boss.creatures[0]], "spells": [mine, 900007],
                                    "timers": [{"kind": "berserk_timer", "seconds": 987, "spell": None}]}]}
    found = check.dbm_cross_check(pb, facts)
    assert all(f.level == "note" for f in found)
    ours = [(f.code, f.detail) for f in found if f.ref == "533:2"]
    assert ("dbm_spell_unlisted", "900007") in ours and ("dbm_spell_unlisted", str(mine)) not in ours
    assert ("dbm_enrage_differs", "dbm=987 script=360") in ours
    assert ("dbm_boss_unmatched", "533:0") in [(f.code, f.ref) for f in found]
    assert check.dbm_cross_check(pb, {"mods": "junk"}) == []


def test_write_and_read_round_trip(tmp_path, addons):
    out = tmp_path / "data" / "raidplay" / "dbm.json"
    dbm.write(dbm.extract(addons), out)
    assert dbm.read(out)["mods"][0]["creatures"] == [900100, 900101]
    out.write_bytes(b'{"format": 99}')
    assert dbm.read(out) is None and dbm.read(tmp_path / "missing.json") is None


def test_cli_show_and_check(capsys, tmp_path):
    assert raidplay_main(["show", "naxx", "gluth", "--difficulty", "1"]) == 0
    out = capsys.readouterr().out
    assert "533:2 Gluth" in out and "spells 54426" in out and "naxx.gluth.mortal_wound" in out
    assert raidplay_main(["show", "631", "11", "--json"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown[0]["name"] == "Sindragosa" and shown[0]["call"].startswith("sindragosa:")
    assert raidplay_main(["show", "603"]) == 1
    assert raidplay_main(["show", "naxx", "--difficulty", "3"]) == 1
    capsys.readouterr()
    assert raidplay_main(["check", "--no-book", "--data-dir", str(tmp_path)]) == 0
    assert "errors=0" in capsys.readouterr().out
    assert raidplay_main(["check", "--dbc", str(tmp_path), "--data-dir", str(tmp_path)]) == 2


def test_cli_extract_never_writes_where_git_would_track(capsys, tmp_path, addons):
    data = tmp_path / "mind-data"
    assert raidplay_main(["dbm-extract", "--addons", str(addons), "--data-dir", str(data)]) == 0
    assert dbm.read(data / "raidplay" / "dbm.json")["mods"]
    repo = Path(raidplay.__file__).resolve().parents[3]
    assert raidplay_main(["dbm-extract", "--addons", str(addons), "--data-dir", str(data),
                          "--out", str(repo / "mind" / "dbm.json")]) == 2
    assert not (repo / "mind" / "dbm.json").exists()
    assert raidplay_main(["dbm-extract", "--addons", str(addons), "--data-dir", str(repo.parent)]) == 2
    assert raidplay_main(["dbm-extract", "--addons", str(tmp_path / "none"), "--data-dir", str(data)]) == 2


def test_the_console_panel_serves_the_playbooks(tmp_path):
    from console.rig import http, make_app, running
    from leeroy_mind.console.playbookview import playbook_view
    view = playbook_view()
    assert view["totals"]["raids"] == 3 and view["totals"]["bosses"] == 33
    assert [r["key"] for r in view["raids"]] == ["naxx", "icc", "toc"]
    first = view["raids"][0]["bosses"][0]["mechanics"][0]
    assert set(first) == {"id", "name", "kind", "roles", "phase", "only", "handled_by", "handled_note",
                          "check", "explain"}
    totals = view["totals"]
    assert totals["scored"] == 14 and totals["scored_playbooks"] == 16 and totals["partial"] > 0

    async def go():
        app = make_app(tmp_path, mind=running(tmp_path))
        await app.start()
        try:
            r = await http(app, "GET", "/api/training/playbooks")
            assert r.status == 200 and r.json()["totals"] == view["totals"]
            assert (await http(app, "GET", "/api/training/playbooks", key=False)).status == 403
        finally:
            await app.close()
    asyncio.run(go())


@pytest.mark.live
def test_every_spell_id_is_in_the_real_spell_book():
    import os
    from leeroy_mind.training.spells import load_spellbook
    directory = os.environ.get("LEEROY_TEST_DBC_DIR")
    if not directory:
        pytest.skip("Set LEEROY_TEST_DBC_DIR for the explicit read-only DBC check")
    assert check.validate(raidplay.load(), load_spellbook(Path(directory))) == []


@pytest.mark.live
def test_every_stock_action_is_registered_by_playerbots():
    import os
    source = os.environ.get("LEEROY_TEST_PLAYERBOTS_SRC")
    if not source:
        pytest.skip("Set LEEROY_TEST_PLAYERBOTS_SRC (mod-playerbots/src) for the read-only check")
    assert check.validate(raidplay.load(), stock=check.registered_names(Path(source))) == []
