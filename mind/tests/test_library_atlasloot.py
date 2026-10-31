from pathlib import Path

from leeroy_mind.library.atlasloot import (AtlasLootSource, _Place, decode, difficulty, load_codes,
                                           table_places)
from leeroy_mind.library.chunks import ItemRow, SourceResult
from leeroy_mind.library.core import Library

ADDONS = Path(__file__).parent / "fixtures" / "library" / "addons"
SAMPLE = (ADDONS / "AtlasLoot_WrathoftheLichKing" / "sample.lua").read_text(encoding="utf-8")


def test_codes_come_from_text_parsing_with_fallback():
    codes = load_codes((ADDONS / "AtlasLoot" / "Core" / "TextParsing.lua").read_text())
    assert codes["#c10#"] == "Death Knight" and codes["#p2#"] == "Blacksmithing"
    assert codes["#a4#"] == "Plate" and codes["#w5#"] == "Gun"  # #w5# only in the fallback
    assert decode("=ds=#s1#, #a4#", codes) == "Head, Plate"
    assert decode("13200 #faction# 770 #arena# / 95 #eoffrost#", codes) == \
        "13200 honor 770 arena points / 95 Emblem of Frost"
    assert decode("2370 #arena# #reqrating# 1950", codes) == \
        "2370 arena points (requires 1950 rating)"
    assert decode(None, codes) == ""


def test_places_follow_banners_wings_and_heroic():
    places = table_places(SAMPLE)
    assert places["FrostmawKeepWarden"] == _Place("Dungeons & Raids", "Frostmaw Keep", False)
    assert places["ICCFrostLord25Man"].instance == "Icecrown Citadel"  # wing banner folded in
    assert places["ICCFrostLord25ManHEROIC"] == _Place("Dungeons & Raids", "Icecrown Citadel", True)
    assert places["ArgentCrusadeSample"].category == "Factions"
    assert "Nope" not in places


def test_difficulty_labels():
    raid = _Place("Dungeons & Raids", "Icecrown Citadel", False)
    assert difficulty("ICCSindragosa", raid) == "10-player"
    assert difficulty("ICCSindragosa25Man", raid) == "25-player"
    assert difficulty("ICCSindragosa25ManHEROIC", raid) == "25-player heroic"
    assert difficulty("UtgardeKeepKeleseth", _Place("Dungeons & Raids", "Utgarde Keep", False)) == ""
    assert difficulty("ArgentCrusade", _Place("Factions", "Argent Crusade", False)) == ""


def test_source_chunks_without_world_items():
    res = AtlasLootSource(ADDONS).load(None)
    assert isinstance(res, SourceResult) and res.name == "atlasloot"
    by = {c.key: c for c in res.chunks}
    normal = by["atlas:FrostmawKeepWarden:0"]
    assert normal.title == "Warden Frostmaw / Rime" and normal.section == "Normal Mode"
    assert normal.instance == "Frostmaw Keep" and normal.kind == "loot"
    assert normal.text.startswith("What Warden Frostmaw / Rime drops (Frostmaw Keep, Normal Mode "
                                  "loot table in AtlasLoot): ")
    assert 'Glacier "Cold" Wraps (Wrist, Cloth; 31%)' in normal.text
    heroic = by["atlas:FrostmawKeepWarden:1"]
    assert heroic.section == "Heroic Mode" and "Emblem of Triumph (Token; 100%)" in heroic.text
    assert "item:90003" in heroic.ref_ids and "atlas:FrostmawKeepWarden" in heroic.ref_ids
    h = by["atlas:ICCFrostLord25ManHEROIC:0"]
    assert (h.title, h.section, h.instance) == ("The Frost Lord", "25-player heroic",
                                                "Icecrown Citadel")
    rep = by["atlas:ArgentCrusadeSample:0"]
    assert rep.instance is None and rep.section == "Revered"
    assert " items in AtlasLoot: " in rep.text and "drops" not in rep.text
    assert "honor" in rep.text and "Emblem of Frost" in rep.text
    assert any("no world item table" in line for line in res.log)


def test_world_item_names_win_when_known(tmp_path, clock):
    lib = Library(tmp_path / "library.db", clock=clock)
    try:
        lib._apply(SourceResult("world", items=[
            ItemRow(90001, "Rimed Warden's Helm", 3, 200, 80, "plate head")]))
        res = AtlasLootSource(ADDONS).load(lib)
        text = next(c.text for c in res.chunks if c.key == "atlas:FrostmawKeepWarden:0")
        assert "Rimed Warden's Helm (200 plate head; 28%)" in text
    finally:
        lib.close()


def test_missing_addon_dir_is_logged(tmp_path):
    res = AtlasLootSource(tmp_path).load(None)
    assert res.chunks == [] and any("no AtlasLoot tables" in line for line in res.log)
