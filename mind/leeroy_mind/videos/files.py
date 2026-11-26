"""His video uploads: the console stages and reads a video once, and the mind adopts it with the
post that names its token. Videos, stills, facts and claims are never deleted.

At upload ffprobe confirms a picture stream and reads the length, and ffmpeg takes up to three
stills (at 10%, 50% and 85% of it; one still for a clip under two seconds or of unknown length)
side by side in one JPEG: the picture the live model looks at and the poster the page shows.
Inputs are this PC's own files only (`-protocol_whitelist file`, the demuxer forced from the
declared type) and the stills come back on a pipe: nothing is fetched or sent anywhere."""
import json
import math
import os
import re
import secrets
import shutil
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from leeroy_mind import clock
from leeroy_mind.imagefiles import image_info, read_image
from leeroy_mind.photos.images import (IMAGES, TOKEN_RE, ImageRefused, _safe_dir, _write_new,
                                       check_jpeg)

MAX_VIDEO_BYTES = 50 * 1024 * 1024
# the declared type -> (its file suffix, the ffmpeg demuxer forced for it)
KINDS = {'video/mp4': ('.mp4', 'mov'), 'video/quicktime': ('.mov', 'mov'),
         'video/webm': ('.webm', 'matroska')}
INCOMING = Path('discord') / 'incoming-video'
VIDEOS = Path('discord') / 'videos'
MINUTE_MAX = 2
DAY_MAX = 50
DAY_KEY = 'videos:uploads:{}'
META_KEY = 'discord:video:{}'
PROBE_S = 20.0
STILLS_S = 60.0
STILL_BOX = (768, 512)  # each still fits inside it, so three side by side stay under 8192 wide
GAP = 6  # black pixels between stills, so the model sees separate moments
_BOXES = frozenset({b'ftyp', b'moov', b'mdat', b'free', b'wide', b'skip', b'pnot'})
_EBML = b'\x1a\x45\xdf\xa3'
_NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)  # the console may run under pythonw
_STEM = re.compile(r'[1-9]\d{0,18}(?:-[0-9a-f]{32})?')
WORDS = {
    'off': 'Videos are off in this mind.',
    'tools': "Videos need ffmpeg and ffprobe on this PC's PATH.",
    'not_video': 'Choose an mp4, webm or mov video.',
    'too_big': 'That video is over 50 MiB.',
    'unreadable': 'That video could not be read. Try an mp4.',
    'no_picture': 'That file has no picture to show.',
    'bad_token': 'Choose the video again: its upload token is invalid.',
    'missing': 'That uploaded video is missing. Choose it again.',
    'link': 'That video path is a link and cannot be used.',
    'reused': 'That upload was already used. Choose the video again.',
    'busy': 'The video could not be staged. Try again.',
    'minute': 'Wait a minute before sending another video (two per minute).',
    'day': "Today's 50-video allowance is used up.",
}


