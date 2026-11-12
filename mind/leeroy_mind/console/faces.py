"""Chosen profile photos; shared file checks are re-exported for existing callers."""
from pathlib import Path

from ..imagefiles import MAX_FACE_BYTES, image_info, read_image

SIZES = ("sm", "md", "lg")


def read_face(data_dir: Path, guid: int) -> bytes | None:
    """The bytes of selfies/<guid>/chosen.jpg when it is a regular file (not a link) of at most
    MAX_FACE_BYTES that starts like a JPEG; None otherwise."""
    path = Path(data_dir) / "selfies" / str(guid) / "chosen.jpg"
    return read_image(path, Path(data_dir) / "selfies")
