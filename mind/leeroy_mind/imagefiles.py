"""Bounded JPEG reads that reject symlinks, junctions and replacement files."""
import os
import stat
from pathlib import Path

MAX_FACE_BYTES = 4 * 1024 * 1024
_JPEG = b"\xff\xd8\xff"
_REPARSE_POINT = 0x400


def image_info(path: Path, root: Path, *, maximum=MAX_FACE_BYTES):
    """A bounded regular image path with no symlink/junction anywhere below its image root."""
    path, root = Path(path), Path(root)
    try:
        relative = path.relative_to(root)
        if not relative.parts or any(part in (".", "..") for part in relative.parts):
            return None
        current = root
        for part in (None, *relative.parts):
            if part is not None:
                current /= part
            info = os.lstat(current)
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & _REPARSE_POINT:
                return None
            if current != path and not stat.S_ISDIR(info.st_mode):
                return None
        path.resolve().relative_to(root.resolve())
        return info if stat.S_ISREG(info.st_mode) and 0 < info.st_size <= maximum else None
    except (OSError, ValueError):
        return None


def read_image(path: Path, root: Path, *, maximum=MAX_FACE_BYTES) -> bytes | None:
    info = image_info(path, root, maximum=maximum)
    if info is None:
        return None
    try:
        with open(path, "rb") as stream:
            opened = os.fstat(stream.fileno())
            # Recheck the opened identity and ancestors; a replacement is not the checked file.
            if ((opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino)
                    or image_info(path, root, maximum=maximum) is None):
                return None
            data = stream.read(maximum + 1)
        return data if len(data) <= maximum and data.startswith(_JPEG) else None
    except OSError:
        return None


