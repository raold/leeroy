import sqlite3

from leeroy_mind.__main__ import main
from leeroy_mind.awareness.cli import awareness_main


def test_cli_is_read_only_on_scratch_and_an_older_schema(tmp_path,capsys,caplog):
    path=tmp_path/"mind.db"
    conn=sqlite3.connect(path)
    conn.execute("CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT)")
    conn.commit(); conn.close()
    before=path.read_bytes()
    assert main(["awareness","card","101","--data-dir",str(tmp_path)])==0
    assert "Member #101: unknown" in capsys.readouterr().out
    assert path.read_bytes()==before and caplog.text==""
    assert awareness_main(["--data-dir",str(tmp_path),"card","101","--budget-tokens","0"])==0
    assert capsys.readouterr().out=="\n"


def test_missing_database_is_not_created_or_named_in_errors(tmp_path,capsys,caplog):
    assert awareness_main(["card","101","--data-dir",str(tmp_path)])==1
    got=capsys.readouterr()
    assert got.out=="" and got.err=="awareness failed: OperationalError\n"
    assert list(tmp_path.iterdir())==[] and caplog.text==""


def test_cli_invalid_guid_and_budget_are_sanitized(tmp_path,capsys):
    sqlite3.connect(tmp_path/"mind.db").close()
    for args in (["card","0"],["card","101","--budget-tokens","-1"]):
        assert awareness_main([*args,"--data-dir",str(tmp_path)])==1
        assert capsys.readouterr().err=="awareness failed: ValueError\n"
