import sqlite3

from leeroy_mind.storage.db import TRANSIENT_CODES, error_code


def test_sqlite_errors_map_to_fixed_codes(tmp_path):
    conn = sqlite3.connect(":memory:")
    conn.set_progress_handler(lambda: 1, 10)
    try:
        conn.execute("WITH RECURSIVE n(x) AS (VALUES(0) UNION ALL SELECT x+1 FROM n WHERE x<100000) "
                     "SELECT sum(x) FROM n").fetchone()
    except sqlite3.OperationalError as error:
        assert error_code(error) == "interrupted"
    path = tmp_path / "a.db"
    a = sqlite3.connect(path, isolation_level=None)
    a.execute("PRAGMA journal_mode=WAL")
    a.execute("CREATE TABLE t(x)")
    b = sqlite3.connect(path, isolation_level=None, timeout=0.05)
    b.execute("BEGIN")
    b.execute("SELECT * FROM t").fetchall()
    a.execute("INSERT INTO t VALUES (1)")
    try:
        b.execute("INSERT INTO t VALUES (2)")  # its snapshot is behind another connection's commit
    except sqlite3.OperationalError as error:
        assert error_code(error) == "busy_snapshot"
    b.execute("ROLLBACK")
    a.execute("BEGIN IMMEDIATE")
    try:
        b.execute("INSERT INTO t VALUES (2)")
    except sqlite3.OperationalError as error:
        assert error_code(error) == "busy"
    a.execute("ROLLBACK")
    try:
        b.execute("SELECT * FROM secret_table")
    except sqlite3.OperationalError as error:
        assert error_code(error) == "other"
    assert {"interrupted", "busy", "busy_snapshot", "locked"} <= TRANSIENT_CODES
    assert error_code(ValueError("a value")) == "ValueError"
    for conn in (a, b):
        conn.close()
