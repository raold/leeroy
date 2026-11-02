import pytest

from leeroy_mind.library import __main__ as cli
from leeroy_mind.library.core import Library
from leeroy_mind.library.serverref import ServerRefSource
from library_snapshot_helpers import command, server_snapshot, teleport


@pytest.fixture
def reference(tmp_path, clock):
    source = tmp_path / 'snapshot'
    server_snapshot(source, commands=[command(), command(name='go', handler='', syntax=''),
        command(name='go xyz', syntax='.go xyz <x> <y> <z> [map]')],
        teleports=[teleport(), teleport(id=8, name='Test Harbor Keep'), teleport(id=9, name='Dalaran')])
    lib = Library(tmp_path / 'library.db', clock=clock)
    lib.build([ServerRefSource(source)])
    yield lib
    lib.close()


def test_exact_paths_precede_fts_and_do_not_invent_abbreviations(reference):
    hits = reference.reference_matches('What does .go xyz do?', ['command'])
    assert [c.title for c in hits] == ['.go xyz']
    assert reference.search_text('What does .go xyz do?', kinds=['command'])[0].title == '.go xyz'
    assert reference.reference_matches('.tel', ['command']) == []
    assert reference.reference_matches('Where is Test Harbor?', ['zone']) == []
    assert reference.reference_matches('Test Harbor') == []


def test_destination_names_and_explicit_ids_are_exact(reference):
    hits = reference.reference_matches('Teleport coordinates for Test Harbor Keep?', ['teleport'])
    assert hits[0].title == 'Test Harbor Keep'
    assert hits[0].metadata['position_x'] == '-1234.5000'
    assert [c.title for c in reference.reference_matches('teleport id 7', ['teleport'])] == ['Test Harbor']
    assert reference.reference_matches('teleport id 999', ['teleport']) == []
    assert reference.reference_matches('Harbor', ['teleport']) == []
    assert reference.reference_matches('Test Harbor', ['teleport'], k=0) == []


def test_teleport_question_prioritizes_coordinates_over_longer_command(reference):
    hits = reference.reference_matches('What are coordinates for the named teleport Dalaran?',
                                       ['teleport', 'command', 'documentation'])
    assert [c.kind for c in hits] == ['teleport', 'command']
    assert hits[0].title == 'Dalaran'


def test_snapshot_cli_requires_path_and_reports_invalid_snapshot(tmp_path, capsys):
    args = ['--data-dir', str(tmp_path / 'data'), 'build', '--sources', 'acwiki', '--no-embed']
    with pytest.raises(SystemExit, match='requires --acwiki-snapshot'):
        cli.main(args)
    assert cli.main([*args, '--acwiki-snapshot', str(tmp_path / 'missing')]) == 1
    assert 'build failed' in capsys.readouterr().err


def test_reference_cli_keeps_placeholders_coordinates_and_provenance(tmp_path, reference, capsys):
    data = ['--data-dir', str(tmp_path)]
    assert cli.main([*data, 'search', '.go xyz', '--kind', 'command', '--text-only', '-k', '1']) == 0
    out = capsys.readouterr().out
    assert '.go xyz <x> <y> <z> [map]' in out
    assert 'source-observed' in out and 'revision' in out
    assert cli.main([*data, 'search', 'Test Harbor', '--kind', 'teleport', '--text-only', '-k', '1']) == 0
    out = capsys.readouterr().out
    assert 'x=-1234.5000, y=+42.125000, z=0.00000001' in out
    assert 'Orientation: 6.283185' in out


def test_existing_build_defaults_do_not_require_new_snapshot_paths():
    assert cli.DEFAULT_SOURCES == ('world', 'atlasloot', 'wiki', 'curated')


def test_snapshot_worker_cli_parses_count_and_rejects_unbounded_values(tmp_path, monkeypatch):
    received = []
    monkeypatch.setattr(cli, 'cmd_build', lambda args: received.append(args.snapshot_workers) or 0)
    assert cli.main(['build', '--snapshot-workers', '12']) == 0
    assert received == [12]
    for count in ('0', '17', 'nan', '1.5'):
        with pytest.raises(SystemExit):
            cli.main(['build', '--snapshot-workers', count])


def test_snapshot_process_cli_is_optional_and_bounded(monkeypatch):
    received = []
    monkeypatch.setattr(cli, 'cmd_build', lambda args: received.append(args.snapshot_parse_processes) or 0)
    assert cli.main(['build']) == 0
    assert cli.main(['build', '--snapshot-parse-processes', '4']) == 0
    assert received == [0, 4]
    for count in ('-1', '17', 'nan', '1.5'):
        with pytest.raises(SystemExit):
            cli.main(['build', '--snapshot-parse-processes', count])
