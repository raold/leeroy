import os
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path

import sqlite_vec

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_MIGRATION_RE = re.compile(r"^(\d{4})_.*\.sql$")
VEC_DIM = 512
_LABEL_BAD = re.compile(r"[^A-Za-z0-9_-]+")


class EmbeddingMismatch(RuntimeError):
    pass


def open_db(path: Path) -> sqlite3.Connection:
    """Open mind.db: WAL, FK on, sqlite-vec loaded, migrated. The connection has
    autocommit=False, so a transaction is always open; commit through Store.transaction()."""
    conn = sqlite3.connect(path, autocommit=True, timeout=5.0)
    try:
        conn.row_factory = sqlite3.Row
        # Pragmas such as journal_mode and foreign_keys are no-ops inside a transaction, so set
        # them while autocommit is still on.
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
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


def _migration_files() -> list[tuple[int, Path]]:
    found = []
    for p in MIGRATIONS_DIR.iterdir():
        m = _MIGRATION_RE.match(p.name)
        if m:
            found.append((int(m.group(1)), p))
    return sorted(found)


def latest_version() -> int:
    return max((v for v, _ in _migration_files()), default=0)


def migrate(conn: sqlite3.Connection) -> int:
    """Apply every migrations/NNNN_*.sql above PRAGMA user_version, each in its own transaction
    (DDL is transactional in SQLite, so a failing file leaves no partial schema). Idempotent."""
    conn.commit()
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    files = _migration_files()
    numbers = [v for v, _ in files]
    if len(numbers) != len(set(numbers)):
        raise RuntimeError(f"duplicate migration numbers in {MIGRATIONS_DIR}")
    if current > latest_version():
        raise RuntimeError(
            f"mind.db is schema v{current}, newer than this code (v{latest_version()}); "
            "refusing to open it")
    for version, path in files:
        if version <= current:
            continue
        try:
            # executescript does not commit when autocommit=False, so the script and the
            # user_version bump commit together below.
            conn.executescript(path.read_text(encoding="utf-8"))
            conn.execute(f"PRAGMA user_version = {int(version)}")
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        current = version
    return current


def checkpoint(conn: sqlite3.Connection) -> None:
    """Fold the WAL into the main file and truncate it. Commits first: call it between units."""
    conn.commit()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
    conn.commit()


def snapshot(conn: sqlite3.Connection, dir: Path, label: str, keep: int = 10,
             now: float | None = None) -> Path:
    """Copy the committed database to dir/mind-<label>-<YYYYmmdd-HHMMSS>.db (local time of
    `now`, unix seconds, default the wall clock), then prune that label's snapshots to the
    newest `keep`: each label keeps its own, so a night of `start` snapshots never rotates out
    a `pre-golive` one. Other files in dir are never touched. Only committed data is copied,
    so it is safe while a unit of work is open on `conn` or another writer is busy."""
    dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.fromtimestamp(time.time() if now is None else now).strftime("%Y%m%d-%H%M%S")
    label = _LABEL_BAD.sub("_", label) or "snap"
    dest = dir / f"mind-{label}-{stamp}.db"
    n = 1
    while dest.exists():  # same label in the same second: never overwrite the earlier snapshot
        n += 1
        dest = dir / f"mind-{label}-{n}-{stamp}.db"
    tmp = dir / f"{dest.name}.partial"
    # Back up through a separate read-only connection. Backing up `conn` itself deadlocks (the
    # backup retries on SQLITE_BUSY forever) when a unit of work has uncommitted writes on it.
    main = conn.execute("PRAGMA database_list").fetchone()["file"]
    src = sqlite3.connect(f"{Path(main).as_uri()}?mode=ro", uri=True, timeout=5.0)
    out = sqlite3.connect(tmp)
    try:
        src.backup(out)
    except BaseException:
        out.close()
        src.close()
        tmp.unlink(missing_ok=True)
        raise
    out.close()
    src.close()
    os.replace(tmp, dest)
    _prune(dir, keep, label)
    return dest


def _prune(dir: Path, keep: int, label: str) -> None:
    """The label's own snapshots (a same-second twin is mind-<label>-<n>-<stamp>.db) beyond the
    newest `keep`."""
    own = re.compile(rf"^mind-{re.escape(label)}(?:-\d+)?-(\d{{8}}-\d{{6}})\.db$")
    snaps = []
    for p in dir.glob(f"mind-{label}-*.db"):
        m = own.match(p.name)
        if m:
            snaps.append((m.group(1), p.name, p))
    snaps.sort()
    for _, _, p in snaps[:max(0, len(snaps) - keep)]:
        p.unlink(missing_ok=True)


def check_embedding_meta(store, model: str, dim: int) -> None:
    """Record the embedding model and dimension on first use; raise EmbeddingMismatch if a
    later call differs (the stored vectors would be meaningless) or dim is not the 512 the
    memory_vec table is built for."""
    if dim != VEC_DIM:
        raise EmbeddingMismatch(f"memory_vec is {VEC_DIM}-dimensional, configured {model} is {dim}d")
    with store.transaction():
        have_model = store.get_meta("embed_model")
        have_dim = store.get_meta("embed_dim")
        if have_model is None and have_dim is None:
            store.set_meta("embed_model", model)
            store.set_meta("embed_dim", str(dim))
            return
        if have_model != model or have_dim != str(dim):
            raise EmbeddingMismatch(
                f"mind.db was built with {have_model} ({have_dim}d), configured {model} ({dim}d)")
