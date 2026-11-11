"""`python -m leeroy_mind.arc --data-dir D end <guid>`: end a member's romance with him by hand,
as history (stage ex, ended by the owner; state.end_romance), for when the mind is stopped.
It holds the instance lock throughout, so it refuses while the mind runs; it refuses a database
older than this code (run `db migrate` first), a member with no persona or no romance, and one
already ex, all before touching mind.db; then it takes a snapshot labelled "arc" and prints her
stage before and after, by guid only (no persona text). The running mind reads it at its next
start."""
import argparse
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

from leeroy_mind.config import default_data_dir


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m leeroy_mind.arc",
                                description=__doc__.split("\n")[0])
    p.add_argument("--data-dir", help="the mind's data dir (mind-data)")
    sub = p.add_subparsers(dest="action", required=True)
    e = sub.add_parser("end", help="end her romance with him (the mind must be stopped)")
    e.add_argument("guid", type=int)
    return p


def _refusal(raw: sqlite3.Connection, guid: int) -> str | None:
    """Why `guid`'s romance cannot be ended, read before anything is written; None when it can."""
    from leeroy_mind.storage.db import latest_version
    have = raw.execute("PRAGMA user_version").fetchone()[0]
    if have < latest_version():
        return (f"mind.db is schema v{have}, this code is v{latest_version()}: run "
                "`db migrate` first")
    human = raw.execute("SELECT value FROM meta WHERE key = 'human_guid'").fetchone()
    if human is None or not str(human[0]).isdigit() or int(human[0]) == 0:
        return "mind.db does not know his guid yet (meta human_guid)"
    from leeroy_mind.personas.tiers import is_cast
    if not is_cast(raw,guid) or raw.execute("SELECT 1 FROM persona_core WHERE guid = ?", (guid,)).fetchone() is None:
        return f"{guid}: no persona"
    stage = raw.execute("SELECT romance_stage FROM relationship WHERE a = ? AND b = ?",
                        (guid, int(human[0]))).fetchone()
    if stage is None or stage[0] == "none":
        return f"{guid}: no romance to end"
    if stage[0] == "ex":
        return f"{guid}: already ex, nothing to end"
    return None


def _end(root: Path, guid: int) -> int:
    from leeroy_mind.app import AlreadyRunning, acquire_instance_lock, instance_lock_held
    from leeroy_mind.arc.state import end_romance
    from leeroy_mind.clock import SystemClock
    from leeroy_mind.storage.db import open_db, snapshot
    from leeroy_mind.storage.store import Store
    db = root / "mind.db"
    if instance_lock_held(root):
        print("the mind is running: stop it first", file=sys.stderr)
        return 1
    try:
        lock = acquire_instance_lock(root)
    except AlreadyRunning:
        print("the mind is running: stop it first", file=sys.stderr)
        return 1
    with lock:
        with closing(sqlite3.connect(db)) as raw:
            raw.row_factory = sqlite3.Row
            why = _refusal(raw, guid)
            if why is not None:
                print(why, file=sys.stderr)
                return 1
            print(f"snapshot {snapshot(raw, root / 'snapshots', 'arc')}")
        store = Store(open_db(db), clock=SystemClock())
        try:
            human = int(store.get_meta("human_guid"))
            before = store.get_relationship(guid, human).romance_stage
            if not end_romance(store, guid, human, by="owner", now=store.clock.now()):
                print(f"{guid}: already ex, nothing to end", file=sys.stderr)
                return 1
            print(f"{guid}: {before} -> {store.get_relationship(guid, human).romance_stage}")
            return 0
        finally:
            store.close()


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    root = Path(args.data_dir).resolve() if args.data_dir else default_data_dir().resolve()
    if not (root / "mind.db").exists():
        print(f"no mind.db at {root / 'mind.db'}", file=sys.stderr)
        return 1
    return _end(root, args.guid)


if __name__ == "__main__":
    sys.exit(main())
