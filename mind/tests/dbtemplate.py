"""A migrated mind.db built once per test session and copied for each test that needs a fresh
one. The template is built from the checked-out code's own migrations (open_db), never cached
across sessions, and removed at exit. Tests of migrations themselves keep calling open_db."""
import atexit
import shutil
import sqlite3
import tempfile
from pathlib import Path

from leeroy_mind.storage.db import open_db

_dir: Path | None = None
_template: Path | None = None


def _build() -> Path:
    global _dir, _template
    if _template is None:
        _dir = Path(tempfile.mkdtemp(prefix="mind-template-"))
        atexit.register(shutil.rmtree, _dir, ignore_errors=True)
        path = _dir / "template.db"
        conn = open_db(path)
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
        conn.close()
        for suffix in ("-wal", "-shm"):
            Path(str(path) + suffix).unlink(missing_ok=True)
        _template = path
    return _template


def open_migrated(path) -> sqlite3.Connection:
    """open_db(path) on a database copied from the session template when path does not exist yet
    (the copy is of a closed file, so there is no -wal or -shm); an existing path opens as it
    is. open_db still runs, so it sees a database already at the latest version."""
    path = Path(path)
    if not path.exists():
        shutil.copyfile(_build(), path)
    conn = open_db(path)
    # migrate() on an up-to-date file ends on a read (PRAGMA user_version), which leaves a read
    # snapshot open under autocommit=False; a fresh build ends on a commit. Commit so a second
    # connection's later write cannot make this one's next write fail with "database is locked".
    conn.commit()
    return conn
