"""library.db: opened and migrated the same way as mind.db (WAL, sqlite-vec, numbered SQL files
over PRAGMA user_version), with its own migrations so the two never share a schema."""
import re
import sqlite3
from pathlib import Path

import sqlite_vec

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_MIGRATION_RE = re.compile(r"^(\d{4})_.*\.sql$")
VEC_DIM = 512


def _migration_files() -> list[tuple[int, Path]]:
    found = [(int(m.group(1)), p) for p in MIGRATIONS_DIR.iterdir()
             if (m := _MIGRATION_RE.match(p.name))]
    numbers = [v for v, _ in found]
    if len(numbers) != len(set(numbers)):
        raise RuntimeError(f"duplicate migration numbers in {MIGRATIONS_DIR}")
    return sorted(found)


def latest_version() -> int:
    return max((v for v, _ in _migration_files()), default=0)


def open_library_db(path: Path, *, readonly: bool = False) -> sqlite3.Connection:
    """WAL, foreign keys on, sqlite-vec loaded, migrated. autocommit=False like mind.db: a
    transaction is always open, so callers commit. `readonly` is the mind's use at runtime: the
    file is opened mode=ro, never created, set up or migrated, and a schema other than this
    code's is refused (the library CLI migrates it)."""
    if readonly:
        conn = sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True,
                               autocommit=True, timeout=5.0)
    else:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, autocommit=True, timeout=5.0)
    try:
        conn.row_factory = sqlite3.Row
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        if readonly:
            have = conn.execute("PRAGMA user_version").fetchone()[0]
            if have != latest_version():
                raise RuntimeError(f"library.db is schema v{have}, this code reads "
                                   f"v{latest_version()}: migrate it with the library CLI")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.autocommit = False
            return conn
        if conn.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower() != "wal":
            raise RuntimeError(f"could not enable WAL on {path}")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.autocommit = False
        migrate(conn)
    except BaseException:
        conn.close()
        raise
    return conn


def migrate(conn: sqlite3.Connection) -> int:
    """Apply each migrations/NNNN_*.sql above user_version in its own transaction. Idempotent."""
    conn.commit()
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    if current > latest_version():
        raise RuntimeError(f"library.db is schema v{current}, newer than this code "
                           f"(v{latest_version()}); refusing to open it")
    for version, path in _migration_files():
        if version <= current:
            continue
        try:
            conn.executescript(path.read_text(encoding="utf-8"))
            conn.execute(f"PRAGMA user_version = {int(version)}")
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        current = version
    return current


def checkpoint(conn: sqlite3.Connection) -> None:
    conn.commit()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
    conn.commit()
