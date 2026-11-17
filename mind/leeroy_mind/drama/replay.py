"""`drama threads replay`: the recorder (drama/pulls.py) run over nights the mind already
stored, on a scratch copy of mind.db, so the owner need not wait to see what the data would
have recorded. The live database is opened read-only for the copy and never written; the copy
lives in the data directory's scratch folder and is deleted afterwards unless kept.

Limits, stated in the output: every night is replayed against today's cast, personas
(households, stances) and relationship rows, since older ones are not stored per night."""
import sqlite3
from pathlib import Path

from leeroy_mind.clock import SystemClock
from leeroy_mind.drama.pulls import cast_from_store, record_night
from leeroy_mind.drama.report import threads_report
from leeroy_mind.storage.db import open_db, snapshot
from leeroy_mind.storage.store import Store

LABEL = "threads-replay"
LIMITS = "replayed against today's cast, households, stances and relationship rows"


def _human(conn) -> int:
    row = conn.execute("SELECT value FROM meta WHERE key = 'human_guid'").fetchone()
    if row is None or not str(row[0]).isdigit() or int(row[0]) <= 0:
        raise ValueError("no human_guid in mind.db")
    return int(row[0])


def replay(data_dir: Path, since: str, *, keep: bool = False, clock=None) -> dict:
    """Replay the stored nights from `since` (YYYY-MM-DD) on a fresh copy. Returns the report
    with `replayed` (the nights), `copy` (its path when kept) and `limits`."""
    clock = clock or SystemClock()
    live = Path(data_dir) / "mind.db"
    scratch = Path(data_dir) / "scratch"
    if scratch.is_symlink():
        raise ValueError("scratch must be a real folder of the data directory")
    source = sqlite3.connect(f"{live.resolve().as_uri()}?mode=ro", uri=True, timeout=5.0)
    source.row_factory = sqlite3.Row
    try:
        copy = snapshot(source, scratch, LABEL, keep=3, now=clock.now())
    finally:
        source.close()
    if copy.resolve() == live.resolve():
        raise ValueError("the replay copy must not be the live database")
    store = Store(open_db(copy), clock=clock)
    try:
        with store.transaction():
            for table in ("pull_event", "pull_shadow", "pull_night"):
                store.conn.execute(f"DELETE FROM {table}")
        cast = cast_from_store(store, _human(store.conn))
        nights = store.conn.execute(
            "SELECT night, window_start, window_end FROM night_run WHERE night >= ? "
            "AND status = 'done' ORDER BY night", (since,)).fetchall()
        for n in nights:
            with store.transaction():
                record_night(store, n["night"], (n["window_start"], n["window_end"]), cast,
                             now=n["window_end"])
        data = threads_report(store.conn, nights=max(1, len(nights)))
    finally:
        store.close()
    if not keep:
        for suffix in ("", "-wal", "-shm"):
            Path(str(copy) + suffix).unlink(missing_ok=True)
    data["replayed"] = [n["night"] for n in nights]
    data["copy"] = str(copy) if keep else None
    data["limits"] = LIMITS
    return data
