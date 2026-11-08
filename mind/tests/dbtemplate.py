"""A pristine schema built once per pytest worker, copied with SQLite's backup API.

The session fixture builds it before any test can patch migration paths. Migration tests
still call the real open_db; ordinary fixtures get independent, writable database files.
"""
import sqlite3
from pathlib import Path

from leeroy_mind.storage.db import open_db

_template: sqlite3.Connection | None = None


def build_template(directory: Path) -> None:
    global _template
    # open_db requires WAL: migrate a file under pytest's worker temp root, then retain
    # its committed pages in memory. No cross-session cache or shared file.
    conn = open_db(directory / "template.db")
    try:
        conn.commit()
        template = sqlite3.connect(":memory:", autocommit=True)
        try:
            conn.backup(template)
        except BaseException:
            template.close()
            raise
        _template = template
    finally:
        conn.close()


def close_template() -> None:
    global _template
    if _template is not None:
        _template.close()
        _template = None


def open_migrated(path) -> sqlite3.Connection:
    """Copy the session schema only for a new path; open existing databases unchanged.

    Use open_db for real connection settings and migration validation, including when
    tests change the migration directory. Only the disposable copy's initial write skips fsync.
    """
    path = Path(path)
    if not path.exists():
        if _template is None:
            raise RuntimeError("the migrated_db_template session fixture has not run")
        copy = sqlite3.connect(path, autocommit=True)
        try:
            copy.execute("PRAGMA synchronous=OFF")
            _template.backup(copy)
        finally:
            copy.close()
    conn = open_db(path)
    # migrate() on an up-to-date file ends on a read (PRAGMA user_version), which leaves a read
    # snapshot open under autocommit=False; a fresh build ends on a commit. Commit so a second
    # connection's later write cannot make this one's next write fail with "database is locked".
    conn.commit()
    return conn
