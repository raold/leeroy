import subprocess
from pathlib import Path

import pytest

from leeroy_mind.guild import armory as module
from leeroy_mind.guild.armory import (
    MysqlArmory, average_item_level, parse_equipped, parse_items, read_premade_specs,
)
from leeroy_mind.guild.roles import premade_spec_no
from leeroy_mind.guild.upgrades import ItemInfo
from leeroy_mind.personas import roster
from leeroy_mind.personas.roster import DbInfo, RosterError, run_mysql


def item_row(entry=1001, level=264, name="Testplate"):
    return [str(entry), name, "4", str(level), "5", "4", "4", "-1", "4", "7", "NULL",
            "0", "0", "0", "0", "0", "0", "0"]


def databases():
    return (DbInfo("localhost", 3306, "synthetic", "s3cret", "test_characters"),
            DbInfo("localhost", 3306, "synthetic", "s3cret", "test_world"))


def test_average_item_level_excludes_shirt_tabard_and_bag_slots():
    assert average_item_level({0: 264, 3: 1, 18: 1, 4: 251}) == 257.5
    assert average_item_level({19: 1, 3: 1, 18: 1}) == 0.
    assert average_item_level({}) == 0.


def test_parse_items_and_equipped():
    items = parse_items([item_row(), item_row(1002, 251, "Testhelm")])
    assert items[1001] == ItemInfo(1001, "Testplate", 4, 264, 5, 4, 4, -1, frozenset({4, 7}))
    assert parse_equipped([["1101", "4", "1001"], ["1101", "0", "1002"],
                           ["1102", "4", "1002"], ["1103", "4", "9999"]], items) == {
        1101: {4: 264, 0: 251}, 1102: {4: 251}}


@pytest.mark.parametrize("rows", [[item_row()[:-1]], [["bad"] * 18],
                                   [["1001", "Secretlike"] + ["bad"] * 16]])
def test_bad_item_rows_fail_without_echoing_row_content(rows):
    with pytest.raises(RosterError, match="item row") as error:
        parse_items(rows)
    assert "Secretlike" not in str(error.value)


@pytest.mark.parametrize("row", [["1", "4"], ["1", "19", "1001"], ["true", "4", "1001"],
                                 ["1", "-1", "1001"], ["1", "4", "1.0"]])
def test_bad_equipment_rows_fail(row):
    with pytest.raises(RosterError, match="equipment row"):
        parse_equipped([row], {})


def test_read_premade_specs_preserves_sparse_indexes_and_pve_order(tmp_path):
    conf = tmp_path / "playerbots.conf"
    conf.write_text('''# Playerbots.PremadeSpecName.1.0 = ignored pve
Playerbots.Other = "unrelated"
Playerbots.PremadeSpecName.1.0 = "arms pve"
Playerbots.PremadeSpecName.1.2 = "prot pve" # trailing comment
Playerbots.PremadeSpecName.1.9 = arms pvp
Playerbots.PremadeSpecName.1.20 = invalid pve
Playerbots.PremadeSpecName.11.0 = "balance pve"
Playerbots.PremadeSpecName.11.1 = "bear pve"
Playerbots.PremadeSpecName.11.2 = "resto pve"
Playerbots.PremadeSpecName.11.3 = "cat pve"
Playerbots.PremadeSpecName.11.4 = "balance pvp"
Playerbots.PremadeSpecName.9.0 = "affli pve"
Playerbots.PremadeSpecName.9.1 = "demo pve"
Playerbots.PremadeSpecName.9.2 = "destro pve"
Playerbots.PremadeSpecName.8.0 = "unterminated
''', encoding="utf-8")
    specs = read_premade_specs(conf)
    assert specs[1] == ["arms pve", "", "prot pve", "", "", "", "", "", "", "arms pvp"]
    assert specs[9] == ["affli pve", "demo pve", "destro pve"]
    assert premade_spec_no(1, "Protection", "tank", specs) == 2
    assert premade_spec_no(11, "Feral", "melee", specs) == 3
    assert premade_spec_no(11, "Restoration", "healer", specs) == 2
    assert 8 not in specs


def test_mysql_armory_cache_database_routing_and_fresh_equipment(monkeypatch):
    chars, world = databases()
    calls = []
    equipped_round = 0
    def query(db, sql, exe):
        nonlocal equipped_round
        calls.append((db.name, sql))
        assert sql.startswith("SELECT ")
        if "FROM item_template" in sql:
            assert db is world and "stat_type10" in sql
            return [item_row()]
        if "FROM character_inventory" in sql:
            assert db is chars and "ci.bag = 0" in sql and "ci.slot < 19" in sql
            equipped_round += 1
            return [["1101", "4", "1001"]] if equipped_round == 1 else []
        if "MAX(quest)" in sql:
            assert "BETWEEN 66001 AND 66018" in sql
            return [["66016"]]
        if "FROM characters" in sql:
            return [["Testguard"]]
        raise AssertionError("unexpected query")
    monkeypatch.setattr(module, "run_mysql", query)
    armory = MysqlArmory(chars, world, Path("mysql.exe"))
    assert armory.items([]) == {} and armory.equipped([]) == {} and calls == []
    assert armory.items([1001, 1001])[1001].item_level == 264
    assert armory.items([1001])[1001].item_level == 264
    assert len(calls) == 1
    assert armory.equipped([1102, 1101]) == {1101: {4: 264}, 1102: {}}
    assert armory.equipped([1101]) == {1101: {}}
    assert armory.human_tier(1) == 16 and armory.name(1101) == "Testguard"
    assert len([sql for _, sql in calls if "FROM item_template" in sql]) == 1


