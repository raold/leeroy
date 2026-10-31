import struct
import subprocess
from pathlib import Path

import pytest

from leeroy_mind.library import mysql as mysql_mod
from leeroy_mind.library import world as W
from leeroy_mind.library.__main__ import main
from leeroy_mind.library.dbc import ClientNames, Encounter, MapInfo, load_client_names
from leeroy_mind.library.mysql import QueryError, check_select, parse_tsv, query, unescape_field
from leeroy_mind.library.world import WorldSource, condition_text, quest_text, resolve_loot
from leeroy_mind.personas.roster import read_db_info

PASSWORD = "S3cret;Pw!"


def item_row(entry, name, quality, ilvl, inv=0, cls=15, sub=0, **kw):
    r = ["0"] * 47
    r[0], r[1], r[2], r[3], r[5], r[6], r[7] = str(entry), name, str(quality), str(ilvl), \
        str(inv), str(cls), str(sub)
    r[4] = str(kw.get("req", 80))
    r[8] = str(kw.get("classes", -1))
    r[9] = str(kw.get("bonding", 1))
    r[11], r[12], r[13] = kw.get("dmg", ("0", "0", "0"))
    r[14], r[15], r[16] = (str(x) for x in kw.get("sockets", (0, 0, 0)))
    r[18] = kw.get("desc", "")
    r[23], r[24] = str(kw.get("trigger", 0)), str(kw.get("spell", 0))
    for i, (st, val) in enumerate(kw.get("stats", ())):
        r[27 + 2 * i], r[28 + 2 * i] = str(st), str(val)
    return r


def quest_row(qid, title, level=80, sort=4812, info=89, desc="", logdesc="", prev=0, nxt=0,
              classes=0, req_items=(), req_npcs=(), rewards=(), races=0, flags=0):
    r = ["0"] * 46
    r[0], r[1], r[2], r[3], r[4], r[5] = str(qid), title, str(level), str(level), str(sort), \
        str(info)
    r[6], r[7] = str(flags), str(races)
    r[9], r[10] = logdesc, desc
    for i, it in enumerate(rewards):
        r[12 + i] = str(it)
    for i, (npc, cnt) in enumerate(req_npcs):
        r[22 + i], r[26 + i] = str(npc), str(cnt)
    for i, (it, cnt) in enumerate(req_items):
        r[30 + i], r[36 + i] = str(it), str(cnt)
    r[42], r[43], r[44], r[45] = str(prev), str(nxt), "0", str(classes)
    return r


ITEMS = [
    item_row(50415, "Bryntroll, the Bone Arbiter", 4, 264, inv=17, cls=2, sub=1,
             dmg=("801", "1203", "3400"), sockets=(2, 8, 0), stats=[(4, 169), (7, 161)],
             trigger=2, spell=71845),
    item_row(49426, "Emblem of Frost", 4, 80, cls=10),
    item_row(51026, "Crystalline Essence of Sindragosa", 1, 80, cls=12, bonding=4),
    item_row(49908, "Primordial Saronite", 4, 80, cls=7),
    item_row(47241, "Emblem of Triumph", 4, 80, cls=10),
    item_row(90001, "Broken Frost Bone", 0, 5),
    item_row(90002, "OLD Test Sword", 4, 200, inv=13, cls=2, sub=7),
    item_row(90003, "Dusty Grey Rag", 0, 1),
]
NAMES = ClientNames(
    maps={631: MapInfo(631, "Icecrown Citadel", 2, 2, 0)},
    sizes={(631, 0): 10, (631, 1): 25, (631, 2): 10, (631, 3): 25},
    areas={4812: "Icecrown Citadel"}, quest_infos={89: "Raid (25)"},
    encounters={855: Encounter(855, 631, 0, 10000, "Sindragosa"),
                867: Encounter(867, 631, 1, 10000, "Sindragosa")},
    extended_costs={2743: (2743, 0, 0, 0, 49426, 0, 0, 0, 0, 60, 0, 0, 0, 0, 0, 0)},
    loaded=["Map.dbc"])


