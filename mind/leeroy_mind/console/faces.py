"""A member's chosen profile photo, read for the console's /api/face. The console only reads
under mind-data/selfies/; whoever chooses a photo replaces chosen.jpg by rename, so the file is
read in one go and closed at once."""
import os
import stat
from pathlib import Path

MAX_FACE_BYTES = 4 * 1024 * 1024
SIZES = ("sm", "md", "lg")
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


def read_face(data_dir: Path, guid: int) -> bytes | None:
    """The bytes of selfies/<guid>/chosen.jpg when it is a regular file (not a link) of at most
    MAX_FACE_BYTES that starts like a JPEG; None otherwise."""
    path = Path(data_dir) / "selfies" / str(guid) / "chosen.jpg"
    return read_image(path, Path(data_dir) / "selfies")
