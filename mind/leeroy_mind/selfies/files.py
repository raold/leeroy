"""Profile photo files, under mind-data/selfies/<guid>/:

    sets/<n>/c0.jpg .. c3.jpg       a ready set's four candidates (1024 px, square)
    sets/<n>/c0.thumb.jpg ..        and their thumbnails
    sets/<n>.part/                  a set the night is rendering (renamed to sets/<n> when done)
    chosen.jpg                      the photo in force; absent until a pick
    history/<pick>.jpg              the photo each change put in force
    snaps/<id>.jpg, <id>.thumb.jpg  a situational photo (P4)

and selfies/guild/<id>.jpg, <id>.thumb.jpg, with <id>.part/ holding a guild photo's tiles while
it renders.

chosen.jpg is only ever replaced by writing a temporary file beside it and renaming it over,
so a reader sees the old photo or the new one, never part of one. Windows refuses to replace a
file another process holds open (the console's face endpoint reads it), so the rename is
retried. The database is the truth: sync_chosen makes the files agree with it after a crash
between a pick's commit and its files. Every path is built from integers only, so no value from
a request or the database can name anything else."""
import os
import shutil
import time
from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace

REPLACE_TRIES = 40  # 2 s of 50 ms tries, as endpoint.write_key_file moves control.json
REPLACE_WAIT_S = 0.05
_GUID_MAX = (1 << 32) - 1
_SET_MAX = (1 << 31) - 1
_ROWID_MAX = (1 << 63) - 1


def _int(name: str, value, lo: int, hi: int) -> int:
    if type(value) is not int:
        raise TypeError(f"{name} must be an int, not {type(value).__name__}")
    if not lo <= value <= hi:
        raise ValueError(f"{name} must be {lo}-{hi}")
    return value


def member_dir(data_dir, guid: int) -> Path:
    return Path(data_dir) / "selfies" / str(_int("guid", guid, 1, _GUID_MAX))


def set_dir(data_dir, guid: int, set_no: int) -> Path:
    return member_dir(data_dir, guid) / "sets" / str(_int("set_no", set_no, 1, _SET_MAX))


def staging_dir(data_dir, guid: int, set_no: int) -> Path:
    return member_dir(data_dir, guid) / "sets" / f"{_int('set_no', set_no, 1, _SET_MAX)}.part"


def candidate(data_dir, guid: int, set_no: int, idx: int, *, thumb: bool = False) -> Path:
    i = _int("idx", idx, 0, 3)
    return set_dir(data_dir, guid, set_no) / (f"c{i}.thumb.jpg" if thumb else f"c{i}.jpg")


def chosen(data_dir, guid: int) -> Path:
    return member_dir(data_dir, guid) / "chosen.jpg"


def history(data_dir, guid: int, pick_id: int) -> Path:
    n = _int("pick_id", pick_id, 1, _ROWID_MAX)
    return member_dir(data_dir, guid) / "history" / f"{n}.jpg"


def _retrying(action, *args) -> None:
    """action(*args), tried again while Windows says another process holds the file."""
    for attempt in range(REPLACE_TRIES):
        try:
            action(*args)
            return
        except PermissionError:
            if attempt == REPLACE_TRIES - 1:
                raise
            time.sleep(REPLACE_WAIT_S)


def replace_atomic(src: Path, dst: Path) -> None:
    """dst holds src's bytes: copied whole to a temporary file beside dst, flushed to disk, then
    renamed over it. The temporary file never survives, and dst is never missing or partial."""
    src, dst = Path(src), Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(f".{dst.name}.{os.getpid()}.tmp")
    try:
        with open(src, "rb") as f, open(tmp, "wb") as out:  # replaces a crash's leftover
            shutil.copyfileobj(f, out)
            out.flush()
            os.fsync(out.fileno())
        _retrying(os.replace, tmp, dst)
    except BaseException:
        with suppress(OSError):
            tmp.unlink()
        raise


def publish_set(data_dir, guid: int, set_no: int) -> None:
    """The set's staging folder renamed to its folder, retried while Windows says another
    process holds a file in it (Defender or the indexer reading the new JPEGs)."""
    _retrying(os.replace, staging_dir(data_dir, guid, set_no), set_dir(data_dir, guid, set_no))