class VideoRefused(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class Facts:
    """What the upload read: the declared type, the size, the length in ms (None when the file
    does not say), one still's size as shown (rotation applied) and how many stills."""
    mime: str
    bytes: int
    ms: int | None
    w: int
    h: int
    frames: int

    def as_dict(self) -> dict:
        return {'mime': self.mime, 'bytes': self.bytes, 'ms': self.ms, 'w': self.w, 'h': self.h,
                'frames': self.frames}

    @classmethod
    def parse(cls, value) -> 'Facts | None':
        if not isinstance(value, dict):
            return None
        facts = cls(*(value.get(k) for k in ('mime', 'bytes', 'ms', 'w', 'h', 'frames')))
        ok = (facts.mime in KINDS and type(facts.bytes) is int and 0 < facts.bytes <= MAX_VIDEO_BYTES
              and (facts.ms is None or type(facts.ms) is int and 0 < facts.ms < 86_400_000)
              and all(type(v) is int and 16 <= v <= 8192 for v in (facts.w, facts.h))
              and type(facts.frames) is int and facts.frames in (1, 3))
        return facts if ok else None


def tools() -> tuple[str, str] | None:
    """(ffmpeg, ffprobe) as found on PATH, or None."""
    ffmpeg, ffprobe = shutil.which('ffmpeg'), shutil.which('ffprobe')
    return (ffmpeg, ffprobe) if ffmpeg and ffprobe else None


def _run(args: list[str], timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout,
                          creationflags=_NO_WINDOW, check=False)


def _input(demuxer: str, path: Path) -> list[str]:
    return ['-protocol_whitelist', 'file', '-f', demuxer, '-i', 'file:' + os.fspath(path)]


def _dir(root: Path, relative: Path, *, create: bool) -> Path:
    try:
        return _safe_dir(root, relative, create=create)
    except ImageRefused as error:
        raise VideoRefused(error.code) from None


def _read(path: Path, root: Path, maximum: int) -> bytes | None:
    """A bounded read of a regular file with no link from `root` down, checked again once open."""
    info = image_info(path, root, maximum=maximum)
    if info is None:
        return None
    try:
        with open(path, 'rb') as stream:
            opened = os.fstat(stream.fileno())
            if ((opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino)
                    or image_info(path, root, maximum=maximum) is None):
                return None
            data = stream.read(maximum + 1)
    except OSError:
        return None
    return data if len(data) <= maximum else None


def _starts_as(data: bytes, demuxer: str) -> bool:
    return data[:4] == _EBML if demuxer == 'matroska' else len(data) >= 12 and data[4:8] in _BOXES


def sniff(data: bytes, mime: str) -> tuple[str, str]:
    """(suffix, demuxer) when the bytes start as their declared type does; VideoRefused if not."""
    kind = KINDS.get(mime)
    if kind is None:
        raise VideoRefused('not_video')
    if len(data) > MAX_VIDEO_BYTES:
        raise VideoRefused('too_big')
    if not _starts_as(data, kind[1]):
        raise VideoRefused('not_video')
    return kind


def probe(path: Path, demuxer: str, ffprobe: str) -> int | None:
    """The length in ms, None when the file does not say; VideoRefused with no picture stream."""
    try:
        r = _run([ffprobe, '-v', 'error', *_input(demuxer, path), '-select_streams', 'v:0',
                  '-show_entries', 'stream=codec_type:format=duration', '-of', 'json'], PROBE_S)
        info = json.loads(r.stdout) if r.returncode == 0 else None
    except (subprocess.TimeoutExpired, OSError, ValueError):
        info = None
    if not isinstance(info, dict):
        raise VideoRefused('unreadable')
    streams = info.get('streams')
    if not isinstance(streams, list) or not any(
            isinstance(s, dict) and s.get('codec_type') == 'video' for s in streams):
        raise VideoRefused('no_picture')
    form = info.get('format')
    try:
        ms = float(form.get('duration')) * 1000 if isinstance(form, dict) else math.nan
    except (TypeError, ValueError):
        return None
    return round(ms) if math.isfinite(ms) and 1 <= ms < 86_400_000 else None


def _times(ms: int | None) -> list[float]:
    if ms is None:
        return [0.0]
    if ms < 2000:
        return [ms / 2000]
    return [round(ms * f / 1000, 3) for f in (0.1, 0.5, 0.85)]


def stills(path: Path, demuxer: str, ms: int | None, ffmpeg: str) -> tuple[bytes, int, int, int]:
    """(JPEG, stills, one still's width, its height): the stills side by side, left to right in
    time, GAP black pixels apart. When the spread cannot be taken, one still from the start."""
    w, h = STILL_BOX
    scale = f'scale=w={w}:h={h}:force_original_aspect_ratio=decrease:force_divisible_by=2,setsar=1'
    gap = f',pad=w=iw+{GAP}:h=ih:x=0:y=0:color=black'
    for times in (_times(ms), [0.0]):
        args = [ffmpeg, '-v', 'error', '-nostdin']
        for t in times:
            args += ['-ss', f'{t:.3f}', *_input(demuxer, path)]
        n = len(times)
        graph = ';'.join(f'[{i}:v:0]{scale}{gap if i < n - 1 else ""}[s{i}]' for i in range(n))
        graph += ';' + ''.join(f'[s{i}]' for i in range(n)) + (f'hstack=inputs={n}[out]' if n > 1
                                                                 else 'null[out]')
        args += ['-filter_complex', graph, '-map', '[out]', '-frames:v', '1', '-q:v', '3',
                 '-f', 'image2pipe', '-c:v', 'mjpeg', 'pipe:1']
        try:
            r = _run(args, STILLS_S)
        except (subprocess.TimeoutExpired, OSError):
            continue
        if r.returncode != 0 or not r.stdout:
            continue
        try:
            sw, sh = check_jpeg(r.stdout)
        except ImageRefused:
            continue
        still = (sw - GAP * (n - 1)) // n
        if still >= 16:
            return r.stdout, n, still, sh
    raise VideoRefused('unreadable')


def stage(data_dir: Path, data: bytes, mime: str, found: tuple[str, str] | None) -> tuple[str, Facts]:
    """Write the video under discord/incoming-video, read it, and write its stills (<token>.jpg)
    and facts (<token>.json) beside it; (token, facts). A video ffmpeg cannot read stays there,
    neither counted nor adoptable."""
    suffix, demuxer = sniff(data, mime)
    if found is None:
        raise VideoRefused('tools')
    ffmpeg, ffprobe = found
    root = Path(data_dir)
    folder = _dir(root, INCOMING, create=True)
    for _ in range(16):
        token = secrets.token_hex(16)
        try:
            _write_new(folder / f'{token}{suffix}', data)
            break
        except FileExistsError:
            continue
    else:
        raise VideoRefused('busy')
    path = folder / f'{token}{suffix}'
    ms = probe(path, demuxer, ffprobe)
    sheet, frames, w, h = stills(path, demuxer, ms, ffmpeg)
    facts = Facts(mime, len(data), ms, w, h, frames)
    _dir(root, INCOMING, create=False)
    _write_new(folder / f'{token}.jpg', sheet)
    _write_new(folder / f'{token}.json', json.dumps(facts.as_dict()).encode('ascii'))
    return token, facts


def staged_count(data_dir: Path, day: str) -> int:
    """Today's accepted uploads (their facts files), counted without the mind's database."""
    try:
        folder = _dir(Path(data_dir), INCOMING, create=False)
    except VideoRefused as error:
        if error.code == 'missing':
            return 0
        raise
    count = 0
    for path in folder.iterdir():
        if path.suffix != '.json' or TOKEN_RE.fullmatch(path.stem) is None:
            continue
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            continue
        if clock.local_at(info.st_mtime).date().isoformat() == day:
            count += 1
    return count


def day_count(conn, day: str) -> int:
    row = conn.execute('SELECT value FROM meta WHERE key=?', (DAY_KEY.format(day),)).fetchone()
    try:
        return max(0, int(row[0])) if row else 0
    except (ValueError, TypeError):
        return DAY_MAX  # a broken counter never grants another allowance


def adopt(data_dir: Path, token: str, message_id: int) -> tuple[str, int, int, int, dict]:
    """Move the staged video under discord/videos and copy its stills under discord/images, both
    named by the message; (stills path, width, height, bytes, the facts kept in meta). A token
    adopts once: its claim file is written first and kept."""
    if not isinstance(token, str) or TOKEN_RE.fullmatch(token) is None:
        raise VideoRefused('bad_token')
    if type(message_id) is not int or message_id <= 0:
        raise VideoRefused('bad_token')
    root = Path(data_dir)
    incoming = _dir(root, INCOMING, create=False)
    claim = incoming / f'{token}.claimed'
    if claim.exists():
        raise VideoRefused('reused')
    raw = _read(incoming / f'{token}.json', root, 4096)
    try:
        facts = Facts.parse(json.loads(raw)) if raw is not None else None
    except ValueError:
        facts = None
    if facts is None:
        raise VideoRefused('missing')
    suffix = KINDS[facts.mime][0]
    source = incoming / f'{token}{suffix}'
    info = image_info(source, root, maximum=MAX_VIDEO_BYTES)
    sheet = read_image(incoming / f'{token}.jpg', root)
    if info is None or info.st_size != facts.bytes or sheet is None:
        raise VideoRefused('missing')
    try:
        sw, sh = check_jpeg(sheet)
    except ImageRefused:
        raise VideoRefused('missing') from None
    videos = _dir(root, VIDEOS, create=True)
    images = _dir(root, IMAGES, create=True)
    try:
        _write_new(claim, str(message_id).encode('ascii'))
    except FileExistsError:
        raise VideoRefused('reused') from None
    still = images / f'{message_id}.jpg'
    try:
        _write_new(still, sheet)
    except FileExistsError:
        # A rolled-back post may have left files with this id: keep them, take names of our own.
        still = images / f'{message_id}-{token}.jpg'
        _write_new(still, sheet)
    video = videos / f'{message_id}{suffix}'
    if os.path.lexists(video):  # os.rename would replace it on POSIX
        video = videos / f'{message_id}-{token}{suffix}'
    try:
        os.rename(source, video)
    except OSError:
        raise VideoRefused('busy') from None
    for relative in (INCOMING, VIDEOS, IMAGES):
        _dir(root, relative, create=False)
    return ((IMAGES / still.name).as_posix(), sw, sh, len(sheet),
            {**facts.as_dict(), 'path': (VIDEOS / video.name).as_posix()})


def _video_path(path, mime: str) -> bool:
    if not isinstance(path, str) or len(path) > 200:
        return False
    p = PurePosixPath(path)
    return (p.parent == PurePosixPath(VIDEOS.as_posix()) and p.suffix == KINDS[mime][0]
            and _STEM.fullmatch(p.stem) is not None)


def by_messages(conn, ids) -> dict[int, dict]:
    """Each message among `ids` that carries a video: its facts and path (meta discord:video:<id>,
    written with the post). A malformed entry is left out."""
    ids = [i for i in dict.fromkeys(ids) if type(i) is int and i > 0]
    if not ids:
        return {}
    rows = conn.execute("SELECT key, value FROM meta WHERE key IN "
                        "(SELECT 'discord:video:' || value FROM json_each(?))", (json.dumps(ids),))
    out = {}
    for key, value in rows:
        try:
            raw = json.loads(value)
        except (TypeError, ValueError):
            continue
        facts = Facts.parse(raw)
        if facts is None or not _video_path(raw.get('path'), facts.mime):
            continue
        out[int(key.rsplit(':', 1)[1])] = {**facts.as_dict(), 'path': raw['path']}
    return out


def read_video(path: Path, root: Path, mime: str) -> bytes | None:
    data = _read(path, root, MAX_VIDEO_BYTES)
    return data if data is not None and _starts_as(data, KINDS[mime][1]) else None


def duration_words(ms: int | None) -> str:
    """'12 seconds', 'about 3 minutes', or '' when the length is unknown."""
    if not ms:
        return ''
    seconds = max(1, round(ms / 1000))
    if seconds < 90:
        return f"{seconds} second{'' if seconds == 1 else 's'}"
    return f'about {round(seconds / 60)} minutes'
