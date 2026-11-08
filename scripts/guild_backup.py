"""Verified, content-addressed guild backups (Python 3.11+, standard library only).

No service imports, credentials, network clients, or live restore operations. The
destination is an ordinary mounted directory; its access belongs to the user.
"""

from __future__ import annotations

import argparse
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import stat
import sys
import tempfile
import time
import tomllib
import uuid

OWNER = "leeroy-guild-backup"
STORE = "leeroy-guild-backups-v1"
STORE_MARKER = ".guild-backup-store.json"
SET_MARKER = ".guild-backup-set.json"
SET_RE = re.compile(r"guild-\d{8}T\d{12}Z-[0-9a-f]{8}\Z")
HASH_RE = re.compile(r"[0-9a-f]{64}\Z")
DB_SET_RE = re.compile(r".+_(\d{8}_\d{6})\Z")
DATABASES = ("mind.db", "library.db")
VOICE_FILES = ("voice.toml", "decision.toml", "lexicon.toml", "attitudes.toml")
GAME_DATABASES = ("acore_auth", "acore_characters", "acore_world", "acore_playerbots")
EXCLUDED_DIRS = {"logs", "scratch", "snapshots", "models", "cache", "caches", "__pycache__"}
# Runtime state that is rewritten while the services run (health checks, PID/endpoint
# files); it is regenerated on start and would make the stability scan fail.
EXCLUDED_FILES = {"control.json", "console.key", "mind.lock", "voice.lock", "voice.pid",
                  "health-state.json", "wow-volume.json", "ui-state.json",
                  "dojo-endpoint.json", "worldserver.pid"}
PENDING_PREFIX = ".pending-"
PENDING_MAX_AGE = 24 * 3600
PART_MAX_AGE = 7 * 24 * 3600
STAGING_NAME = ".staging"
CAPTURE_PREFIX = "leeroy-guild-capture-"
VERIFY_STATE = ".verify-state.json"
VERIFY_EVERY = 7 * 24 * 3600
BLOCK = 1024 * 1024


class BackupError(Exception):
    """Messages deliberately contain no file contents or private paths."""


class SourceChanged(BackupError):
    """Retry the entire local capture, never mix files from different attempts."""


def safe_path(path: Path) -> Path:
    """Reject links/junctions on every existing component, including ancestors."""
    path = Path(os.path.abspath(path))
    for part in (*reversed(path.parents), path):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise BackupError(f"Refusing a symlink or junction/reparse point at {part}.")
    return path


def regular(path: Path) -> Path:
    path = safe_path(path)
    if not stat.S_ISREG(path.stat().st_mode):
        raise BackupError("Expected a regular file.")
    return path


def digest(path: Path) -> dict:
    with regular(path).open("rb") as source:
        sha = hashlib.sha256()
        size = 0
        while chunk := source.read(BLOCK):
            sha.update(chunk)
            size += len(chunk)
    return {"sha256": sha.hexdigest(), "size": size}


def json_bytes(value: dict) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2) + "\n").encode("utf-8")


def write_new(path: Path, data: bytes) -> None:
    safe_path(path)
    with path.open("xb") as out:
        out.write(data)
        out.flush()
        os.fsync(out.fileno())


def read_json(path: Path) -> dict:
    with regular(path).open("rb") as source:
        value = json.load(source)
    if not isinstance(value, dict):
        raise BackupError("Invalid backup metadata.")
    return value


def owned(value: dict, store_id: str | None = None) -> bool:
    return (value.get("owner") == OWNER and value.get("format") == 1
            and (store_id is None or value.get("store_id") == store_id))


def metadata(store_id: str, **extra) -> dict:
    return {"owner": OWNER, "format": 1, "store_id": store_id, **extra}


def open_store(destination: Path, *, create: bool) -> tuple[Path, str]:
    destination = safe_path(destination)
    if not destination.is_dir():
        raise BackupError("Destination is unavailable; mount it before backing up.")
    store = safe_path(destination / STORE)
    if not store.exists() and create:
        store.mkdir()
        write_new(store / STORE_MARKER, json_bytes(metadata(uuid.uuid4().hex)))
    marker = read_json(store / STORE_MARKER)
    store_id = marker.get("store_id", "")
    if not owned(marker) or not re.fullmatch(r"[0-9a-f]{32}", store_id):
        raise BackupError("The destination has no valid guild-backup ownership marker.")
    for name in ("sets", "objects"):
        child = safe_path(store / name)
        if create:
            child.mkdir(exist_ok=True)
        if not child.is_dir():
            raise BackupError("The backup store is incomplete.")
    return store, store_id


