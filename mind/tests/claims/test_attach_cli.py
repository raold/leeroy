import json
import logging
from pathlib import Path

import pytest

from leeroy_mind.__main__ import main
from leeroy_mind.claims.cli import claims_main
from leeroy_mind.claims.store import live_claims
from leeroy_mind.testing.fake_llm import FakeLLM
from selfies.fakes import mind_app

from .conftest import say
from .test_store import put


@pytest.mark.parametrize('toml,on', [('', False), ('[claims]\nenabled=false', False),
    ('[claims]\nenabled=true', True), ('[claims]\nenabled="SECRET_NAME"', False),
    ('claims=[]', False), ('[claims]\nenabled=true\nunknown="SECRET_NAME"', False)])
def test_attach(tmp_path, caplog, toml, on):
    with caplog.at_level(logging.INFO, logger='leeroy_mind'):
        app = mind_app(tmp_path, toml=toml)
    try:
        names = [s.name for s in app.night_runner.stages]
        assert ('claims' in names) is on
        if on:
            at = names.index('claims')
            assert names[at - 1] == 'notes' and app.night_runner.stages[at].model == 'reflection'
        assert app.claims.enabled is on
        assert 'SECRET_NAME' not in caplog.text
    finally:
        app.store.close()


def test_read_commands_never_migrate_or_write(store, context, tmp_path, capsys):
    (tmp_path / 'mind.toml').write_text('', encoding='utf-8')
    put(store)
    assert main(['claims', '--data-dir', str(tmp_path), 'status']) == 0
    report = json.loads(capsys.readouterr().out)
    assert report['by_kind']['opinion'] == 1 and report['by_member']['101']['opinion'] == 1
    assert claims_main(['list', '101', '--data-dir', str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out)[0]['text'] == 'loves the Saurfang fight'
    fake = FakeLLM()
    assert claims_main(['check', '101', 'I hate Saurfang', '--data-dir', str(tmp_path)],
                       make_llm=lambda *_: fake) == 0
    assert json.loads(capsys.readouterr().out)[0]['reason'] == 'polarity'
    assert len(fake.calls) == 1 and 'input' in fake.calls[0]
    assert len(live_claims(store.conn, 101)) == 1


def test_eval_fake_uses_scratch_and_replays_in_order(store, context, tmp_path, capsys):
    (tmp_path / 'mind.toml').write_text('', encoding='utf-8')
    put(store, text='hates the Saurfang fight', polarity=-1)  # future state must not seed replay
    say(store, 101, 'I love Saurfang')
    second = say(store, 101, 'I hate Saurfang')
    say(store, 102, 'I love tea')
    say(store, 103, 'I work at the bakery')
    say(store, 1, 'I hate tea', origin='human')
    assert claims_main(['eval', '--fake', '--data-dir', str(tmp_path)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report['fake'] and report['members'] == 3 and report['lines'] == 4
    assert report['flagged'] == 1 and report['conflicts'][0]['line_id'] == second
    assert Path(report['scratch']).parent == tmp_path / 'scratch'
    assert live_claims(store.conn, 101)[0].text == 'hates the Saurfang fight'
    assert live_claims(store.conn, 102) == []


def test_old_schema_status_does_not_migrate(tmp_path, monkeypatch, capsys):
    from test_migrations import db_before, migration_number
    import sqlite3
    path = tmp_path / 'mind.db'
    db_before('claims', path, tmp_path, monkeypatch).close()
    (tmp_path / 'mind.toml').write_text('', encoding='utf-8')
    assert claims_main(['status', '--data-dir', str(tmp_path)]) == 1
    assert json.loads(capsys.readouterr().out)['reason'] == 'claims_schema_missing'
    with sqlite3.connect(path) as conn:
        assert conn.execute('PRAGMA user_version').fetchone()[0] == migration_number('claims') - 1
