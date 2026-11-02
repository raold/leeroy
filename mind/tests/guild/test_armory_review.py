"""Independent armory boundaries; every subprocess and SQL result is synthetic."""

import re
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from leeroy_mind.guild import armory as armory_mod
from leeroy_mind.guild.armory import MysqlArmory, read_premade_specs
from leeroy_mind.guild.roles import premade_spec_no
from leeroy_mind.guild.upgrades import ItemInfo, upgrade_gain
from leeroy_mind.personas import roster
from leeroy_mind.personas.roster import DbInfo, RosterError


def _db(name="synthetic_characters", password="synthetic-secret"):
    return DbInfo("synthetic-host", 3306, "synthetic-user", password, name)


def _armory():
    return MysqlArmory(_db(), _db("synthetic_world"), Path("synthetic-mysql.exe"))


def _item_row(entry):
    return [str(entry), "Synthetic item", "4", "264", "11", "4", "0", "-1",
            "7", "NULL", *(["0"] * 8)]


def test_review_item_batches_are_bounded_and_foreign_rows_do_not_poison_cache(monkeypatch):
    calls = []

    def query(db, sql, executable):
        assert db.name == "synthetic_world" and sql.startswith("SELECT ")
        values = tuple(int(value) for value in re.search(r"IN \(([0-9,]+)\)$", sql)[1].split(","))
        calls.append(values)
        return [_item_row(value) for value in values] + [_item_row(9999)]

    monkeypatch.setattr(armory_mod, "run_mysql", query)
    armory = _armory()
    requested = list(range(1, 502))
    assert set(armory.items(reversed(requested))) == set(requested)
    assert [len(batch) for batch in calls] == [500, 1]
    assert tuple(value for batch in calls for value in batch) == tuple(requested)
    assert set(armory.items(requested + requested)) == set(requested)
    assert len(calls) == 2
    assert set(armory.items([9999])) == {9999}
    assert calls[-1] == (9999,)


@pytest.mark.parametrize("method", ["items", "equipped"])
def test_review_invalid_tail_of_large_generator_never_reaches_mysql(monkeypatch, method):
    def forbidden(*args):
        pytest.fail("an invalid identifier reached the query boundary")

    monkeypatch.setattr(armory_mod, "run_mysql", forbidden)

    def requested():
        yield from range(1, 502)
        yield True

    with pytest.raises(ValueError, match="uint32"):
        getattr(_armory(), method)(requested())


def test_review_uint32_endpoint_is_quoted_as_a_number_and_equipment_is_owner_filtered(monkeypatch):
    maximum = 0xFFFFFFFF
    calls = []

    def query(db, sql, executable):
        calls.append(sql)
        assert sql.startswith("SELECT ") and ";" not in sql
        if "FROM character_inventory" in sql:
            assert f"ci.guid IN ({maximum})" in sql
            return [[str(maximum), "10", str(maximum)], ["101", "10", str(maximum)]]
        if "FROM item_template" in sql:
            assert f"entry IN ({maximum})" in sql
            return [_item_row(maximum)]
        if "MAX(quest)" in sql:
            assert f"guid = {maximum}" in sql
            return [["66018"]]
        assert f"WHERE guid = {maximum}" in sql
        return [["Syntheticguard"]]

    monkeypatch.setattr(armory_mod, "run_mysql", query)
    armory = _armory()
    assert armory.equipped([maximum]) == {maximum: {10: 264}}
    assert armory.human_tier(maximum) == 18
    assert armory.name(maximum) == "Syntheticguard"
    assert len(calls) == 4


@pytest.mark.parametrize(("rows", "expected"), [
    ([["NULL"]], 0), ([["66001"]], 1), ([["66018"]], 18),
])
def test_review_progression_endpoints(monkeypatch, rows, expected):
    monkeypatch.setattr(armory_mod, "run_mysql", lambda *args: rows)
    assert _armory().human_tier(101) == expected


@pytest.mark.parametrize("rows", [
    [["66000"]], [["66019"]], [["66016.0"]], [["66016", "66017"]],
    [["66016"], ["66017"]], [["private-looking-invalid-result"]],
])
def test_review_malformed_progression_fails_without_echoing_sql_results(monkeypatch, rows):
    monkeypatch.setattr(armory_mod, "run_mysql", lambda *args: rows)
    with pytest.raises(RosterError, match="tier result: invalid columns") as error:
        _armory().human_tier(101)
    assert "private-looking" not in str(error.value)


def test_review_sparse_premade_indices_are_not_sorted_by_spec_or_collapsed(tmp_path):
    conf = tmp_path / "playerbots.conf"
    conf.write_text('''Playerbots.PremadeSpecName.9.9 = "affli pve"
Playerbots.PremadeSpecName.9.3 = "demo pve"
Playerbots.PremadeSpecName.9.7 = "destro pve"
Playerbots.PremadeSpecName.11.8 = "cat pve"
Playerbots.PremadeSpecName.11.1 = "bear pvp"
Playerbots.PremadeSpecName.11.4 = "bear pve" # usable tank build
Playerbots.PremadeSpecName.11.2 = "resto pve"
Playerbots.PremadeSpecName.11.10 = "bear pve"
Playerbots.PremadeSpecName.9.1 = "demo pve" trailing garbage
''', encoding="utf-8")
    premades = read_premade_specs(conf)
    assert len(premades[9]) == 10 and premades[9][1] == ""
    assert [premade_spec_no(9, name, "ranged", premades) for name in (
        "Affliction", "Demonology", "Destruction")] == [9, 3, 7]
    assert premade_spec_no(11, "Feral", "tank", premades) == 4
    assert premade_spec_no(11, "Feral", "melee", premades) == 8
    assert premade_spec_no(11, "Restoration", "healer", premades) == 2