def fake_world(sql: str):
    table = {
        W.SQL_ITEMS: ITEMS,
        W.SQL_INSTANCES: [["631"]],
        W.SQL_ENCOUNTERS: [["855", "0", "36853"], ["867", "0", "36853"]],
        W.SQL_SPAWNS: [["36853", "631"]],
        W.SQL_BOSSES: [["36853", "Sindragosa", "", "3", "36853", "38265", "0", "38267", "83"]],
        W.SQL_REFERENCE_LOOT: [["34237", "50415", "0", "0", "0", "1", "1", "1", "1"],
                               ["34237", "90001", "0", "0", "0", "1", "1", "1", "1"]],
        W.SQL_EVENTS: [["8", "Love is in the Air"]],
        W.SQL_CONDITIONS: [["1", "38267", "47241", "8", "66014", "0"]],
        W.SQL_CHESTS: [["201872", "Gunship Armory", "28057", "-1", "0"],
                       ["999", "Herb Node", "500", "571", "1"]],
        W.SQL_VENDORS: [["50415", "2743", "Rhydian", "Emblem Vendor"],
                        ["49426", "0", "Kalin Windflight", None]],
        W.SQL_QUESTS: [
            quest_row(24545, "The Sacred and the Corrupt", classes=35, nxt=24743,
                      logdesc="Place 25 Primordial Saronite in the runeforge.",
                      desc="Listen, $r.$B$BYou, $N, will need $Ghis:her; courage.",
                      req_items=[(49908, 25)], req_npcs=[(36853, 1)]),
            quest_row(24743, "Shadow's Edge", prev=24545, rewards=[50415]),
            quest_row(666, "<UNUSED> Placeholder"), quest_row(667, "DAILY TEST QUEST (RAID)"),
            quest_row(668, "HYJAL FLAG"), quest_row(669, "CLUCK!")],
        W.SQL_QUEST_ENDS: [["start", "24545", "Highlord Darion Mograine"],
                           ["end", "24545", "Runeforge"]],
    }
    if sql in table:
        return table[sql]
    if sql.startswith(W.SQL_LOOTIDS.split("{")[0]):
        return [["38265", "38265"], ["38267", "38267"]]
    if sql.startswith(W.SQL_CREATURE_LOOT.split("{")[0]):
        return [["36853", "1", "34237", "100", "0", "1", "0", "2", "2"],
                ["36853", "49426", "0", "100", "0", "1", "0", "2", "2"],
                ["36853", "51026", "0", "100", "1", "2", "0", "1", "1"],
                ["38267", "49908", "0", "38", "0", "1", "0", "1", "1"],
                ["38267", "47241", "0", "100", "0", "1", "0", "1", "1"]]
    if sql.startswith(W.SQL_CHEST_LOOT.split("{")[0]):
        return [["28057", "50415", "0", "100", "0", "1", "0", "1", "1"]]
    if sql.startswith(W.SQL_NPC_NAMES.split("{")[0]):
        return [["36853", "Sindragosa"]]
    if sql.startswith(W.SQL_GO_NAMES.split("{")[0]):
        return []
    raise AssertionError(f"unexpected SQL: {sql[:80]}")


@pytest.fixture
def world():
    res = WorldSource(run=fake_world, names=NAMES).load(None)
    return res, {c.key: c for c in res.chunks}


