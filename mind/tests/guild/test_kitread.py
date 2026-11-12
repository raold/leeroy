import logging

import pytest

from leeroy_mind.guild.kitread import KIT, TARGETS, kit_sql, parse_kit, kit_view


def test_kit_sql_is_one_select_for_his_guid():
    sql = kit_sql(1)
    assert sql.startswith('SELECT ') and sql.count('SELECT') == 1 and ';' not in sql
    assert 'ci.guid = 1' in sql and 'ii.guid = ci.item' in sql
    assert all(str(item) in sql for item in KIT)
    for bad in (True,0,-1,2**32,'1 OR 1=1'):
        with pytest.raises(ValueError):
            kit_sql(bad)


def test_parse_kit_drops_malformed_rows(caplog):
    rows = [['46376','2'],['40211','8'],['46376','bad-private-value'],['43015','-1'],['9','1'],None,['41119']]
    assert parse_kit(rows,logging.getLogger('kit-test')) == {46376:2,40211:8}
    assert 'dropped=5' in caplog.text and 'bad-private-value' not in caplog.text


def test_kit_view_shortfalls_against_targets():
    view = kit_view({46376:1,46377:2,40211:8,43015:5,41119:7})
    groups = {g['kind']:g for g in view['groups']}
    assert {k:g['target'] for k,g in groups.items()} == TARGETS
    assert [groups[k]['short'] for k in TARGETS] == [0,2,5]
    assert groups['flask']['have'] == 3
    assert len(view['items']) == 5


def test_missing_character_does_not_become_an_empty_inventory(monkeypatch):
    from types import SimpleNamespace
    import threading
    from leeroy_mind.guild.ports import LazyArmory
    from leeroy_mind.personas import roster
    armory = object.__new__(LazyArmory)
    armory._lock = threading.RLock()
    armory._get = lambda:SimpleNamespace(chars=None)
    armory.cfg = SimpleNamespace(mysql_exe=None)
    calls = []
    def query(db,sql,exe):
        calls.append(sql)
        return []
    monkeypatch.setattr(roster,'run_mysql',query)
    assert armory.kit(1) is None
    assert calls == ['SELECT guid FROM characters WHERE guid=1']
    def found(db,sql,exe):
        calls.append(sql)
        return [['1']] if 'FROM characters ' in sql else [['46376','2']]
    monkeypatch.setattr(roster,'run_mysql',found)
    assert armory.kit(1)=={46376:2}
    assert all(sql.startswith('SELECT ') for sql in calls)
