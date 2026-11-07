"""A member's chosen profile photo, read for the console's /api/face. The console only reads
under mind-data/selfies/; whoever chooses a photo replaces chosen.jpg by rename, so the file is
read in one go and closed at once."""
import os
import stat
from pathlib import Path

MAX_FACE_BYTES = 4 * 1024 * 1024
SIZES = ("sm", "md", "lg")
_JPEG = b"\xff\xd8\xff"


def read_face(data_dir: Path, guid: int) -> bytes | None:
    """The bytes of selfies/<guid>/chosen.jpg when it is a regular file (not a link) of at most
    MAX_FACE_BYTES that starts like a JPEG; None otherwise."""
    path = Path(data_dir) / "selfies" / str(guid) / "chosen.jpg"
    try:
        info = os.lstat(path)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FACE_BYTES:
            return None
        with open(path, "rb") as f:
            data = f.read(MAX_FACE_BYTES + 1)
    except OSError:
        return None
    if len(data) > MAX_FACE_BYTES or not data.startswith(_JPEG):
        return None
    return data