def _held(function, path, exc: BaseException) -> None:
    """shutil.rmtree's onexc: a file or folder another process holds is removed again as
    _retrying does; one already gone is fine; anything else is raised."""
    if isinstance(exc, FileNotFoundError):
        return
    if not isinstance(exc, PermissionError) or function not in (os.unlink, os.rmdir):
        raise exc
    _retrying(function, path)


def clear_stale(data_dir, guid: int, set_no: int) -> None:
    """Remove what a render of this set left without its ready row: the set's folder and its
    staging folder, a file someone holds retried as publish_set does. Only for a set the
    database says is still queued."""
    for d in (set_dir(data_dir, guid, set_no), staging_dir(data_dir, guid, set_no)):
        with suppress(FileNotFoundError):
            shutil.rmtree(d, onexc=_held)


def _same(path: Path, data: bytes) -> bool:
    try:
        return path.read_bytes() == data
    except FileNotFoundError:
        return False


def sync_chosen(data_dir, guid: int, current) -> bool:
    """Make the member's files agree with the photo in force (`store.current`): with one,
    history/<pick>.jpg and chosen.jpg hold its candidate's bytes; without one, there is no
    chosen.jpg (history stays). True when a file was changed. A photo whose candidate and
    history copy are both gone is FileNotFoundError, and chosen.jpg is left as it is."""
    target = chosen(data_dir, guid)
    if current is None:
        if not target.exists():
            return False
        _retrying(_unlink, target)
        return True
    if current.guid != guid:
        raise ValueError("that photo is another member's")
    src = candidate(data_dir, guid, current.set_no, current.idx)
    kept = history(data_dir, guid, current.pick)
    if not src.exists():
        src = kept  # the set's files are gone; the history copy is the photo
    data = src.read_bytes()
    changed = False
    for dst in (kept, target):
        if not _same(dst, data):
            replace_atomic(src, dst)
            changed = True
    return changed


def _unlink(path: Path) -> None:
    with suppress(FileNotFoundError):
        path.unlink()


def snap(data_dir, guid: int, snap_id: int, *, thumb: bool = False) -> Path:
    n = _int("snap_id", snap_id, 1, _ROWID_MAX)
    return member_dir(data_dir, guid) / "snaps" / (f"{n}.thumb.jpg" if thumb else f"{n}.jpg")


def _guild_dir(data_dir) -> Path:
    return Path(data_dir) / "selfies" / "guild"


def guild_photo(data_dir, snap_id: int, *, thumb: bool = False) -> Path:
    n = _int("snap_id", snap_id, 1, _ROWID_MAX)
    return _guild_dir(data_dir) / (f"{n}.thumb.jpg" if thumb else f"{n}.jpg")


def guild_tiles(data_dir, snap_id: int) -> Path:
    return _guild_dir(data_dir) / f"{_int('snap_id', snap_id, 1, _ROWID_MAX)}.part"


def snap_file(data_dir, row, *, thumb: bool = False) -> Path:
    if row.kind == "guild":
        return guild_photo(data_dir, row.id, thumb=thumb)
    return snap(data_dir, row.guid, row.id, thumb=thumb)


def sync_snaps(store, data_dir, *, now: float) -> int:
    """Make the photo files agree with the rows: a ready row whose file is missing ends
    `missing`; every `*.part` folder and `*.tmp` file under snaps/ and guild/ is removed (a
    render's leftovers). Returns the rows changed."""
    from .snaps import SnapError, expire
    with store.transaction():
        ready = store.conn.execute(
            "SELECT id, kind, guid FROM selfie_snap WHERE status='ready' ORDER BY id").fetchall()
    changed = 0
    for r in ready:
        row = SimpleNamespace(id=r[0], kind=r[1], guid=r[2])
        if not snap_file(data_dir, row).exists():
            with suppress(SnapError):
                expire(store, row.id, "missing", now=now)
                changed += 1
    root = Path(data_dir) / "selfies"
    for d in [_guild_dir(data_dir), *sorted(root.glob("[0-9]*/snaps"))]:
        if not d.is_dir():
            continue
        for leftover in d.iterdir():
            if leftover.is_dir() and leftover.name.endswith(".part"):
                shutil.rmtree(leftover, onexc=_held)
            elif leftover.is_file() and leftover.name.endswith(".tmp"):
                _retrying(_unlink, leftover)
    return changed