def test_boss_loot_text(world):
    _, by = world
    ten = by["loot:npc:36853:0"]
    assert (ten.title, ten.section, ten.instance, ten.map_id) == (
        "Sindragosa", "10-player", "Icecrown Citadel", 631)
    assert ten.text.startswith("What Sindragosa drops (Icecrown Citadel, 10-player loot table).")
    assert "Always: 2 x Emblem of Frost." in ten.text
    assert "2 items from: Bryntroll, the Bone Arbiter (264 two-hand axe), Broken Frost Bone." \
        in ten.text
    assert "Quest items: Crystalline Essence of Sindragosa." in ten.text
    assert "item:50415" in ten.ref_ids and "npc:36853" in ten.ref_ids
    heroic = by["loot:npc:36853:3"]
    assert heroic.section == "25-player heroic"
    assert "Primordial Saronite (38%)" in heroic.text
    assert "Emblem of Triumph (by progression tier)" in heroic.text
    assert "Always" not in heroic.text
    assert "loot:npc:36853:1" not in by  # a difficulty with an empty loot table
    gun = by["loot:go:201872"]
    assert gun.instance is None and "Bryntroll" in gun.text
    assert not any(k.startswith("loot:go:999") for k in by)  # a herb node in the open world


def test_progression_vanilla_raid_bosses_are_labelled_as_their_tier():
    """mod-individual-progression's 40-player Naxxramas and Onyxia are base entries on the Wrath
    maps (whose difficulty 0 is 10-player): its id range (351028, 301000) or a vanilla boss
    level gives them away; a boss elsewhere keeps its map's own size."""
    names = ClientNames(
        maps={533: MapInfo(533, "Naxxramas", 2, 2, 25),
              249: MapInfo(249, "Onyxia's Lair", 2, 0, 40),
              409: MapInfo(409, "Molten Core", 2, 0, 40)},
        sizes={(533, 0): 10, (533, 1): 25, (249, 0): 10, (249, 1): 25, (409, 0): 40})
    rows = {
        W.SQL_ITEMS: [item_row(22349, "Desecrated Breastplate", 4, 88, cls=15),
                      item_row(39272, "Drape of Surgery", 4, 200, inv=16, cls=4, sub=1)],
        W.SQL_INSTANCES: [["533"], ["249"], ["409"]],
        W.SQL_ENCOUNTERS: [],
        W.SQL_SPAWNS: [["16028", "533"], ["351028", "533"], ["301000", "249"], ["990001", "533"],
                       ["11502", "409"]],
        W.SQL_BOSSES: [["16028", "Patchwerk", "", "3", "16028", "29324", "0", "0", "83"],
                       ["351028", "Patchwerk", "", "3", "351028", "0", "0", "0", "63"],
                       ["301000", "Onyxia", "", "3", "301000", "0", "0", "0", "63"],
                       ["990001", "Old Abomination", "", "3", "990001", "0", "0", "0", "62"],
                       ["11502", "Ragnaros", "", "3", "11502", "0", "0", "0", "63"]],
        W.SQL_REFERENCE_LOOT: [], W.SQL_EVENTS: [], W.SQL_CONDITIONS: [], W.SQL_CHESTS: [],
        W.SQL_VENDORS: [], W.SQL_QUESTS: [], W.SQL_QUEST_ENDS: [],
    }

    def run(sql):
        if sql in rows:
            return rows[sql]
        if sql.startswith(W.SQL_LOOTIDS.split("{")[0]):
            return [["29324", "29324"]]
        if sql.startswith(W.SQL_CREATURE_LOOT.split("{")[0]):
            return [[e, "22349" if e in ("351028", "301000", "990001", "11502") else "39272",
                     "0", "100", "0", "1", "0", "1", "1"]
                    for e in ("16028", "29324", "351028", "301000", "990001", "11502")]
        return []

    res = WorldSource(run=run, names=names).load(None)
    by = {c.key: c for c in res.chunks}
    assert by["loot:npc:16028:0"].section == "10-player"
    assert by["loot:npc:16028:1"].section == "25-player"
    tier = W.VANILLA_TIER
    assert tier == "40-player (vanilla, mod-individual-progression tier)"
    assert by["loot:npc:351028:0"].section == tier
    assert by["loot:npc:351028:0"].text.startswith(
        f"What Patchwerk drops (Naxxramas, {tier} loot table).")
    assert by["loot:npc:301000:0"].section == tier       # ONYXIA_40, by its id
    assert by["loot:npc:990001:0"].section == tier       # a vanilla level on a Wrath raid map
    assert by["loot:npc:11502:0"].section == "40-player"  # Molten Core is a 40-player map anyway
    assert f"Drops from: Patchwerk (Naxxramas, {tier}); Onyxia" in by["item:22349"].text
    assert any("3 bosses are mod-individual-progression's vanilla raid tier" in line
               for line in res.log)


