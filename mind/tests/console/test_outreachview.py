"""Counts survive old schemas and never read private outreach topics or references."""
import asyncio
import hashlib
import json
from pathlib import Path
import sqlite3

import pytest

from conftest import seed_test_cast
from console.rig import http, make_app
from console.test_console_bank import served
from leeroy_mind.console import outreachview, personaview, tonightview


NOW = 1_793_304_000.0
PRIVATE = 'UNSEEN_TOPIC_SENTINEL'


@pytest.fixture
def count_db(tmp_path):
    """A deliberately minimal schema proves the view needs no topic/source columns."""
    path = tmp_path / 'counts.db'
    conn = sqlite3.connect(path)
    conn.execute('CREATE TABLE outreach_item(guid INTEGER,kind TEXT,state TEXT,not_before REAL,'
                 'expires REAL,about TEXT,ref TEXT)')
    conn.executemany('INSERT INTO outreach_item VALUES (?,?,?,?,?,?,?)', [
        (101,'life','open',NOW-1,NOW+1,PRIVATE,PRIVATE),
        (101,'life','told',NOW-1,NOW+1,PRIVATE,PRIVATE),
        (101,'plan','dropped',NOW-1,NOW+1,PRIVATE,PRIVATE),
        (101,'followup','open',NOW+1,NOW+2,PRIVATE,PRIVATE),
        (101,'birthday','open',NOW-2,NOW,PRIVATE,PRIVATE),
        (102,'his_day','open',NOW,NOW+2,PRIVATE,PRIVATE),
    ])
    conn.commit()
    conn.close()
    yield path


def _readonly(path):
    conn = sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)
    def guard(action, table, column, *args):
        if action == sqlite3.SQLITE_READ and table == 'outreach_item' and column in {'about','ref'}:
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK
    conn.set_authorizer(guard)
    return conn


def test_member_counts_states_without_topics_or_other_members(count_db):
    before = hashlib.sha256(count_db.read_bytes()).digest()
    conn = _readonly(count_db)
    try:
        view = outreachview.member_view(conn,101)
        assert view == {'status':'ok','kinds':{
            'life':{'open':1,'told':1,'dropped':0},
            'plan':{'open':0,'told':0,'dropped':1},
            'followup':{'open':1,'told':0,'dropped':0},
            'birthday':{'open':1,'told':0,'dropped':0}},
            'totals':{'open':3,'told':1,'dropped':1}}
        assert PRIVATE not in json.dumps(view)
        assert outreachview.member_view(conn,105) == {
            'status':'ok','kinds':{},'totals':{'open':0,'told':0,'dropped':0}}
    finally:
        conn.close()
    assert hashlib.sha256(count_db.read_bytes()).digest() == before


def test_tonight_counts_only_open_ready_unexpired_rows(count_db,tmp_path):
    conn = _readonly(count_db)
    try:
        assert outreachview.tonight_view(conn,now=NOW) == {'status':'ok','count':2}
        result = tonightview.tonight_view(conn,tmp_path,now=NOW,status=None,kit=None,
                                          kit_why=None,outreach_enabled=True)
        assert result['sections']['outreach'] == {'status':'ok','count':2}
        assert result['sections']['waiting']['status'] == 'not_set_up'
        assert PRIVATE not in json.dumps(result)
    finally:
        conn.close()


@pytest.mark.parametrize('ddl', ['', 'CREATE TABLE outreach_item(id INTEGER,guid INTEGER)'])
def test_absent_or_older_table_is_not_a_zero(ddl):
    conn = sqlite3.connect(':memory:')
    try:
        if ddl:
            conn.execute(ddl)
        before = conn.total_changes
        expected = {'status':'not_set_up','why':'missing_source'}
        assert outreachview.member_view(conn,101) == expected
        assert outreachview.tonight_view(conn,now=NOW) == expected
        assert conn.total_changes == before
    finally:
        conn.close()


def test_read_failure_exposes_no_error_or_topic_text():
    class Broken:
        def execute(self,*args):
            raise sqlite3.OperationalError(PRIVATE)
    assert outreachview.member_view(Broken(),101) == {'status':'unavailable','why':'read_failed'}
    assert outreachview.tonight_view(Broken(),now=NOW) == {'status':'unavailable','why':'read_failed'}


def test_enabled_empty_table_is_zero_and_off_skips_outreach_reads(store,tmp_path):
    seed_test_cast(store)
    store.set_meta('human_guid','1')
    assert outreachview.tonight_view(store.conn,now=NOW) == {'status':'ok','count':0}
    enabled = personaview.member_view(store.conn,101,now=NOW,outreach_enabled=True)
    assert enabled['outreach']['totals'] == {'open':0,'told':0,'dropped':0}
    reads = []
    def guard(action,table,*args):
        if action == sqlite3.SQLITE_READ and table == 'outreach_item':
            reads.append(table)
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK
    store.conn.set_authorizer(guard)
    try:
        assert 'outreach' not in personaview.member_view(store.conn,101,now=NOW)
        view = tonightview.tonight_view(store.conn,tmp_path,now=NOW,status=None,kit=None,kit_why=None)
        assert tuple(view['sections']) == tonightview.SECTIONS
        assert not reads
    finally:
        store.conn.set_authorizer(None)


@pytest.mark.parametrize('text,enabled', [('',False),('[outreach]\nenabled="yes"',False),
                                        ('[outreach]\nenabled=true',True)])
def test_console_uses_startup_switch_and_keeps_existing_key_gates(store,text,enabled):
    seed_test_cast(store)
    store.set_meta('human_guid','1')
    data = Path(store.conn.execute('PRAGMA database_list').fetchone()['file']).parent
    path = data / 'mind.toml'
    path.write_text(text,encoding='utf-8')
    app = make_app(data,clock=lambda:NOW)
    path.write_text('[outreach]\nenabled='+('false' if enabled else 'true'),encoding='utf-8')
    async def go():
        async with served(app):
            for route in ('/api/persona?guid=101','/api/tonight'):
                assert (await http(app,'GET',route,key=False)).status == 403
            member = (await http(app,'GET','/api/persona?guid=101')).json()
            tonight = (await http(app,'GET','/api/tonight')).json()
            assert ('outreach' in member) is enabled
            assert ('outreach' in tonight['sections']) is enabled
            if enabled:
                assert member['outreach']['status'] == 'ok'
                assert tonight['sections']['outreach']['count'] == 0
            assert PRIVATE not in json.dumps([member,tonight])
    asyncio.run(go())
