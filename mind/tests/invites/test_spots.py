"""The meeting spots: the mind's table is native's compiled one (LeeroyInviteSpots.h), a spot the
Realm's places whitelist also names has its exact coordinates, and his words resolve to them."""
from pathlib import Path
import re
import struct

from leeroy_mind.invites import spots as S
from leeroy_mind.realm.places import DIST, parse_places

HEADER = (Path(__file__).resolve().parents[3] / "modules" / "mod-leeroy" / "src" / "Lib"
          / "LeeroyInviteSpots.h")
_ROW = re.compile(r'\{"([a-z_]+)", (\d+), (\d+), (-?[\d.]+)f, (-?[\d.]+)f, (-?[\d.]+)f, ([\d.]+)f, '
                  r'(true|false)\}')


def f32(v: float) -> float:
    return struct.unpack("<f", struct.pack("<f", v))[0]


def test_the_table_is_natives():
    native = [(m[0], int(m[1]), int(m[2]), float(m[3]), float(m[4]), float(m[5]), float(m[6]),
               m[7] == "true") for m in _ROW.findall(HEADER.read_text(encoding="utf-8"))]
    ours = [(s.name, s.map, s.zone, s.x, s.y, s.z, s.o, s.fish) for s in S.SPOTS]
    assert native == ours
    assert f"std::array<InviteSpot, {len(S.SPOTS)}>" in HEADER.read_text(encoding="utf-8")


def test_shared_names_carry_the_places_coordinates():
    places = parse_places(DIST.read_bytes())
    shared = [s for s in S.SPOTS if s.name in places]
    assert {s.name for s in shared} == {"dal_fountain", "dal_inn", "dal_bank", "org_inn", "org_bank",
                                       "uc_bank", "tb_bank", "booty_bay_docks"}
    for s in shared:
        p = places[s.name]
        assert (p.map, p.zone) == (s.map, s.zone)
        assert [f32(v) for v in (p.x, p.y, p.z, p.o)] == [f32(v) for v in (s.x, s.y, s.z, s.o)]
        assert (p.kind == "fish") == s.fish


def test_every_city_names_a_default_spot_in_it():
    for zone, (words, default) in S.CITIES.items():
        assert words and S.BY_NAME[default].zone == zone
    assert len({s.name for s in S.SPOTS}) == len(S.SPOTS)
    assert all(s.label and s.kind for s in S.SPOTS)


def test_resolve():
    org, dal = 1637, 4395
    assert S.resolve("meet me at the inn", his_zone=org).name == "org_inn"
    assert S.resolve("meet me at the inn", his_zone=dal).name == "dal_inn"
    assert S.resolve("the bank in silvermoon", his_zone=org).name == "smc_bank"
    assert S.resolve("krasus landing", his_zone=org).name == "dal_krasus"
    assert S.resolve("let's go to dalaran", his_zone=org).name == "dal_krasus"
    assert S.resolve("fishing in dalaran", his_zone=org, fishing=True).name == "dal_fountain"
    assert S.resolve("fishing in org", his_zone=org, fishing=True) is None  # beside him
    assert S.resolve("fishing in org", his_zone=dal, fishing=True) is S.UNKNOWN
    assert S.resolve("at the auction house", his_zone=org) is S.UNKNOWN
    assert S.resolve("the inn", his_zone=0) is S.UNKNOWN
    assert S.resolve("come here", his_zone=org) is None