def test_item_chunks_cross_reference(world):
    res, by = world
    axe = by["item:50415"].text
    assert axe.startswith("Bryntroll, the Bone Arbiter: epic two-hand axe, item level 264, "
                          "requires level 80.")
    assert "801-1203 damage, speed 3.40" in axe and "+169 Strength, +161 Stamina" in axe
    assert "sockets: red, blue" in axe and "Has chance on hit effect." in axe
    assert "Drops from: Sindragosa (Icecrown Citadel, 10-player); Gunship Armory." in axe
    assert "Sold by Rhydian <Emblem Vendor> for 60 Emblem of Frost." in axe
    assert "Reward from the quest Shadow's Edge." in axe
    assert "item:90002" not in by  # placeholder name
    assert "item:90003" not in by and "item:90001" in by  # grey, kept only when it drops
    assert len(res.items) == len(ITEMS)
    assert "Sold by Kalin Windflight." in by["item:49426"].text  # a NULL subname adds nothing


def test_item_weights(world):
    res, _ = world
    w = {c.key: c.weight for c in res.chunks}
    assert w["item:50415"] == 1.0          # epic weapon
    assert w["item:49426"] == 0.9          # currency: an emblem
    assert w["item:49908"] == 0.7          # an epic trade good
    assert w["item:51026"] == 0.4          # a common quest item
    assert w["item:90001"] == 0.3          # grey
    assert all(c.weight == 1.0 for c in res.chunks if c.kind in ("loot", "zone"))
    assert all(c.weight == W.QUEST_WEIGHT for c in res.chunks if c.kind == "quest")


def test_quest_chunks_and_rows(world):
    res, by = world
    q = by["quest:24545"].text
    assert q.startswith("Quest The Sacred and the Corrupt (level 80, requires level 80, "
                        "Icecrown Citadel, raid (25), Warrior/Paladin/Death Knight only).")
    assert "Starts with Highlord Darion Mograine. Ends with Runeforge." in q
    assert "Needs: Sindragosa, 25 x Primordial Saronite." in q
    assert "Leads to: Shadow's Edge." in q
    assert "Text: Listen, <race>. You, <name>, will need his/her courage." in q
    assert "quest:666" not in by and "quest:667" not in by and "quest:668" not in by
    assert "quest:669" in by  # a real quest that shouts
    rows = {r.id: r for r in res.quests}
    assert rows[24545].next_id == 24743 and rows[24743].prev_id == 24545
    assert by["quest:24545"].section == "Icecrown Citadel"


def test_instance_overview(world):
    _, by = world
    assert by["instance:631"].text == (
        "Icecrown Citadel is a Wrath of the Lich King raid with 10-player, 25-player, "
        "10-player heroic, 25-player heroic modes. Its encounters, in order: Sindragosa.")


def test_helpers():
    assert quest_text("Hi $N the $C.$b$BBye $Gsir:madam;!") == "Hi <name> the <class>. Bye sir/madam!"
    assert condition_text(12, 8, False, {8: "Love is in the Air"}) == "during Love is in the Air"
    assert condition_text(1, 57940, False, {}) == "with Essence of Wintergrasp"
    assert condition_text(8, 66015, True, {}) == "by progression tier"
    rows = W._loot_rows([["1", "0", "2", "100", "0", "1", "0", "1", "3"],
                         ["2", "0", "1", "100", "0", "1", "0", "1", "1"],
                         ["2", "77", "0", "0", "0", "1", "1", "1", "1"]])
    lines = resolve_loot(rows[1], rows)  # 1 -> 2 -> 1 (a cycle) -> stops
    assert [(ln.item, ln.pool, ln.picks) for ln in lines] == [(77, 2, 3)]


