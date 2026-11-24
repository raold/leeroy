"""A member's talent tree from her talents (guild/talents.py) and the armory's read of them
(guild/armory.py parse_talents, MysqlArmory.spec_tabs). Synthetic DBC bytes and invented spells
only; nothing reads the server's files or its database."""
import struct

import pytest

from leeroy_mind.guild import talents as t
from leeroy_mind.guild.armory import MysqlArmory, parse_talents
from leeroy_mind.personas.roster import DbInfo, RosterError


def dbc(rows, fields):
    """A WDBC file of `rows` (each `fields` uint32s) and an empty string block."""
    head = struct.pack("<4s4I", b"WDBC", len(rows), fields, 4 * fields, 1)
    return head + b"".join(struct.pack(f"<{fields}I", *r) for r in rows) + b"\0"


def tab(tab_id, class_mask, page, pet=0):
    row = [0] * t.TAB_FIELDS
    row[0], row[20], row[21], row[22] = tab_id, class_mask, pet, page
    return row


def talent(talent_id, tab_id, *ranks):
    row = [0] * t.TALENT_FIELDS
    row[0], row[1] = talent_id, tab_id
    row[4:4 + len(ranks)] = ranks
    return row


WARRIOR, DRUID = 1, 1 << 10
TABS = dbc([tab(161, WARRIOR, 0), tab(164, WARRIOR, 1), tab(163, WARRIOR, 2),
            tab(283, DRUID, 0), tab(281, DRUID, 1), tab(282, DRUID, 2),
            tab(409, 0, 0, pet=1)], t.TAB_FIELDS)
TALENTS = dbc([talent(1, 161, 9001, 9002, 9003), talent(2, 164, 9011, 9012),
               talent(3, 163, 9021), talent(4, 283, 9101, 9102, 9103, 9104, 9105),
               talent(5, 282, 9201, 9202), talent(6, 409, 9301)], t.TALENT_FIELDS)


def test_tables_read_each_rank_spells_tree_and_points():
    table = t.tables(TALENTS, TABS)
    assert table[9003] == t.TalentSpell(WARRIOR, 0, 3)
    assert table[9105] == t.TalentSpell(DRUID, 0, 5) and table[9201].page == 2
    assert 9301 not in table  # a pet's tab


@pytest.mark.parametrize(("cls", "spells", "tree"), [
    (1, [9003, 9012], 0),  # 3 points in Arms, 2 in Fury
    (1, [9001, 9012], 1),
    (1, [9001, 9011], 0),  # a tie: the first page, as playerbots counts it
    (11, [9105, 9202], 0),
    (11, [9101, 9202], 2),
    (11, [9003], -1),  # a warrior's talent on a druid counts nothing
    (1, [], -1), (0, [9003], -1),
])
def test_her_tree_is_the_page_with_most_points(cls, spells, tree):
    assert t.spec_tab(cls, spells, t.tables(TALENTS, TABS)) == tree


def test_a_file_of_another_layout_is_refused():
    with pytest.raises(ValueError):
        t.tables(dbc([talent(1, 161, 9001)][:0], 5), TABS)
    with pytest.raises(ValueError):
        t.tables(b"WDBC", TABS)


def test_read_tables_and_the_data_dir(tmp_path):
    (tmp_path / "dbc").mkdir()
    (tmp_path / "dbc" / t.TALENT_FILE).write_bytes(TALENTS)
    (tmp_path / "dbc" / t.TAB_FILE).write_bytes(TABS)
    assert t.read_tables(tmp_path / "dbc")[9021] == t.TalentSpell(WARRIOR, 2, 1)
    conf = tmp_path / "worldserver.conf"
    conf.write_text('# DataDir = "nope"\nDataDir = "D:\\Game\\Data"\nLogsDir = ""\n',
                    encoding="utf-8")
    assert str(t.read_data_dir(conf)).replace("\\", "/") == "D:/Game/Data"
    conf.write_text("DataDir = \n", encoding="utf-8")
    assert t.read_data_dir(conf) is None


def test_the_armory_reads_her_active_talents(tmp_path, monkeypatch):
    (tmp_path / t.TALENT_FILE).write_bytes(TALENTS)
    (tmp_path / t.TAB_FILE).write_bytes(TABS)
    asked = []

    def run_mysql(db, sql, exe):
        asked.append(sql)
        return [["201", "1", "9003"], ["201", "1", "9012"], ["202", "11", "9202"],
                ["203", "11", "9301"]]
    monkeypatch.setattr("leeroy_mind.guild.armory.run_mysql", run_mysql)
    info = DbInfo("127.0.0.1", 3306, "u", "p", "acore_characters")
    armory = MysqlArmory(info, info, tmp_path / "mysql.exe", dbc_dir=tmp_path)
    assert armory.spec_tabs([202, 201, 203]) == {201: 0, 202: 2}  # 203: no points
    assert "activeTalentGroup" in asked[0] and "IN (201,202,203)" in asked[0]
    assert MysqlArmory(info, info, tmp_path / "mysql.exe").spec_tabs([201]) == {}  # no dbc dir


def test_talent_rows_are_checked():
    assert parse_talents([["7", "11", "9201"], ["7", "11", "9202"]]) == {7: (11, [9201, 9202])}
    with pytest.raises(RosterError):
        parse_talents([["7", "11"]])
    with pytest.raises(RosterError):
        parse_talents([["x", "11", "9201"]])