@contextmanager
def store_lock(store: Path):
    """OS lock survives neither a process crash nor disconnect; no stale PID lock."""
    path = safe_path(store / ".lock")
    with path.open("a+b") as handle:
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise BackupError("Backup store is busy or does not support file locking.") from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def excluded(parts: tuple[str, ...]) -> bool:
    return (any(p.lower() in EXCLUDED_DIRS or p.lower().endswith(".part") for p in parts[:-1])
            or parts[-1].lower() in EXCLUDED_FILES
            or parts[-1].lower().endswith(("-wal", "-shm", "-journal", ".part")))


def inventory(mind: Path, voice: Path, game_set: Path | None) -> dict[str, Path]:
    def unreadable(error):
        raise error  # os.walk otherwise silently omits unreadable directories.

    files = {}
    safe_path(mind)
    if not mind.is_dir():
        raise BackupError("Mind data directory is missing.")
    for base, dirs, names in os.walk(mind, followlinks=False, onerror=unreadable):
        base = safe_path(Path(base))
        dirs[:] = sorted(d for d in dirs if d.lower() not in EXCLUDED_DIRS
                         and not d.lower().endswith(".part"))
        for directory in dirs:
            safe_path(base / directory)
        for name in sorted(names):
            path = base / name
            rel = path.relative_to(mind)
            if excluded(rel.parts) or rel.as_posix() in DATABASES:
                continue
            files["mind-data/" + rel.as_posix()] = regular(path)
    safe_path(voice)
    for name in VOICE_FILES:
        path = safe_path(voice / name)
        if path.exists():
            files["voice-data/" + name] = regular(path)
    if game_set is not None:
        safe_path(game_set)
        if list(game_set.glob("*.err")):
            raise BackupError("Newest game database backup has dump errors.")
        for db in GAME_DATABASES:
            path = regular(game_set / f"{db}.sql")
            if path.stat().st_size == 0:
                raise BackupError("Newest game database backup has an empty dump.")
            files[f"game-db/{game_set.name}/{db}.sql"] = path
    return files


def newest_game_set(root: Path) -> Path:
    candidates = []
    for path in safe_path(root).iterdir():
        match = DB_SET_RE.fullmatch(path.name)
        if match:
            try:
                stamp = datetime.strptime(match[1], "%Y%m%d_%H%M%S")
            except ValueError:
                continue
            safe_path(path)
            if path.is_dir():
                candidates.append((stamp, path.name, path))
    if not candidates:
        raise BackupError("No ac-backup.sh set is available.")
    return max(candidates)[2]


def copy_file(source: Path, target: Path) -> dict:
    target.parent.mkdir(parents=True, exist_ok=True)
    sha = hashlib.sha256()
    size = 0
    with regular(source).open("rb") as inp, target.open("xb") as out:
        while chunk := inp.read(BLOCK):
            out.write(chunk)
            sha.update(chunk)
            size += len(chunk)
    return {"sha256": sha.hexdigest(), "size": size}


