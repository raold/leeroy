"""`db prune`: a dry run that counts (the default), or `--go` with the mind stopped and a
snapshot first. See storage/prune.py for the rules."""
import sqlite3
import time
from contextlib import nullcontext
from datetime import datetime
from pathlib import Path

from leeroy_mind.storage import prune


def _show(findings, word) -> None:
    for f in findings:
        note = f"  {f.note}" if f.note else ""
        print(f"{f.rule:24} {f.rows:9} {word:5} {f.bytes / 1048576:9.1f} MB{note}")


def run(root, *, go=False, vacuum=False, as_of=None) -> int:
    from leeroy_mind.app import AlreadyRunning, acquire_instance_lock
    from leeroy_mind.storage.db import snapshot

    root = Path(root)
    path = (root / "mind.db").resolve()
    if as_of is not None and go:
        print("refused: --as-of is for a dry run")
        return 1
    try:
        now = time.time() if as_of is None else datetime.fromisoformat(as_of).timestamp()
    except ValueError:
        print("refused: --as-of takes a date, YYYY-MM-DD")
        return 1
    try:
        lock = acquire_instance_lock(root) if go else nullcontext()
    except AlreadyRunning:
        print("refused: mind service is running")
        return 1
    with lock:
        conn = None
        try:
            conn = sqlite3.connect(f"{path.as_uri()}?mode={'rw' if go else 'ro'}", uri=True)
            conn.row_factory = sqlite3.Row
            if not go:
                _show(prune.plan(conn, now), "rows")
                _show(prune.plan_files(root, now), "files")
                print("dry run, nothing written (the bytes are the rows' own; the file shrinks "
                      "only with --go --vacuum)")
                return 0
            if not prune.indexed(conn):
                print("refused: run db migrate first (the deletes need its indexes); nothing written")
                return 1
            taken = snapshot(conn, root / "snapshots", "pre-prune")
            print(f"snapshot {taken.name}")
            conn.execute("PRAGMA foreign_keys=ON")
            _show(prune.apply(conn, now), "rows")
            _show(prune.prune_files(root, now), "files")
            if vacuum:
                before = path.stat().st_size
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
                conn.execute("VACUUM")
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
                print(f"vacuum: {before / 1048576:.0f} MB -> {path.stat().st_size / 1048576:.0f} MB")
            print("written; snapshot taken")
            return 0
        except (sqlite3.Error, OSError) as err:
            print(f"refused: {type(err).__name__}; a batch already committed stays removed")
            return 1
        finally:
            if conn is not None:
                conn.close()
