"""T1: the tuning tables. The log holds every owner action, the values table the current state,
and the database refuses rows the log could never mean."""
import sqlite3

import pytest

from leeroy_mind.storage.db import latest_version, open_db

TABLES = {"tuning_group", "tuning_change", "tuning_row", "tuning_value"}


def _tables(conn):
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_a_new_database_has_the_tuning_tables_and_no_rows(tmp_path):
    conn = open_db(tmp_path / "mind.db")
    try:
        assert TABLES <= _tables(conn)
        for t in TABLES:
            assert conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] == 0
        assert conn.execute("SELECT value FROM meta WHERE key='tuning_version'").fetchone() is None
    finally:
        conn.close()


def test_the_tuning_migration_is_the_latest_and_needs_the_realms(tmp_path):
    names = sorted(p.name for p in (open_db.__globals__["MIGRATIONS_DIR"]).iterdir()
                   if p.suffix == ".sql")
    tuning = [n for n in names if n.endswith("_tuning.sql")]
    assert len(tuning) == 1 and int(tuning[0][:4]) == latest_version()
    assert any(n.startswith("0016_") for n in names)


@pytest.fixture
def conn(tmp_path):
    c = open_db(tmp_path / "mind.db")
    c.execute("INSERT INTO tuning_change(id,ts,source,label) VALUES (1,1000,'console','')")
    c.commit()
    yield c
    c.close()


@pytest.mark.parametrize("sql", [
    "INSERT INTO tuning_change(id,ts,source,label) VALUES (2,1000,'web','')",
    "INSERT INTO tuning_change(id,ts,source,label,previewed) VALUES (2,1000,'cli','',2)",
    "INSERT INTO tuning_change(id,ts,source,label,undoes) VALUES (2,1000,'cli','',99)",
    "INSERT INTO tuning_row(change,scope,target,dial,before,after) VALUES (1,'guild','','talkative',NULL,6)",
    "INSERT INTO tuning_row(change,scope,target,dial,before,after) VALUES (1,'member','101','Talk',NULL,6)",
    "INSERT INTO tuning_row(change,scope,target,dial,before,after) VALUES (1,'member','x1','talkative',NULL,6)",
    "INSERT INTO tuning_row(change,scope,target,dial,before,after) VALUES (1,'world','x','chattiness',NULL,6)",
    "INSERT INTO tuning_row(change,scope,target,dial,before,after) VALUES (1,'member','101','talkative',NULL,6.5)",
    "INSERT INTO tuning_row(change,scope,target,dial,before,after) VALUES (9,'member','101','talkative',NULL,6)",
    "INSERT INTO tuning_value(scope,target,dial,value,change) VALUES ('member','101','talkative',NULL,1)",
    "INSERT INTO tuning_value(scope,target,dial,value,change) VALUES ('member','101','talkative',11,1)",
    "INSERT INTO tuning_group(id,name,predicate,created) VALUES (1,'','{}',1000)",
    "INSERT INTO tuning_group(id,name,predicate,created) VALUES (1,'women','[1]',1000)",
    "INSERT INTO tuning_group(id,name,predicate,created) VALUES (1,'women','not json',1000)",
])
def test_rows_the_log_could_never_mean_are_refused(conn, sql):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(sql)


def test_a_live_group_name_is_unique_but_a_retired_one_may_come_back(conn):
    conn.execute("INSERT INTO tuning_group(id,name,predicate,created) VALUES (1,'women','{}',1)")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO tuning_group(id,name,predicate,created) VALUES (2,'women','{}',1)")
    conn.execute("UPDATE tuning_group SET retired=2 WHERE id=1")
    conn.execute("INSERT INTO tuning_group(id,name,predicate,created) VALUES (2,'women','{}',3)")


def test_a_value_row_is_one_per_scope_target_and_dial(conn):
    conn.execute("INSERT INTO tuning_value(scope,target,dial,value,change) "
                 "VALUES ('member','101','talkative',6,1)")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO tuning_value(scope,target,dial,value,change) "
                     "VALUES ('member','101','talkative',7,1)")
