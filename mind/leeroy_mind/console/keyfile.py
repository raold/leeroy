"""The console's stable key (--key-file): one key the owner bookmarks once, kept in a file only
his user can read, instead of a new key at each start. By default the file is
%LOCALAPPDATA%\\leeroy\\console.key (default_path), under his profile, not in mind-data.

A missing file is made with a fresh key (secrets.token_urlsafe(32)), private as it is created,
as control.json is (endpoint.create_private). An existing one is used only when it is a regular
file (not a folder, a link or another reparse point), he owns it and no one else may read it
(endpoint.private_problem, read from the opened file) and it holds one key: 43-128 URL-safe
characters (what token_urlsafe(32) makes, so no weak hand-written key), then at most a line
end. `console rotate-key` writes a new key over it the same way control.json is replaced. No
message here shows what a file holds."""
import os
import re
import secrets
import stat
from pathlib import Path

from .. import endpoint

_KEY = re.compile(r"[A-Za-z0-9_-]{43,128}")
MAX_BYTES = 256
_REPARSE_POINT = 0x400  # FILE_ATTRIBUTE_REPARSE_POINT


class KeyFileError(Exception):
    """Why the key file cannot be used, in words that never hold its contents."""


def new_key() -> str:
    return secrets.token_urlsafe(32)


def default_path() -> Path:
    """%LOCALAPPDATA%\\leeroy\\console.key, its folder made when missing. KeyFileError when
    LOCALAPPDATA is not set (then --key-file must name a path)."""
    base = os.environ.get("LOCALAPPDATA")
    if not base or not Path(base).is_absolute():
        raise KeyFileError("LOCALAPPDATA is not set: name the key file (--key-file PATH)")
    folder = Path(base) / "leeroy"
    try:
        folder.mkdir(exist_ok=True)
    except OSError as e:
        raise KeyFileError(f"cannot make {folder} ({type(e).__name__})") from None
    return folder / "console.key"


def _regular(path: Path) -> os.stat_result | None:
    """The file's own lstat when it is a plain regular file; None when there is none.
    KeyFileError for anything else."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as e:
        raise KeyFileError(f"cannot read {path} ({type(e).__name__})") from None
    if not stat.S_ISREG(st.st_mode) or getattr(st, "st_file_attributes", 0) & _REPARSE_POINT:
        raise KeyFileError(f"{path} is not a regular file")
    return st


def _fix(path: Path) -> str:
    return (f"run `python -m leeroy_mind console rotate-key --key-file {path}` to write a "
            "private one")


def _read(path: Path, st: os.stat_result) -> str:
    """The key, once the opened file is the one lstat saw and only he may read it."""
    try:
        f = open(path, "rb")
    except OSError as e:
        raise KeyFileError(f"cannot read {path} ({type(e).__name__})") from None
    with f:
        now = os.fstat(f.fileno())
        if (now.st_dev, now.st_ino) != (st.st_dev, st.st_ino):
            raise KeyFileError(f"{path} changed while it was read")
        try:
            problem = endpoint.private_problem(f.fileno())
        except OSError as e:
            raise KeyFileError(f"cannot read who may open {path} ({type(e).__name__})") \
                from None
        if problem is not None:
            raise KeyFileError(f"{path} is readable by others ({problem}): {_fix(path)}")
        try:
            raw = f.read(MAX_BYTES + 1)
        except OSError as e:
            raise KeyFileError(f"cannot read {path} ({type(e).__name__})") from None
    text = raw.decode("ascii", "replace") if len(raw) <= MAX_BYTES else ""
    for end in ("\r\n", "\n"):
        if text.endswith(end):
            text = text[:-len(end)]
            break
    if not _KEY.fullmatch(text):
        raise KeyFileError(f"{path} is not a console key (43-128 URL-safe characters): "
                           f"{_fix(path)}")
    return text


def _create(path: Path) -> str | None:
    """A new private file at path with a fresh key; None when another made it first."""
    key = new_key()
    try:
        fd = endpoint.create_private(path)
    except FileExistsError:
        return None
    except OSError as e:
        raise KeyFileError(f"cannot create {path} ({type(e).__name__}): is its folder there?") \
            from None
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(f"{key}\n".encode("ascii"))
            f.flush()
            os.fsync(f.fileno())
    except BaseException:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise
    return key


def load(path: Path) -> str:
    """The key in the file at path, made first when there is none. KeyFileError otherwise."""
    path = Path(path)
    st = _regular(path)
    if st is None:
        key = _create(path)
        if key is not None:
            return key
        st = _regular(path)
        if st is None:
            raise KeyFileError(f"{path} went away as it was made")
    return _read(path, st)


def rotate(path: Path) -> str:
    """A new key written over the file at path (made when missing), private; the new key."""
    path = Path(path)
    _regular(path)
    key = new_key()
    try:
        endpoint.write_private(path, f"{key}\n".encode("ascii"))
    except OSError as e:
        raise KeyFileError(f"cannot write {path} ({type(e).__name__})") from None
    return key
