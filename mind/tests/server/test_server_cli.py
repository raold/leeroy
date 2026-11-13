import hashlib
import json
import sqlite3
from datetime import datetime, timezone

from leeroy_mind.__main__ import main
from leeroy_mind.server.cli import status


def test_status_cli_reads_old_meta_only_database_without_migration(tmp_path, capsys):
    path = tmp_path / 'mind.db'
    with sqlite3.connect(path) as conn:
        conn.execute('CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT)')
        rows = [('server:recent', json.dumps([dict(channel='general',seed='purpose',messages=n,reason='length')
                                              for n in range(8)])),
                ('server:dm:open:101', '123.0'),
                ('server:day:2026-11-12', '{"messages":42,"conversations":6}')]
        conn.executemany('INSERT INTO meta VALUES (?,?)', rows)
    before = hashlib.sha256(path.read_bytes()).digest()
    result = status(tmp_path, local=datetime(2026,11,12,4,tzinfo=timezone.utc))
    assert result['config']['enabled'] is False
    assert result['window'] == dict(start='03:00',end='08:00',inside=True)
    assert result['counts'] == dict(messages=42,conversations=6,reactions=0,dms=0)
    assert [r['messages'] for r in result['recent']] == [7,6,5,4,3]
    assert result['open_dms'] == [dict(guid=101,opened=123.0)]
    assert main(['server','status','--data-dir',str(tmp_path),'--json']) == 0
    assert json.loads(capsys.readouterr().out)['recent'] == result['recent']
    assert main(['server','status','--data-dir',str(tmp_path)]) == 0
    assert 'Quiet window: 03:00-08:00' in capsys.readouterr().out
    assert hashlib.sha256(path.read_bytes()).digest() == before
    with sqlite3.connect(path) as conn:
        assert [r[0] for r in conn.execute('SELECT name FROM sqlite_master')] == ['meta','sqlite_autoindex_meta_1']


def test_missing_database_is_reported_and_never_created(tmp_path, capsys):
    assert main(['server','status','--data-dir',str(tmp_path),'--json']) == 1
    assert json.loads(capsys.readouterr().out)['error'] == 'server_status_unavailable'
    assert not (tmp_path/'mind.db').exists()


def test_enabled_config_and_invalid_config(tmp_path):
    with sqlite3.connect(tmp_path/'mind.db') as conn:
        conn.execute('CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT)')
    conf = tmp_path/'mind.toml'
    conf.write_text('[server]\nenabled=true\nquiet_window=["02:00","06:00"]\n')
    local = datetime(2026,11,12,7,tzinfo=timezone.utc)
    result = status(tmp_path,local=local)
    assert result['config']['enabled'] and not result['window']['inside']
    assert result['counts'] == dict(messages=0,conversations=0,reactions=0,dms=0)
    conf.write_text('[server]\nenabled=true\nbogus=true\n')
    assert not status(tmp_path,local=local)['config']['enabled']