def test_tsv_unescape():
    assert unescape_field("NULL") is None
    assert unescape_field(r"a\tb\nc\\d") == "a\tb\nc\\d"
    assert parse_tsv("1\tx\\ny\n2\tNULL\n") == [["1", "x\ny"], ["2", None]]


def test_tsv_rows_ending_in_crlf():
    """mysql.exe on Windows ends rows with CRLF: the last column must not keep the CR."""
    assert parse_tsv("855\tSindragosa\r\n856\tNULL\r\n") == [["855", "Sindragosa"],
                                                            ["856", None]]
    assert parse_tsv("1\ta\rb\r\n") == [["1", "a\rb"]]  # a CR inside a value stays
    assert parse_tsv("1\tNULL\r\n\r\n") == [["1", None]]


def _crlf_runner(conf, monkeypatch):
    """The real query() over a fake mysql.exe that answers fake_world in Windows' CRLF."""
    db = read_db_info(conf, "WorldDatabaseInfo")

    def fake_run(argv, **kw):
        rows = fake_world(kw["input"].decode("utf-8"))
        out = "".join("\t".join("NULL" if v is None else v.replace("\\", "\\\\") for v in r)
                      + "\r\n" for r in rows)
        return subprocess.CompletedProcess(argv, 0, out.encode("utf-8"), b"")

    monkeypatch.setattr(mysql_mod.subprocess, "run", fake_run)
    return lambda sql: query(db, sql, Path("mysql.exe"))


def test_world_source_through_crlf_mysql_output(conf, monkeypatch):
    res = WorldSource(run=_crlf_runner(conf, monkeypatch), names=NAMES).load(None)
    by = {c.key: c for c in res.chunks}
    assert "Starts with Highlord Darion Mograine. Ends with Runeforge." in by["quest:24545"].text
    assert "Sold by Kalin Windflight." in by["item:49426"].text
    assert "Sold by Rhydian <Emblem Vendor> for 60 Emblem of Frost." in by["item:50415"].text
    assert not any("\r" in c.text or "\r" in c.title or "<NULL>" in c.text for c in res.chunks)


# -- credentials -------------------------------------------------------------------------

@pytest.fixture
def conf(tmp_path):
    p = tmp_path / "worldserver.conf"
    p.write_text(f'WorldDatabaseInfo = "127.0.0.1;3306;acore;{PASSWORD};acore_world"\n'
                 f'DataDir = "{tmp_path / "nodata"}"\n', encoding="utf-8")
    return p


def test_query_keeps_the_password_in_a_deleted_option_file(conf, monkeypatch):
    db = read_db_info(conf, "WorldDatabaseInfo")
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"] = list(argv)
        seen["input"] = kw["input"]
        cnf = Path(argv[1].split("=", 1)[1])
        seen["cnf"] = cnf
        seen["text"] = cnf.read_text(encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, b"1\tSindragosa\n", b"")

    monkeypatch.setattr(mysql_mod.subprocess, "run", fake_run)
    assert query(db, "SELECT 1, 'x'", Path("mysql.exe")) == [["1", "Sindragosa"]]
    assert not any(PASSWORD in a for a in seen["argv"]) and PASSWORD.encode() not in seen["input"]
    assert PASSWORD in seen["text"] and not seen["cnf"].exists()
    assert not seen["cnf"].parent.exists()
    assert PASSWORD not in repr(db) and PASSWORD not in repr(WorldSource(conf=conf))
    # read-only on the server, and a server-side limit: a killed client leaves no query behind
    assert ("--init-command=SET SESSION transaction_read_only = ON, "
            "SESSION max_execution_time = 30000") in seen["argv"]