@pytest.mark.parametrize("invalid", [True, 0, -1, 1.0, "1", "1); DROP TABLE characters; --", 2 ** 32])
def test_identifiers_are_validated_before_any_query(monkeypatch, invalid):
    def forbidden(*args):
        pytest.fail("invalid input reached mysql")
    monkeypatch.setattr(module, "run_mysql", forbidden)
    armory = MysqlArmory(*databases(), Path("mysql.exe"))
    for call in (lambda: armory.items([1001, invalid]), lambda: armory.equipped([1101, invalid]),
                 lambda: armory.human_tier(invalid), lambda: armory.name(invalid)):
        with pytest.raises(ValueError, match="uint32"):
            call()


def test_unknown_tier_and_character(monkeypatch):
    armory = MysqlArmory(*databases(), Path("mysql.exe"))
    monkeypatch.setattr(module, "run_mysql", lambda *a: [["NULL"]])
    assert armory.human_tier(1) == 0
    monkeypatch.setattr(module, "run_mysql", lambda *a: [])
    assert armory.human_tier(1) == 0 and armory.name(1101) is None
    monkeypatch.setattr(module, "run_mysql", lambda *a: [["99999"]])
    with pytest.raises(RosterError, match="tier"):
        armory.human_tier(1)


def test_run_mysql_password_only_in_private_temporary_file(monkeypatch):
    db = databases()[0]
    seen = {}
    def fake_run(argv, **kwargs):
        assert db.password not in " ".join(argv)
        assert kwargs["timeout"] == 60
        path = Path(argv[1].split("=", 1)[1])
        seen["path"] = path
        assert db.password in path.read_text(encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, "1001\tTestitem\tNULL\n\n", "")
    monkeypatch.setattr(roster.subprocess, "run", fake_run)
    assert run_mysql(db, "SELECT entry,name,stat_type1 FROM item_template", Path("mysql.exe")) == [
        ["1001", "Testitem", "NULL"]]
    assert not seen["path"].exists() and not seen["path"].parent.exists()


@pytest.mark.parametrize("failure", ["exit", "timeout", "missing"])
def test_run_mysql_failure_cleans_up_and_does_not_echo_credentials(monkeypatch, failure):
    db = databases()[0]
    paths = []
    def fake_run(argv, **kwargs):
        paths.append(Path(argv[1].split("=", 1)[1]))
        if failure == "timeout":
            raise subprocess.TimeoutExpired(argv, 60, output="s3cret")
        if failure == "missing":
            raise FileNotFoundError("s3cret")
        return subprocess.CompletedProcess(argv, 1, "s3cret", "escaped credential s3cret")
    monkeypatch.setattr(roster.subprocess, "run", fake_run)
    with pytest.raises(RosterError) as error:
        run_mysql(db, "SELECT 1", Path("mysql.exe"))
    assert "s3cret" not in str(error.value) and "s3cret" not in repr(error.value)
    assert paths and all(not p.exists() and not p.parent.exists() for p in paths)


def test_credentials_with_control_characters_stay_one_option_line(monkeypatch):
    db = DbInfo("localhost", 3306, "user", 'quoted"\\secret\npassword\r\t', "test_characters")
    def fake_run(argv, **kwargs):
        text = Path(argv[1].split("=", 1)[1]).read_text(encoding="utf-8")
        assert len(text.splitlines()) == 5
        assert '\\npassword\\r\\t' in text
        assert db.password not in " ".join(argv)
        return subprocess.CompletedProcess(argv, 0, "1\n", "")
    monkeypatch.setattr(roster.subprocess, "run", fake_run)
    assert run_mysql(db, "SELECT 1", Path("mysql.exe")) == [["1"]]


def test_fake_armory_keeps_returned_equipment_independent():
    from .fakes import FakeArmory
    items = parse_items([item_row()])
    armory = FakeArmory(items, {1101: {4: 264}})
    armory.names[1101] = "Testguard"
    assert armory.items([1001, 9999]) == items
    assert armory.equipped([1101, 1102]) == {1101: {4: 264}, 1102: {}}
    copy = armory.equipped([1101])
    copy[1101][4] = 1
    assert armory.equipped([1101])[1101][4] == 264
    assert armory.human_tier(1) == 16 and armory.name(1101) == "Testguard"
    assert armory.name(1199) is None


@pytest.mark.mysql
def test_live_armory():
    # Explicit opt-in only, matching the existing roster/mysql test policy.
    chars = roster.read_db_info(roster.DEFAULT_CONF, "CharacterDatabaseInfo")
    world = roster.read_db_info(roster.DEFAULT_CONF, "WorldDatabaseInfo")
    armory = MysqlArmory(chars, world, roster.DEFAULT_MYSQL)
    assert armory.human_tier(1) >= 16
    assert armory.equipped([1])[1]
    assert isinstance(armory.name(1), str) and armory.name(1)