def test_review_weapon_comparison_respects_fixed_and_dual_slots():
    weapon = ItemInfo(1, "Synthetic sword", 4, 264, 13, 2, 7, -1, frozenset({4}))
    equipment = {15: 277, 16: 251, 17: 200}
    assert upgrade_gain(weapon, 1, "str", equipment) == 13
    assert upgrade_gain(replace(weapon, inventory_type=21), 1, "str", equipment) is None
    assert upgrade_gain(replace(weapon, inventory_type=17, item_subclass=8),
                        1, "str", equipment) is None
    assert upgrade_gain(replace(weapon, inventory_type=22), 1, "str", equipment) == 13
    assert upgrade_gain(weapon, 1, "str", {15: 277}) == 64
    assert equipment == {15: 277, 16: 251, 17: 200}


def test_review_token_mask_precedes_gain_and_missing_slots_have_defined_baseline():
    token = ItemInfo(2, "Synthetic token", 4, 264, 0, 15, 0, 1 << 7, frozenset())
    assert upgrade_gain(token, 8, "int", {}) == 64
    assert upgrade_gain(token, 1, "str", {}) is None
    assert upgrade_gain(token, 8, "int", {0: 263, 4: 264}) is None
    ring = ItemInfo(3, "Synthetic ring", 4, 264, 11, 4, 0, -1, frozenset({7}))
    assert upgrade_gain(ring, 8, "int", {10: 264}) == 64
    assert upgrade_gain(ring, 8, "int", {10: 264, 11: 264}) is None


def test_review_roster_still_delegates_and_keeps_old_member_defaults(monkeypatch):
    calls = []
    rows = [["101", "Syntheticguard", "2", "1", "1", "80", "3", "0", "1", "1637"],
            ["102", "Syntheticmage", "5", "8", "0", "80", "1", "1", "571", "4395"]]

    def query(db, sql, executable):
        calls.append((db, sql, executable))
        return rows

    monkeypatch.setattr(roster, "run_mysql", query)
    db, exe = _db(), Path("synthetic-mysql.exe")
    members = roster.query_roster(db, 21, exe)
    assert members == roster.parse_roster_tsv("\n".join("\t".join(row) for row in rows))
    assert [member.spec_tab for member in members] == [-1, -1]
    assert [member.online for member in members] == [False, True]
    assert calls == [(db, roster.ROSTER_SQL.format(guild_id=21), exe)]


@pytest.mark.parametrize("guild_id", [True, "21", 0, -1, 2 ** 32])
def test_review_roster_invalid_id_never_reaches_shared_runner(monkeypatch, guild_id):
    def forbidden(*args):
        pytest.fail("invalid guild id reached mysql")

    monkeypatch.setattr(roster, "run_mysql", forbidden)
    with pytest.raises(RosterError, match="positive uint32"):
        roster.query_roster(_db(), guild_id, Path("synthetic-mysql.exe"))


def test_review_mysql_interrupt_cleans_owned_secret_file_and_propagates(monkeypatch):
    seen = []
    interrupt = KeyboardInterrupt()

    def interrupted(argv, **kwargs):
        option = Path(argv[1].split("=", 1)[1])
        assert option.is_file()
        seen.append(option)
        raise interrupt

    monkeypatch.setattr(roster.subprocess, "run", interrupted)
    with pytest.raises(KeyboardInterrupt) as error:
        roster.run_mysql(_db(), "SELECT 1", Path("synthetic-mysql.exe"))
    assert error.value is interrupt
    assert seen and all(not option.exists() and not option.parent.exists() for option in seen)


def test_review_mysql_failure_cannot_disclose_escaped_diagnostics(monkeypatch):
    secret = 'synthetic"secret\\tail'
    seen = []

    def failed(argv, **kwargs):
        assert secret not in " ".join(argv)
        option = Path(argv[1].split("=", 1)[1])
        seen.append(option)
        return subprocess.CompletedProcess(argv, 1, "synthetic diagnostic payload",
            r"error near synthetic\"secret\\tail and private query context")

    monkeypatch.setattr(roster.subprocess, "run", failed)
    with pytest.raises(RosterError) as error:
        roster.run_mysql(_db(password=secret), "SELECT 1", Path("synthetic-mysql.exe"))
    assert str(error.value) == "mysql failed (1)"
    assert seen and all(not option.exists() and not option.parent.exists() for option in seen)


def test_review_mysql_batch_rows_preserve_unicode_empty_fields_and_null_marker(monkeypatch):
    def completed(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, "101\t\tNULL\r\n102\tÉlodie\t0\r\n", "")

    monkeypatch.setattr(roster.subprocess, "run", completed)
    assert roster.run_mysql(_db(), "SELECT 1", Path("synthetic-mysql.exe")) == [
        ["101", "", "NULL"], ["102", "Élodie", "0"]]
