from contextlib import closing
from dataclasses import replace
import json
import logging
from pathlib import Path
import sqlite3

import pytest

from claims.conftest import context  # noqa: F401 - synthetic identities only
from leeroy_mind.__main__ import main
from leeroy_mind.eval.talk import cli as talk_cli
from leeroy_mind.nightly import cli as nightly_cli

from .test_detectors import insert, line


@pytest.fixture
def data(store, context, clock, tmp_path, monkeypatch):
    (tmp_path / 'mind.toml').write_text('', encoding='utf-8')
    monkeypatch.setattr(nightly_cli, 'SystemClock', lambda: clock)
    own = [line(1, 'I love Saurfang', ts=clock.now() - 20),
           line(2, 'I hate Saurfang', ts=clock.now() - 10),
           line(3, 'SYNTHETIC_PRIVATE_LINE_TOKEN is an invented private sentence', guid=102, ts=clock.now() - 5)]
    insert(store, own)
    return tmp_path


def invoke(data, *args):
    return main(['eval', 'talk', '--data-dir', str(data), '--fake', *args])


def test_fake_end_to_end_source_is_unchanged_and_report_has_no_text(data, store, capsys, caplog):
    source = data / 'mind.db'
    before = source.read_bytes(), source.stat().st_mtime_ns
    rows = list(store.conn.execute('SELECT * FROM chat_line'))
    version = store.conn.execute('PRAGMA user_version').fetchone()[0]
    with caplog.at_level(logging.DEBUG):
        assert invoke(data, '--json', 'result.json') == 0
    report = json.loads((data / 'scratch/result.json').read_text(encoding='utf-8'))
    assert report['fake'] and report['overall']['lines'] == 3
    assert report['detectors']['contradiction']['flagged_lines'] == 1
    assert report['detectors']['contradiction']['per_100_lines'] == 33.333333
    assert before == (source.read_bytes(), source.stat().st_mtime_ns)
    assert rows == list(store.conn.execute('SELECT * FROM chat_line'))
    assert store.conn.execute('PRAGMA user_version').fetchone()[0] == version
    assert store.conn.execute('SELECT count(*) FROM member_claim').fetchone()[0] == 0
    output = capsys.readouterr().out + caplog.text
    assert 'SYNTHETIC_PRIVATE_LINE_TOKEN' not in output
    assert 'Saurfang' not in output
    assert 'Saurfang' not in json.dumps(report)
    assert 'excerpt' not in json.dumps(report)
    assert list((data / 'scratch').glob('mind-talk-eval-*.db'))


def test_baseline_and_report_path_confinement(data, capsys):
    assert invoke(data, '--json', 'before.json') == 0
    baseline = data / 'scratch/before.json'
    assert invoke(data, '--baseline', str(baseline), '--json', 'after.json') == 0
    after = json.loads((data / 'scratch/after.json').read_text(encoding='utf-8'))
    assert all(row['delta_per_100_lines'] == 0 for row in after['baseline_changes'].values())
    before = baseline.read_bytes()
    assert invoke(data, '--json', 'before.json') == 1
    assert baseline.read_bytes() == before
    assert invoke(data, '--json', '../outside.json') == 1
    assert not (data / 'outside.json').exists()
    assert invoke(data, '--json', str(data.parent / 'outside.json')) == 1
    assert 'Saurfang' not in capsys.readouterr().out


def test_optional_excerpts_stay_in_explicit_scratch_report(data, capsys, caplog):
    assert invoke(data, '--show-text') == 1
    assert invoke(data, '--show-text', '--json', 'private.json') == 0
    report = json.loads((data / 'scratch/private.json').read_text(encoding='utf-8'))
    assert report['detectors']['contradiction']['hits'][0]['excerpt'] == 'I hate Saurfang'
    assert 'I hate Saurfang' not in capsys.readouterr().out + caplog.text


def test_invalid_config_and_dependency_errors_do_not_expose_private_content(data, capsys, caplog, monkeypatch):
    (data / 'mind.toml').write_text('[SYNTHETIC_PRIVATE_SECTION]\nname="PRIVATE_NAME"\n', encoding='utf-8')
    with caplog.at_level(logging.DEBUG):
        assert invoke(data) == 0
    assert 'SYNTHETIC_PRIVATE_SECTION' not in caplog.text

    async def failed(*_args, **_kw):
        logging.getLogger('leeroy_mind.dependency').warning('SYNTHETIC_PRIVATE_ERROR')
        raise ValueError('SYNTHETIC_PRIVATE_EXCEPTION')

    monkeypatch.setattr(talk_cli, 'evaluate', failed)
    assert invoke(data) == 1
    captured = capsys.readouterr()
    assert 'SYNTHETIC_PRIVATE' not in captured.out + captured.err + caplog.text
    assert 'code=ValueError' in captured.err


def test_live_switches_and_fake_judge_cannot_reach_evaluation(data, monkeypatch):
    def forbidden(*_args, **_kw):
        raise AssertionError('evaluation must not start')

    monkeypatch.setattr(talk_cli, 'evaluate', forbidden)
    assert invoke(data, '--judge') == 1
    with pytest.raises(SystemExit):
        main(['eval', '--live-db', 'talk', '--data-dir', str(data)])


def test_since_window_and_parent_context_never_include_future_lines(data, store, clock):
    insert(store, [line(4, 'I love tea', ts=clock.now() - 16 * 86400),
                   line(5, 'I hate tea', ts=clock.now() + 10)])
    assert invoke(data, '--since', '1', '--json', 'one-day.json') == 0
    report = json.loads((data / 'scratch/one-day.json').read_text(encoding='utf-8'))
    assert report['overall']['lines'] == 3
    assert report['since_days'] == 1


def test_parent_data_argument_and_empty_history(data, store):
    with store.transaction():
        store.conn.execute('DELETE FROM chat_line')
    assert main(['eval', '--data-dir', str(data), 'talk', '--fake', '--json', 'empty.json']) == 0
    report = json.loads((data / 'scratch/empty.json').read_text(encoding='utf-8'))
    assert report['overall'] == dict(lines=0, flagged_lines=0, per_100_lines=0)
    assert report['top_offenders'] == []


def test_scratch_link_cannot_escape_the_data_directory(data, tmp_path):
    target = data.parent / (data.name + '-foreign')
    target.mkdir()
    try:
        (data / 'scratch').symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip('symbolic-link creation is unavailable for this account')
    assert invoke(data, '--json', 'escape.json') == 1
    assert list(target.iterdir()) == []
