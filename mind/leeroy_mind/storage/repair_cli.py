"""The offline, snapshot-first db repair-provenance command."""
import sqlite3
from contextlib import nullcontext

import sqlite_vec

from leeroy_mind.storage import repair_provenance
from leeroy_mind.storage.provenance import ProvenanceError, WalkLimit


def run(root, *, go=False):
    from leeroy_mind.app import AlreadyRunning, acquire_instance_lock
    from leeroy_mind.storage.db import snapshot

    path = (root / "mind.db").resolve()
    try:
        lock = acquire_instance_lock(root) if go else nullcontext()
    except AlreadyRunning:
        print("refused: mind service is running")
        return 1
    with lock:
        conn = None
        try:
            mode = "rw" if go else "ro"
            conn = sqlite3.connect(f"{path.as_uri()}?mode={mode}", uri=True)
            conn.row_factory = sqlite3.Row
            if go:
                # Never migrate, even for a repair. The snapshot preserves the exact
                # input schema; the service lock covers snapshot, writes and verification.
                snapshot(conn, root / "snapshots", "pre-repair-provenance")
                conn.enable_load_extension(True)
                sqlite_vec.load(conn)
                conn.enable_load_extension(False)
            conn.execute("BEGIN IMMEDIATE" if go else "BEGIN")
            result = (repair_provenance.apply if go else repair_provenance.plan)(conn)
            if go:
                conn.commit()
            print(f"before: self refs {result.before.self_refs}, "
                  f"other cycle edges {result.before.cycle_edges}")
            print(f"rows to change {len(result.changed)}, self refs dropped "
                  f"{result.self_dropped}, cycle refs dropped {result.cycle_dropped}")
            print(f"after: self refs {result.after.self_refs}, "
                  f"other cycle edges {result.after.cycle_edges}")
            print("written; snapshot taken" if go else "dry run, nothing written")
            return 0
        except (ProvenanceError, sqlite3.Error, ValueError, TypeError, OSError) as err:
            code = "bound" if isinstance(err, WalkLimit) else "invalid_or_unavailable"
            print(f"refused: {code}; nothing written")
            return 1
        finally:
            if conn is not None:
                conn.close()  # rolls back an incomplete/failed repair
