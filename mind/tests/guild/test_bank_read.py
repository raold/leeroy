"""The guild bank read: four read-only SELECTs, the strict parse, and LazyArmory.bank. No MySQL:
run_mysql is patched; the rows are synthetic."""
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from leeroy_mind import endpoint
from leeroy_mind.guild import bankread
from leeroy_mind.guild.config import GuildConfig
from leeroy_mind.guild.ports import LazyArmory
from leeroy_mind.library import mysql as library_mysql
from leeroy_mind.personas import roster

MONEY = [["1234567"]]
TABS = [["0", "Consumables"], ["1", ""]]
ITEMS = [["0", "0", "2589", "20"], ["1", "97", "49623", "1"], ["0", "14", "4306", "5"]]
LOG = [["1793304000", "4", "1101", "50000"], ["1793303000", "5", "1102", "10000"]]


def test_the_bank_sql_is_four_read_only_selects_for_one_guild():
    sqls = bankread.bank_sql(21)
    assert len(sqls) == 4
    for sql in sqls:
        library_mysql.check_select(sql)
        assert "21" in sql
    assert "TabId=100" in sqls[3] and "LIMIT 50" in sqls[3]
    for bad in ("21", 0, 2 ** 32, True, -1, 1.0):
        with pytest.raises(ValueError):
            bankread.bank_sql(bad)
    assert "4294967295" in bankread.bank_sql(2 ** 32 - 1)[0]


def test_parse_bank_lays_out_tabs_slots_money_and_log():
    b = bankread.parse_bank(MONEY, TABS, ITEMS, LOG)
    assert b["money"] == 1234567
    assert [t["tab"] for t in b["tabs"]] == [0, 1]
    assert b["tabs"][0]["name"] == "Consumables" and b["tabs"][1]["name"] == ""
    assert b["tabs"][0]["slots"] == [[0, 2589, 20], [14, 4306, 5]]
    assert b["tabs"][1]["slots"] == [[97, 49623, 1]]
    assert b["log"] == [[1793304000, 4, 1101, 50000], [1793303000, 5, 1102, 10000]]


def test_a_tab_row_with_only_its_id_has_an_empty_name():
    assert bankread.parse_bank(MONEY, [["2"]], [], [])["tabs"][0] == {"tab": 2, "name": "", "slots": []}


def test_parse_bank_refuses_a_bad_row():
    bad_items = [[["0", "98", "1", "1"]], [["6", "0", "1", "1"]], [["0", "3", "1", "1"], ["0", "3", "2", "1"]],
                 [["0", "12a", "1", "1"]], [["0", "1", "1"]], [["0", "1", "-1", "1"]]]
    for items in bad_items:
        with pytest.raises(ValueError):
            bankread.parse_bank(MONEY, TABS, items, [])
    for money in ([], [["12a"]], [["1", "2"]], [[""]]):
        with pytest.raises(ValueError):
            bankread.parse_bank(money, TABS, [], [])
    with pytest.raises(ValueError):
        bankread.parse_bank(MONEY, [["6", "x"]], [], [])
    with pytest.raises(ValueError):
        bankread.parse_bank(MONEY, [["0", "a"], ["0", "b"]], [], [])
    with pytest.raises(ValueError):
        bankread.parse_bank(MONEY, TABS, [], [["1", "x", "3", "4"]])


def test_a_tab_name_is_cut_and_stripped_of_control_characters():
    b = bankread.parse_bank(MONEY, [["0", "A\x07" + "b" * 80]], [], [])
    assert b["tabs"][0]["name"] == "A" + "b" * (bankread.NAME_MAX - 1)


def test_six_full_tabs_fit_in_one_reply():
    items = [[str(t), str(s), "4294967295", "1000"] for t in range(6) for s in range(98)]
    tabs = [[str(t), "T" * 32] for t in range(6)]
    log = [["4294967295", "9", "4294967295", "99999999999"]] * 50
    b = bankread.parse_bank([["99999999999999"]], tabs, items, log)
    reply = {"ok": True, "read_at": 1793304000.123456, "guild": 4294967295, **b}
    assert len(endpoint.encode(reply)) <= endpoint.MAX_REPLY


def _armory(monkeypatch, run):
    monkeypatch.setattr(roster, "run_mysql", run)
    a = LazyArmory(replace(GuildConfig(), worldserver_conf=Path(__file__)))
    a._backend = SimpleNamespace(chars="db")
    return a


def test_lazy_armory_bank_runs_the_four_queries_and_parses(monkeypatch):
    seen = []

    def run(db, sql, exe):
        seen.append(sql)
        return [MONEY, TABS, ITEMS, LOG][len(seen) - 1]
    b = _armory(monkeypatch, run).bank(21)
    assert len(seen) == 4 and b["money"] == 1234567 and b["tabs"][0]["slots"][0] == [0, 2589, 20]


def test_a_mysql_failure_names_no_secret(monkeypatch):
    def run(db, sql, exe):
        raise library_mysql.QueryError("access denied for hunter2")
    with pytest.raises(library_mysql.QueryError):
        _armory(monkeypatch, run).bank(21)
    assert "hunter2" not in json.dumps(bankread.bank_sql(21))
