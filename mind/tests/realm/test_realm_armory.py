from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from leeroy_mind.library import mysql
from leeroy_mind.personas.roster import DbInfo
from leeroy_mind.realm.armory import RealmArmory, RealmArmoryUnavailable
from leeroy_mind.realm.ports import LivePorts, armory_from_config


def database(name="characters"):
    return DbInfo("localhost", 3306, "reader", "synthetic-secret", name)


def test_armory_is_inert_until_bounded_constant_rank_query(monkeypatch):
    calls = []
    def query(db, sql, exe, **kwargs):
        calls.append((db, sql, exe, kwargs))
        return [["4"], ["0"], ["1"]]
    monkeypatch.setattr(mysql, "query", query)
    armory = RealmArmory(database(), database("world"), database("auth"), Path("synthetic-mysql"))
    assert calls == []
    assert armory.guild_ranks(21) == [0, 1, 4]
    assert calls == [(armory.chars, "SELECT rid FROM guild_rank WHERE guildid=21 ORDER BY rid",
                      Path("synthetic-mysql"), dict(timeout=10, max_ms=5000))]


@pytest.mark.parametrize("guild_id", [True, 0, -1, 2**32, "21", 21.0])
def test_invalid_guild_id_never_queries(monkeypatch, guild_id):
    monkeypatch.setattr(mysql, "query", lambda *a, **k: pytest.fail("query must not start"))
    armory = RealmArmory(database(), database("world"), database("auth"), Path("synthetic-mysql"))
    with pytest.raises(ValueError):
        armory.guild_ranks(guild_id)


@pytest.mark.parametrize("rows", [[["4"], ["4"]], [["10"]], [["４"]], [[None]], [["1", "2"]], [["-1"]]])
def test_malformed_rank_evidence_is_unavailable(monkeypatch, rows):
    monkeypatch.setattr(mysql, "query", lambda *a, **k: rows)
    armory = RealmArmory(database(), database("world"), database("auth"), Path("synthetic-mysql"))
    with pytest.raises(RealmArmoryUnavailable, match="evidence unavailable"):
        armory.guild_ranks(21)


def test_query_error_suppresses_escaped_secrets_and_sql_details(monkeypatch):
    def fail(*args, **kwargs):
        raise mysql.QueryError("server stderr synthetic\\-secret SELECT configured-path")
    monkeypatch.setattr(mysql, "query", fail)
    armory = RealmArmory(database(), database("world"), database("auth"), Path("synthetic-mysql"))
    with pytest.raises(RealmArmoryUnavailable) as caught:
        armory.guild_ranks(21)
    assert str(caught.value) == "armory query unavailable"
    assert caught.value.__suppress_context__ is True


def test_invalid_database_identifier_is_rejected_before_io():
    with pytest.raises(RealmArmoryUnavailable, match="configuration unavailable"):
        RealmArmory(database("characters;DROP"), database("world"), database("auth"), Path("synthetic-mysql"))


def test_live_armory_reads_only_configured_credentials_lazily_and_caches(monkeypatch):
    from leeroy_mind.personas import roster
    calls = []
    def read(path, key="CharacterDatabaseInfo"):
        calls.append((path, key))
        return database(key)
    monkeypatch.setattr(roster, "read_db_info", read)
    cfg = NS(worldserver_conf=Path("synthetic-worldserver.conf"), mysql_exe=Path("synthetic-mysql"))
    app = NS(clock=object(), store=NS(conn=object()), human_guid=1, guild=NS(cfg=cfg))
    ports = LivePorts(app)
    assert calls == []
    backend = ports.armory()
    assert backend is not None and ports.armory() is backend
    assert calls == [(cfg.worldserver_conf, key) for key in
                     ("CharacterDatabaseInfo", "WorldDatabaseInfo", "LoginDatabaseInfo")]


def test_cli_config_error_is_unavailable_with_safe_diagnostic(tmp_path, caplog):
    path = tmp_path / "mind.toml"
    path.write_text('[guild]\nworldserver_conf = 3\n', encoding="utf-8")
    assert armory_from_config(path) is None
    assert "realm_armory_configuration_unavailable" in caplog.text
    assert str(path) not in caplog.text