def test_query_errors_hide_the_password(conf, monkeypatch):
    db = read_db_info(conf, "WorldDatabaseInfo")
    monkeypatch.setattr(mysql_mod.subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(
        argv, 1, b"", f"Access denied (using password {PASSWORD})".encode()))
    with pytest.raises(QueryError) as e:
        query(db, "SELECT 1", Path("mysql.exe"))
    assert PASSWORD not in str(e.value) and "***" in str(e.value)
    with pytest.raises(QueryError):
        query(db, "DELETE FROM item_template", Path("mysql.exe"))


@pytest.mark.parametrize("sql", [
    "SELECT 1; DELETE FROM item_template",
    "SELECT 1;DROP TABLE x;",
    "SELECT 1 -- a comment",
    "SELECT 1 /* a comment */",
    "SELECT 1 # a comment",
    "SELECT '1;'; SELECT 2",
    "SELECT 1 \\! calc",
    "SELECT 'unterminated",
    "UPDATE item_template SET name = 'x'",
])
def test_only_one_plain_select_runs(sql, monkeypatch):
    monkeypatch.setattr(mysql_mod.subprocess, "run", lambda *a, **kw: pytest.fail("ran mysql"))
    with pytest.raises(QueryError):
        check_select(sql)


def test_quoted_semicolons_and_world_sql_pass():
    check_select("SELECT 'a;b', \"c;d -- e\", `f#g` FROM t WHERE x = 'it''s' AND y = 'a\\'b';")
    for sql in (W.SQL_BOSSES, W.SQL_QUEST_ENDS, W.SQL_CHESTS, W.SQL_ITEMS, W.SQL_QUESTS,
                W.SQL_VENDORS, W.SQL_CREATURE_LOOT.format(ids="1,2")):
        check_select(sql)


def test_cli_build_failure_prints_no_credentials(conf, tmp_path, capsys):
    rc = main(["--data-dir", str(tmp_path / "data"), "build", "--sources", "world", "--no-embed",
               "--conf", str(conf), "--mysql", str(tmp_path / "no-such-mysql.exe")])
    out = capsys.readouterr()
    assert rc == 1 and "build failed" in out.err
    assert PASSWORD not in out.out and PASSWORD not in out.err


def test_world_chunks_never_carry_the_password(world):
    res, _ = world
    assert not any(PASSWORD in c.text or PASSWORD in c.title for c in res.chunks)


# -- DBC ---------------------------------------------------------------------------------

def _wdbc(fields: int, records: list[list[int]], strings: bytes) -> bytes:
    body = b"".join(struct.pack(f"<{fields}I", *r) for r in records)
    return struct.pack("<4s4I", b"WDBC", len(records), fields, fields * 4, len(strings)) \
        + body + strings


def test_dbc_names(tmp_path):
    strings = b"\0IcecrownCitadel\0Icecrown Citadel\0"
    rec = [0] * 66
    rec[0], rec[1], rec[2], rec[5], rec[63], rec[65] = 631, 1, 2, 17, 2, 25
    (tmp_path / "Map.dbc").write_bytes(_wdbc(66, [rec], strings))
    (tmp_path / "QuestSort.dbc").write_bytes(_wdbc(5, [[1, 0, 0, 0, 0]], b"\0"))  # wrong layout
    (tmp_path / "AreaTable.dbc").write_bytes(b"not a dbc at all")
    names = load_client_names(tmp_path)
    assert names.maps[631] == MapInfo(631, "Icecrown Citadel", 2, 2, 25)
    assert names.loaded == ["Map.dbc"] and names.quest_sorts == {} and names.areas == {}
    assert load_client_names(None).loaded == []
