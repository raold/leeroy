#!/usr/bin/env python3
"""Small, standard-library-only data operations for ac-release.sh.

No application imports: checking a database must never migrate it. All command
arguments are paths/raid IDs, never database credentials or persona contents.
"""
from __future__ import annotations

from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys


def read_db(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)


def voice_stopped(data: Path) -> int:
    """Probe the voice OS lock without importing models, creating files or changing bytes.

    Voice files are accepted only while that separately managed service is stopped.
    This release tool does not enable a stopped voice service.
    """
    path = data / "voice.lock"
    try:
        path.stat()
    except FileNotFoundError:
        return 0
    with path.open("r+b") as fh:
        if os.name == "nt":
            import msvcrt
            try:
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                return 1
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return 1
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    return 0


def raids(path: Path, allowed: set[int]) -> int:
    with closing(read_db(path)) as db:
        open_raids = [("guild", int(row[0])) for row in db.execute(
            "SELECT raid_id FROM guild_raid WHERE ended IS NULL ORDER BY raid_id"
        )]
        row = db.execute("SELECT value FROM meta WHERE key='guild:offline:owned'").fetchone()
        if row:
            owned = json.loads(row[0])
            if not isinstance(owned, dict):
                raise ValueError("invalid owned raid")
            if owned.get("phase") not in ("complete", "rejected"):
                raid_id = owned.get("raid_id")
                if type(raid_id) is not int or raid_id < 1:
                    raise ValueError("invalid owned raid identity")
                open_raids.append(("offline", raid_id))
    blocked = False
    for kind, raid_id in open_raids:
        if raid_id in allowed:
            print(f"note: allowing {kind} raid {raid_id}")
        else:
            print(f"FAIL: open {kind} raid {raid_id}")
            blocked = True
    return int(blocked)


def backup(source: Path, target: Path, *, restore: bool = False) -> None:
    if source.resolve() == target.resolve():
        raise ValueError("snapshot and database must differ")
    if not restore:
        target.parent.mkdir(parents=True, exist_ok=True)
        # Never overwrite an existing recovery point, including on a same-second retry.
        with target.open("xb"):
            pass
    with closing(read_db(source)) as src, closing(sqlite3.connect(target, timeout=5)) as dst, dst:
        src.backup(dst)
        if dst.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise ValueError("database verification failed")


def binaries(source: Path, target: Path, *, restore: bool = False) -> None:
    # Only runtime binaries, not configuration, logs, maps or the build tree.
    source, target = source.resolve(strict=True), target.resolve()
    if source == target or source in target.parents or target in source.parents:
        raise ValueError("binary backup must be outside the binary directory")
    if restore:
        names = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
    else:
        names = sorted(p.name for p in source.iterdir() if p.suffix.lower() in (".exe", ".dll"))
        if not {"authserver.exe", "worldserver.exe"}.issubset({n.lower() for n in names}):
            raise ValueError("server binaries unavailable")
        target.mkdir(parents=True, exist_ok=False)
    for name in names:
        if Path(name).name != name or Path(name).suffix.lower() not in (".exe", ".dll"):
            raise ValueError("invalid binary name")
        src, dst = source / name, target / name
        if src.is_symlink() or dst.is_symlink() or not src.is_file():
            raise ValueError("binary is not a regular file")
        shutil.copy2(src, dst)
    if restore:
        # Remove only newly introduced runtime binaries in this exact directory.
        for path in target.iterdir():
            if path.suffix.lower() in (".exe", ".dll") and path.name not in names:
                path.unlink()
    else:
        (target / "manifest.json").write_text(json.dumps(names), encoding="utf-8")


def main() -> int:
    action, source, *args = sys.argv[1:]
    if action == "raids":
        return raids(Path(source), {int(arg) for arg in args})
    if action == "voice-stopped":
        return voice_stopped(Path(source))
    if action in ("snapshot", "restore"):
        backup(Path(source), Path(args[0]), restore=action == "restore")
    elif action in ("save-binaries", "restore-binaries"):
        binaries(Path(source), Path(args[0]), restore=action == "restore-binaries")
    else:
        raise ValueError("unknown action")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, TypeError, sqlite3.Error):
        # Malformed stored JSON/SQLite errors can contain private values.
        print("FAIL: release data operation failed; inspect the database or paths locally", file=sys.stderr)
        sys.exit(1)
