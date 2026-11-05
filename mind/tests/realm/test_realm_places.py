"""The realm's places: the same strict CSV the native module reads, and its capital zones."""
import math
from pathlib import Path
import re

import pytest

from leeroy_mind.realm import places
from leeroy_mind.realm.places import (ALLIANCE_CAPITALS, HORDE_CAPITALS, NEUTRAL_CITIES, Place,
                                      Places, PlacesError, load_places, places_path,
                                      trade_visible)

REPO = Path(__file__).resolve().parents[3]
NATIVE_H = REPO / "modules/mod-leeroy/src/Lib/LeeroyRealmCommands.h"
NATIVE_CPP = REPO / "modules/mod-leeroy/src/Lib/LeeroyRealmCommands.cpp"
HEADER = "name,map,zone,x,y,z,o,kind"
ROW = "org_bank,1,1637,1627.32,-4376.07,12.0576,3.4383,city"


def write(tmp_path, text, name="leeroy_places.csv"):
    path = tmp_path / name
    path.write_bytes(text.encode("utf-8") if isinstance(text, str) else text)
    return path


def test_places_parse_dist():
    loaded = load_places(places.DIST)
    assert len(loaded) == 20 and list(loaded)[0] == "org_bank"
    assert loaded["uc_bank"] == Place("uc_bank", 0, 1497, 1599.96, 240.65, -52.0596, 0.087266, "city")
    assert all(name == place.name for name, place in loaded.items())
    found = Places(loaded)
    assert [p.name for p in found.by_kind("city")] == ["org_bank", "org_ah", "uc_bank", "tb_bank",
                                                        "dal_bank"]
    assert [p.name for p in found.by_kind("city", zone=1497)] == ["uc_bank"]
    assert found.by_kind("photo") == []
    assert found.zone_of("tb_bank") == 1638 and found.zone_of("org_gate") == 14
    assert "dal_inn" in found and "near_human" not in found and len(found) == 20
    with pytest.raises(KeyError):
        found.zone_of("near_human")
    with pytest.raises(ValueError):
        found.by_kind("bank")


def test_capitals_match_native_header():
    header = NATIVE_H.read_text(encoding="utf-8")

    def native(name):
        m = re.search(rf"\b{name}\s*=\s*\{{([^}}]*)\}}", header)
        assert m is not None, name
        return tuple(re.findall(r'"([^"]*)"|(\d+)', m.group(1)))

    def numbers(name):
        return tuple(int(n) for _, n in native(name))

    assert HORDE_CAPITALS == numbers("kHordeCapitals")
    assert ALLIANCE_CAPITALS == numbers("kAllianceCapitals")
    assert NEUTRAL_CITIES == numbers("kNeutralCities")
    assert places.PLACE_KINDS == tuple(s for s, _ in native("kPlaceKinds"))
    sizes = re.search(r"kMaxCsvBytes\s*=\s*([^,]+),\s*kMaxCsvRows\s*=\s*(\d+),\s*"
                      r"kMaxCsvLineBytes\s*=\s*(\d+);", header)
    assert sizes is not None
    assert places.MAX_BYTES == math.prod(int(n) for n in sizes.group(1).split("*"))
    assert places.MAX_ROWS == int(sizes.group(2))
    assert places.MAX_LINE_BYTES == int(sizes.group(3))
    assert f'"{places.HEADER}"' in NATIVE_CPP.read_text(encoding="utf-8")


def test_trade_visible_ten_zones():
    cities = (*HORDE_CAPITALS, *ALLIANCE_CAPITALS, *NEUTRAL_CITIES)
    assert len(set(cities)) == 10
    assert all(trade_visible(zone) for zone in cities)
    assert not any(trade_visible(zone) for zone in (0, 14, 33, 3711, 210, 4812))
    assert not trade_visible(True) and not trade_visible("1637")


def test_places_skip_blank_and_comment_lines(tmp_path):
    path = write(tmp_path, f"# local places\r\n\r\n  {HEADER}  \r\n# a note\r\n\t{ROW} \r\n")
    assert list(load_places(path)) == ["org_bank"]
    assert load_places(write(tmp_path, f"{HEADER}\n")) == {}
    assert load_places(write(tmp_path, f"{HEADER}\nx,0,1,1e3,-2.5E+2,.5,0,inn\n"))["x"].x == 1000.0


@pytest.mark.parametrize("rows, why", [
    ([], "missing header"),
    (["name,map,zone,x,y,z,o"], "invalid header"),
    (["﻿" + HEADER], "invalid header"),
    ([HEADER, ROW.replace("org_bank", '"org_bank"')], "invalid characters"),
    ([HEADER, ROW + "\0"], "invalid characters"),
    ([HEADER, ROW.rsplit(",", 1)[0]], "expected 8 columns"),
    ([HEADER, ROW + ",x"], "expected 8 columns"),
    ([HEADER, ROW, ROW], "duplicate name"),
    ([HEADER, ROW.replace("org_bank", "near_human")], "invalid name"),
    ([HEADER, ROW.replace("org_bank", "Org_bank")], "invalid name"),
    ([HEADER, ROW.replace("org_bank", "a" * 65)], "invalid name"),
    ([HEADER, ROW.replace(",1,1637,", ",1000,1637,")], "invalid map"),
    ([HEADER, ROW.replace(",1,1637,", ",+1,1637,")], "invalid map"),
    ([HEADER, ROW.replace(",1637,", ",0,")], "invalid zone"),
    ([HEADER, ROW.replace(",1637,", ",65536,")], "invalid zone"),
    ([HEADER, ROW.replace("1627.32", "20001")], "invalid coordinates"),
    ([HEADER, ROW.replace("1627.32", "nan")], "invalid coordinates"),
    ([HEADER, ROW.replace("1627.32", "1_627")], "invalid coordinates"),
    ([HEADER, ROW.replace("3.4383", "6.3")], "invalid orientation"),
    ([HEADER, ROW.replace("3.4383", "-0.1")], "invalid orientation"),
    ([HEADER, ROW.replace(",city", ",bank")], "invalid kind"),
])
def test_places_are_as_strict_as_native(tmp_path, rows, why):
    with pytest.raises(PlacesError, match=why):
        load_places(write(tmp_path, "\n".join(rows) + "\n"))


def test_places_limits_match_native(tmp_path):
    with pytest.raises(PlacesError, match="line too long"):
        load_places(write(tmp_path, f"{HEADER}\n# {'x' * 1024}\n"))
    rows = [f"p{i},1,1637,0,0,0,0,city" for i in range(places.MAX_ROWS + 1)]
    with pytest.raises(PlacesError, match="too many rows"):
        load_places(write(tmp_path, "\n".join([HEADER, *rows])))
    with pytest.raises(PlacesError, match="file too large"):
        load_places(write(tmp_path, HEADER + "\n" + "#" * places.MAX_BYTES))
    with pytest.raises(PlacesError, match="unavailable"):
        load_places(tmp_path / "missing.csv")


def test_places_path_prefers_the_live_configs(tmp_path):
    conf = tmp_path / "configs" / "worldserver.conf"
    assert places_path(conf) == places.DIST and places_path(None) == places.DIST
    live = write(tmp_path, f"{HEADER}\n{ROW}\n")
    (tmp_path / "configs" / "modules").mkdir(parents=True)
    moved = live.rename(tmp_path / "configs" / "modules" / "leeroy_places.csv")
    assert places_path(conf) == moved
    assert list(Places.load(places_path(conf))) == ["org_bank"]