def check_sqlite(path: Path) -> None:
    # immutable prevents verification from creating sidecars in a completed set.
    with closing(sqlite3.connect(regular(path).as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
        if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise BackupError("SQLite integrity check failed.")


def sqlite_snapshot(source: Path, target: Path) -> None:
    # One step (pages=-1) copies the whole database inside a single read transaction.
    # In WAL mode that is a consistent snapshot that never blocks the mind's writers.
    # A stepped copy (pages=N) restarts from page 1 whenever another connection writes
    # between steps, so on a large, busy database it may never finish. The cost is that
    # a single step cannot be interrupted, so there is no deadline; the copy is bounded
    # by the database size.
    with closing(sqlite3.connect(regular(source).as_uri() + "?mode=ro", uri=True,
                                timeout=5)) as src:
        with closing(sqlite3.connect(target)) as dst:
            src.backup(dst, pages=-1)
    check_sqlite(target)


def capture(stage: Path, mind: Path, voice: Path, game_set: Path | None) -> dict:
    """A file-content scan on each side of both online database snapshots.

    Databases are individually transactional. A changing ordinary file or
    inventory invalidates this attempt; no writer locks are taken on the mind.
    """
    sources = inventory(mind, voice, game_set)
    files = {name: copy_file(path, stage / name) for name, path in sources.items()}
    for name in DATABASES:
        path = stage / "mind-data" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        sqlite_snapshot(mind / name, path)
    after = inventory(mind, voice, game_set)
    if sources.keys() != after.keys() or any(digest(path) != files[name] for name, path in after.items()):
        raise SourceChanged("Source files changed during capture; no set was published.")
    for name in DATABASES:
        files["mind-data/" + name] = digest(stage / "mind-data" / name)
    return files


def object_path(store: Path, sha: str) -> Path:
    if not HASH_RE.fullmatch(sha):
        raise BackupError("Invalid object digest.")
    return safe_path(store / "objects" / sha[:2] / sha)


def transfer(source: Path, store: Path, entry: dict, *, verify_existing: bool = False) -> bool:
    """Resume only a byte-for-byte matching prefix; verify after close on NAS.

    An object that already exists is trusted by its content-addressed name (it is only
    renamed into place after verification) and its size; re-reading it over the network
    every run is skipped unless verify_existing is set.
    """
    target = object_path(store, entry["sha256"])
    target.parent.mkdir(exist_ok=True)
    if target.exists():
        if regular(target).stat().st_size != entry["size"] or (
                verify_existing and digest(target) != entry):
            raise BackupError("Existing backup object is corrupt; retained sets were left alone.")
        return False
    partial = safe_path(target.with_name(target.name + ".part"))
    offset = 0
    if partial.exists():
        with regular(partial).open("rb") as old, regular(source).open("rb") as src:
            while chunk := old.read(BLOCK):
                if chunk != src.read(len(chunk)):
                    offset = 0
                    break
                offset += len(chunk)
    with regular(source).open("rb") as src, partial.open("r+b" if partial.exists() else "xb") as out:
        out.truncate(offset)
        out.seek(offset)
        src.seek(offset)
        while chunk := src.read(BLOCK):
            out.write(chunk)
        out.flush()
        os.fsync(out.fileno())
    if digest(partial) != entry:
        raise BackupError("Destination copy did not pass SHA-256 verification.")
    os.replace(partial, target)
    return True


def valid_payload_name(name: str) -> bool:
    path = PurePosixPath(name)
    reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{n}" for n in range(1, 10)),
                *(f"LPT{n}" for n in range(1, 10))}
    return (not path.is_absolute() and path.as_posix() == name and len(path.parts) >= 2
            and path.parts[0] in {"mind-data", "voice-data", "game-db"}
            and all(p not in {".", ".."} and not re.search(r'[\\:<>"|?*\x00-\x1f]', p)
                    and not p.endswith((".", " ")) and p.split(".")[0].upper() not in reserved
                    for p in path.parts)
            and not excluded(path.parts[1:]))


def read_set(path: Path, store_id: str, *, expected_name: str | None = None) -> dict:
    safe_path(path)
    name = expected_name or path.name
    if not SET_RE.fullmatch(name) or not path.is_dir():
        raise BackupError("Unrecognized backup set.")
    if {p.name for p in path.iterdir()} != {SET_MARKER, "manifest.json"}:
        raise BackupError("Backup set contains foreign files; it will not be pruned.")
    marker = read_json(path / SET_MARKER)
    manifest = read_json(path / "manifest.json")
    if (not owned(marker, store_id) or not owned(manifest, store_id)
            or marker.get("set_id") != name or manifest.get("set_id") != name
            or marker.get("manifest_sha256") != digest(path / "manifest.json")["sha256"]):
        raise BackupError("Backup set ownership or manifest checksum is invalid.")
    stamp = datetime.strptime(name[6:28], "%Y%m%dT%H%M%S%fZ").replace(tzinfo=timezone.utc)
    if manifest.get("created_utc") != stamp.isoformat():
        raise BackupError("Backup timestamp does not match its set name.")
    files = manifest.get("files")
    if not isinstance(files, dict) or not all("mind-data/" + name in files for name in DATABASES):
        raise BackupError("Backup manifest lacks the required databases.")
    folded = set()
    for name, entry in files.items():
        if (not valid_payload_name(name) or name.casefold() in folded or not isinstance(entry, dict)
                or not isinstance(entry.get("sha256"), str) or not HASH_RE.fullmatch(entry["sha256"])
                or type(entry.get("size")) is not int or entry["size"] < 0):
            raise BackupError("Backup manifest has an unsafe or invalid file entry.")
        folded.add(name.casefold())
    return manifest


def stale_pending(path: Path, store_id: str, now: float) -> bool:
    """An abandoned publish of this script: only its own manifest and marker, old enough."""
    try:
        if (not path.name.startswith(PENDING_PREFIX) or not safe_path(path).is_dir()
                or now - path.stat().st_mtime < PENDING_MAX_AGE
                or {p.name for p in path.iterdir()} != {SET_MARKER, "manifest.json"}):
            return False
        marker, manifest = read_json(path / SET_MARKER), read_json(path / "manifest.json")
        return (owned(marker, store_id) and owned(manifest, store_id)
                and marker.get("manifest_sha256") == digest(path / "manifest.json")["sha256"])
    except (BackupError, OSError, ValueError, TypeError):
        return False


def clean_stale(store: Path, store_id: str, now: float | None = None) -> int:
    """Remove abandoned pending dirs, stale object .part files and old staging dirs."""
    now = time.time() if now is None else now
    removed = 0
    for path in (store / "sets").iterdir():
        if stale_pending(path, store_id, now):
            regular(path / SET_MARKER).unlink()
            regular(path / "manifest.json").unlink()
            path.rmdir()
            removed += 1
    for shard in (store / "objects").iterdir():
        if shard.is_dir() and not shard.is_symlink():
            for path in shard.iterdir():
                if (path.name.endswith(".part") and path.is_file() and not path.is_symlink()
                        and now - path.stat().st_mtime >= PART_MAX_AGE):
                    path.unlink()
                    removed += 1
    staging = store / STAGING_NAME
    if staging.is_dir() and not staging.is_symlink():
        for path in staging.iterdir():
            if (path.name.startswith(CAPTURE_PREFIX) and path.is_dir()
                    and not path.is_symlink() and now - path.stat().st_mtime >= PENDING_MAX_AGE):
                safe_path(path)
                shutil.rmtree(path, ignore_errors=True)  # Only our own prefixed staging dir.
                removed += 1
    return removed


def list_sets(store: Path, store_id: str) -> tuple[list[tuple[Path, dict]], bool]:
    result = []
    foreign = False
    for path in (store / "sets").iterdir():
        try:
            result.append((path, read_set(path, store_id)))
        except (BackupError, OSError, ValueError, TypeError):
            foreign = True  # Preserve unknown/incomplete/corrupt sets and their objects.
    return sorted(result, key=lambda item: item[0].name, reverse=True), foreign


def materialize(store: Path, manifest: dict, target: Path) -> None:
    for name, entry in manifest["files"].items():
        actual = copy_file(object_path(store, entry["sha256"]), target / name)
        if actual != entry:
            raise BackupError("Restore failed SHA-256 verification.")
    for name in DATABASES:
        check_sqlite(target / "mind-data" / name)


def publish(store: Path, store_id: str, stage: Path, files: dict,
            captured_at: datetime | None = None, *, verify_all: bool = False) -> tuple[str, int]:
    if (any(not valid_payload_name(path) for path in files)
            or len({path.casefold() for path in files}) != len(files)):
        raise BackupError("Source names cannot be restored safely on Windows.")
    now = captured_at or datetime.now(timezone.utc)
    name = "guild-" + now.strftime("%Y%m%dT%H%M%S%fZ") + "-" + uuid.uuid4().hex[:8]
    manifest = metadata(store_id, set_id=name, created_utc=now.isoformat(), files=files)
    uploaded = sum(transfer(stage / path, store, entry, verify_existing=verify_all)
                   for path, entry in files.items())
    # Nothing under sets/ is published until every destination object verifies.
    pending = store / "sets" / (PENDING_PREFIX + uuid.uuid4().hex)
    pending.mkdir()
    manifest_bytes = json_bytes(manifest)
    write_new(pending / "manifest.json", manifest_bytes)
    write_new(pending / SET_MARKER, json_bytes(metadata(
        store_id, set_id=name, manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest())))
    read_set(pending, store_id, expected_name=name)
    final = store / "sets" / name
    # Same-directory rename: an interrupted upload cannot become the newest set.
    os.rename(pending, final)
    read_set(final, store_id)
    return name, uploaded


def retained_names(sets: list[tuple[Path, dict]], daily: int, weekly: int) -> set[str]:
    days, weeks, keep = set(), set(), set()
    for path, manifest in sets:
        stamp = datetime.fromisoformat(manifest["created_utc"])
        day, week = stamp.date(), stamp.date().isocalendar()[:2]
        if day not in days and len(days) < daily:
            keep.add(path.name)
            days.add(day)
        if week not in weeks and len(weeks) < weekly:
            keep.add(path.name)
            weeks.add(week)
    return keep


def prune(store: Path, store_id: str, daily: int, weekly: int) -> int:
    clean_stale(store, store_id)
    sets, foreign = list_sets(store, store_id)
    keep = retained_names(sets, daily, weekly)
    referenced, removed_hashes = set(), set()
    removed = 0
    for path, manifest in sets:
        hashes = {e["sha256"] for e in manifest["files"].values()}
        if path.name in keep:
            referenced.update(hashes)
            continue
        read_set(path, store_id)  # Revalidate immediately before touching anything.
        # No recursive delete: these are the only two files an owned set contains.
        regular(path / SET_MARKER).unlink()
        regular(path / "manifest.json").unlink()
        path.rmdir()
        removed_hashes.update(hashes)
        removed += 1
    if not foreign:
        for sha in removed_hashes - referenced:
            path = object_path(store, sha)
            if path.exists() and digest(path)["sha256"] == sha:
                path.unlink()  # Only objects proven to belong to a pruned manifest.
    return removed


def verify_due(store: Path, store_id: str, now: float | None = None) -> bool:
    try:
        state = read_json(store / VERIFY_STATE)
        return (not owned(state, store_id)
                or (time.time() if now is None else now) - float(state["last_full_verify"]) >= VERIFY_EVERY)
    except (BackupError, OSError, ValueError, TypeError, KeyError):
        return True


def record_verified(store: Path, store_id: str) -> None:
    path = safe_path(store / VERIFY_STATE)
    temp = path.with_name(path.name + ".tmp")
    if temp.exists():
        temp.unlink()
    write_new(temp, json_bytes(metadata(store_id, last_full_verify=time.time())))
    os.replace(temp, path)


def local_volume(path: Path) -> bool:
    """True when the path is certainly on a local disk (never UNC or a mapped share)."""
    if os.name != "nt":
        return True
    drive = os.path.splitdrive(os.path.abspath(path))[0]
    if not drive or drive.startswith("\\"):
        return False
    import ctypes
    return ctypes.windll.kernel32.GetDriveTypeW(drive + "\\") == 3  # DRIVE_FIXED


def restore(store: Path, store_id: str, output: Path | None = None) -> int:
    candidates = sorted((p for p in (store / "sets").iterdir() if SET_RE.fullmatch(p.name)),
                        key=lambda p: p.name, reverse=True)
    if not candidates:
        raise BackupError("No completed backup set is available.")
    # Do not silently fall back to an older set when the newest one is corrupt.
    manifest = read_set(candidates[0], store_id)
    if output is not None:
        output = safe_path(output)
        if output.exists() or not output.parent.is_dir():
            raise BackupError("Restore target must be new, with an existing parent directory.")
    with tempfile.TemporaryDirectory(prefix="leeroy-guild-restore-",
                                     dir=output.parent if output else None) as temp:
        restored = Path(temp) / "payload"
        restored.mkdir()
        materialize(store, manifest, restored)
        if output is not None:
            os.rename(restored, output)
    return len(manifest["files"])


def load_config(path: Path) -> tuple[Path, int, int, Path | None]:
    with regular(path).open("rb") as source:
        config = tomllib.load(source)
    if set(config) - {"destination", "keep_daily", "keep_weekly", "staging_dir"}:
        raise BackupError("Unknown backup config field; credentials do not belong here.")
    destination = config.get("destination")
    daily, weekly = config.get("keep_daily", 7), config.get("keep_weekly", 8)
    if (not isinstance(destination, str) or not Path(destination).is_absolute()
            or any(type(n) is not int or n < 0 for n in (daily, weekly)) or daily + weekly < 1):
        raise BackupError("Use an absolute destination and nonnegative retention with at least one set.")
    staging = config.get("staging_dir")
    if staging is not None and (not isinstance(staging, str) or not Path(staging).is_absolute()):
        raise BackupError("staging_dir must be an absolute local directory.")
    return Path(destination), daily, weekly, Path(staging) if staging else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--mind-data", type=Path)
    parser.add_argument("--voice-data", type=Path)
    parser.add_argument("--db-backup-dir", type=Path)
    parser.add_argument("--verify-all", action="store_true",
                        help="re-read every referenced destination object (default: weekly)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--with-db", action="store_true")
    mode.add_argument("--restore-test", action="store_true")
    mode.add_argument("--restore-to", type=Path, metavar="NEW_DIRECTORY")
    args = parser.parse_args(argv)
    try:
        config = args.config
        if config is None:
            if not os.environ.get("LOCALAPPDATA"):
                raise BackupError("Set LOCALAPPDATA or supply --config.")
            config = Path(os.environ["LOCALAPPDATA"]) / "leeroy" / "backup.toml"
        destination, daily, weekly, staging_dir = load_config(config)
        restoring = args.restore_test or args.restore_to is not None
        mind = safe_path(args.mind_data or Path(os.environ.get("LEEROY_MIND_DATA", args.root / "mind-data")))
        voice = safe_path(args.voice_data or Path(os.environ.get("LEEROY_VOICE_DATA", args.root / "voice-data")))
        # Do not accidentally recurse into backups or place restore output in live data.
        store_path = safe_path(destination / STORE)
        for source in (mind, voice, safe_path(args.db_backup_dir or args.root / "db_backup")):
            if store_path.is_relative_to(source) or source.is_relative_to(store_path):
                raise BackupError("Destination must be separate from source data directories.")
            if args.restore_to and safe_path(args.restore_to).is_relative_to(source):
                raise BackupError("Restore into a separate staging directory, never live data.")
        if staging_dir is not None:
            staging_dir = safe_path(staging_dir)
            for source in (mind, voice):
                if staging_dir.is_relative_to(source) or source.is_relative_to(staging_dir):
                    raise BackupError("staging_dir must be separate from source data directories.")
            if staging_dir.is_relative_to(store_path):
                raise BackupError("staging_dir must be outside the backup store.")
        if args.restore_to and safe_path(args.restore_to).is_relative_to(store_path):
            raise BackupError("Restore target must be outside the backup store.")
        store, store_id = open_store(destination, create=not restoring)
        with store_lock(store):
            if restoring:
                count = restore(store, store_id, args.restore_to)
                print(f"Restore verified: {count} files; SQLite integrity ok.")
                return 0
            if staging_dir is None and local_volume(destination):
                staging_dir = store / STAGING_NAME
            if staging_dir is not None:
                safe_path(staging_dir).mkdir(parents=True, exist_ok=True)
            verify_all = args.verify_all or verify_due(store, store_id)
            game_set = newest_game_set(args.db_backup_dir or args.root / "db_backup") if args.with_db else None
            for attempt in range(3):
                with tempfile.TemporaryDirectory(prefix=CAPTURE_PREFIX, dir=staging_dir) as temp:
                    stage = Path(temp)
                    try:
                        captured_at = datetime.now(timezone.utc)
                        files = capture(stage, mind, voice, game_set)
                    except (FileNotFoundError, SourceChanged):
                        if attempt == 2:
                            raise
                        continue
                    name, uploaded = publish(store, store_id, stage, files, captured_at,
                                             verify_all=verify_all)
                    if verify_all:
                        record_verified(store, store_id)
                    removed = prune(store, store_id, daily, weekly)
                    print(f"Backup verified: {name}; {len(files)} files, {uploaded} new objects, {removed} sets pruned.")
                    return 0
        return 1
    except BackupError as exc:
        print(f"Guild backup failed: {exc}", file=sys.stderr)
    except (OSError, ValueError, TypeError, sqlite3.Error):
        # OS/SQLite/TOML errors can include private paths or data. Never echo them.
        print("Guild backup failed: file, config, or database operation failed; no success recorded.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
