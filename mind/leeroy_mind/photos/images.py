"""JPEG-only upload storage. Files and durable token claims are never pruned."""
import os
import re
import secrets
import stat
from pathlib import Path

from leeroy_mind.imagefiles import read_image

MAX_IMAGE_BYTES = 4 * 1024 * 1024
INCOMING = Path('discord') / 'incoming'
IMAGES = Path('discord') / 'images'
TOKEN_RE = re.compile(r'[0-9a-f]{32}')
_SOF = frozenset({0xc0, 0xc1, 0xc2, 0xc3, 0xc5, 0xc6, 0xc7, 0xc9, 0xca, 0xcb, 0xcd, 0xce, 0xcf})


class ImageRefused(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def jpeg_size(data: bytes) -> tuple[int, int]:
    if not data.startswith(b'\xff\xd8\xff'):
        raise ImageRefused('not_jpeg')
    pos = 2
    while pos < len(data):
        if data[pos] != 0xff:
            break
        while pos < len(data) and data[pos] == 0xff:
            pos += 1
        if pos >= len(data):
            break
        marker = data[pos]
        pos += 1
        if marker in (0, 0xd8, 0xd9, 0xda):
            break
        if marker == 1 or 0xd0 <= marker <= 0xd7:
            continue
        if pos + 2 > len(data):
            break
        size = int.from_bytes(data[pos:pos+2], 'big')
        if size < 2 or pos + size > len(data):
            break
        if marker in _SOF:
            if size < 8 or size != 8 + 3 * data[pos+7]:
                break
            return int.from_bytes(data[pos+5:pos+7], 'big'), int.from_bytes(data[pos+3:pos+5], 'big')
        pos += size
    raise ImageRefused('not_jpeg')


def check_jpeg(data: bytes) -> tuple[int, int]:
    if len(data) > MAX_IMAGE_BYTES:
        raise ImageRefused('too_big')
    w, h = jpeg_size(data)
    if not (16 <= w <= 8192 and 16 <= h <= 8192):
        raise ImageRefused('bad_size')
    return w, h


def _safe_dir(root: Path, relative: Path, *, create: bool) -> Path:
    current = root
    for part in (None, *relative.parts):
        if part is not None:
            current /= part
        try:
            if create:
                current.mkdir(exist_ok=True)
            info = current.lstat()
        except FileNotFoundError:
            raise ImageRefused('missing') from None
        if (stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400
                or not stat.S_ISDIR(info.st_mode)):
            raise ImageRefused('link')
    return current


def _write_new(path: Path, data: bytes) -> None:
    # Exclusive creation rejects a target symlink as well as an existing file.
    # On an interrupted write the partial file stays for inspection.
    with path.open('xb') as stream:
        stream.write(data)


def stage(data_dir: Path, data: bytes) -> str:
    check_jpeg(data)
    folder = _safe_dir(Path(data_dir), INCOMING, create=True)
    for _ in range(16):
        token = secrets.token_hex(16)
        try:
            _write_new(folder / f'{token}.jpg', data)
        except FileExistsError:
            continue
        _safe_dir(Path(data_dir), INCOMING, create=False)
        return token
    raise ImageRefused('busy')


def adopt(data_dir: Path, token: str, message_id: int) -> tuple[str, int, int, int]:
    if not isinstance(token, str) or TOKEN_RE.fullmatch(token) is None:
        raise ImageRefused('bad_token')
    if type(message_id) is not int or message_id <= 0:
        raise ImageRefused('bad_token')
    root = Path(data_dir)
    incoming = _safe_dir(root, INCOMING, create=False)
    dest = _safe_dir(root, IMAGES, create=True) / f'{message_id}.jpg'
    source = incoming / f'{token}.jpg'
    adopted = incoming / f'{token}.adopted'
    claim = incoming / f'{token}.claimed'
    if claim.exists() or adopted.exists():
        raise ImageRefused('reused')
    try:
        info = source.lstat()
    except FileNotFoundError:
        raise ImageRefused('missing') from None
    if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
        raise ImageRefused('link')
    data = read_image(source, root)
    if data is None:
        raise ImageRefused('too_big' if info.st_size > MAX_IMAGE_BYTES else 'not_jpeg')
    w, h = check_jpeg(data)
    try:
        # Atomic cross-platform claim: two posts cannot adopt the same token,
        # even if the first transaction or process dies after creating its image.
        _write_new(claim, str(message_id).encode('ascii'))
        try:
            _write_new(dest, data)
        except FileExistsError:
            # A rolled-back post may have left an image with the next SQL id.
            # Keep it, and store this attempt at its own never-reused path.
            dest = dest.with_name(f'{message_id}-{token}.jpg')
            _write_new(dest, data)
    except FileExistsError:
        raise ImageRefused('reused') from None
    _safe_dir(root, INCOMING, create=False)
    _safe_dir(root, IMAGES, create=False)
    source.rename(adopted)
    return (IMAGES / dest.name).as_posix(), w, h, len(data)
