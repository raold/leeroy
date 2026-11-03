import json
import sqlite3
from contextlib import closing

import pytest

from conftest import make_persona
from leeroy_mind.personas.io import save_persona
from leeroy_mind.realm import cli
from leeroy_mind.realm.armory import RealmArmoryUnavailable
from leeroy_mind.realm.store import record_receipt


def test_status_is_read_only_and_reports_actual_realm_rows(tmp_path, store, monkeypatch, capsys):
    from leeroy_mind.storage import db
    from leeroy_mind import app
    with store.conn:
        store.conn.execute("INSERT INTO realm_step(name,sha256,approved) VALUES ('review','abc',1)")
        store.conn.execute("INSERT INTO realm_canon(achievement,guid,reason,ts,state) VALUES (1,101,'deferred',2,'blocked')")
    record_receipt(store.conn, command_id="moving", kind="realm_move", stage="job",
                   ok=False, reason="offline", ts=3)
    # SQLite may create WAL/shared-memory sidecars even for a closed WAL-header
    # snapshot opened mode=ro. The main database's bytes and mtime stay unchanged.
    copied = tmp_path / "closed-copy"
    copied.mkdir()
    path = copied / "mind.db"
    copy_conn = sqlite3.connect(path)
    try:
        store.conn.backup(copy_conn)
    finally:
        copy_conn.close()
    before = path.read_bytes(), path.stat().st_mtime_ns
    def forbidden(*args, **kwargs):
        raise AssertionError("read-only CLI tried application/database initialization")
    monkeypatch.setattr(db, "open_db", forbidden)
    monkeypatch.setattr(app, "MindApp", forbidden)
    assert cli.realm_main(["status", "--data-dir", str(copied)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["counts"]["realm_step"] == 1
    assert result["approvals"][0]["name"] == "review"
    assert result["failures"][0]["reason"] == "offline"
    assert result["grant_deferrals"][0]["state"] == "blocked"
    assert before == (path.read_bytes(), path.stat().st_mtime_ns)


def test_reader_uses_ro_uri_and_query_only(tmp_path, store):
    with closing(cli._open(tmp_path)) as conn:
        assert conn.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO realm_step(name,sha256,approved) VALUES ('x','y',1)")
        # Check the URI's mode=ro independently of the connection-level guard.
        # This changes only the disposable reader's pragma, never the DB mode.
        conn.execute("PRAGMA query_only=OFF")
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO realm_step(name,sha256,approved) VALUES ('x','y',1)")
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")


def test_status_missing_db_does_not_create_it(tmp_path, capsys):
    assert cli.realm_main(["--data-dir", str(tmp_path), "status"]) == 1
    assert not (tmp_path / "mind.db").exists()
    assert "command unavailable" in capsys.readouterr().out


def test_status_old_schema_is_not_migrated(tmp_path, capsys):
    path = tmp_path / "mind.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE old_data(id INTEGER)")
    before = path.read_bytes()
    assert cli.realm_main(["status", "--data-dir", str(tmp_path)]) == 1
    assert path.read_bytes() == before
    assert "not migrated" in capsys.readouterr().out


def test_titles_reads_cache_aliases_and_actual_guild_rank(tmp_path, store, monkeypatch, capsys):
    for guid, title in [(101, "Raid Lead / DKP"), (102, "Baker"), (103, "dkperson")]:
        save_persona(store, make_persona(guid, f"Member{guid}", officer_title=title),
                     source="seed", ts=1, race="Human", class_name="Warrior")
    called = []
    class Armory:
        def guild_ranks(self, guild):
            called.append(guild)
            return [0, 2, 4]
    monkeypatch.setattr(cli, "armory_from_config", lambda path: Armory())
    assert cli.realm_main(["titles", "--data-dir", str(tmp_path)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["titles"]["raid lead"] == [[101, "Member101"]]
    assert result["titles"]["dkp"] == [[101, "Member101"]]
    assert result["titles"]["chef"] == [[102, "Member102"]]
    assert len(result["titles"]) == 10
    assert result["trial_rank_exists"] is True and called == [21]


@pytest.mark.parametrize("ranks,exists", [([], False), ([0, 1, 2], False), ([4], True)])
def test_proven_rank_absence_differs_from_unavailable(tmp_path, store, monkeypatch, capsys, ranks, exists):
    class Armory:
        def guild_ranks(self, guild):
            return ranks
    monkeypatch.setattr(cli, "armory_from_config", lambda path: Armory())
    assert cli.realm_main(["titles", "--data-dir", str(tmp_path)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["armory"] == "available" and result["trial_rank_exists"] is exists


@pytest.mark.parametrize("absent", [False, True])
def test_titles_unavailable_armory_is_partial_and_redacted(tmp_path, store, monkeypatch, capsys, absent):
    class Armory:
        def guild_ranks(self, guild):
            raise RealmArmoryUnavailable("password=private secret path")
    monkeypatch.setattr(cli, "armory_from_config", lambda path: None if absent else Armory())
    assert cli.realm_main(["titles", "--data-dir", str(tmp_path)]) == 1
    output = capsys.readouterr().out
    result = json.loads(output)
    assert result["armory"] == "unavailable" and result["trial_rank_exists"] is None
    assert "titles" in result and "private" not in output and "password" not in output


def test_cli_errors_do_not_echo_local_values(tmp_path, store, capsys):
    (tmp_path / "mind.toml").write_text('[realm]\ntrial_rank = "private-name"\n')
    assert cli.realm_main(["titles", "--data-dir", str(tmp_path)]) == 1
    output = capsys.readouterr().out
    assert "private-name" not in output and str(tmp_path) not in output
