"""The worker's request lines: one strict JSON form per op, parsed here and nowhere else.

The mind speaks this protocol from its own code (it never imports this package); the shapes
and limits are the contract."""
import json
from dataclasses import dataclass
from pathlib import PurePath
from typing import Literal

MAX_LINE = 65536
DEFAULT_NSFW_MAX = 0.2
MAX_PHRASES = 64
MAX_PHRASE_CHARS = 500
RENDER_KEYS = frozenset({"id", "op", "phrases", "negative", "seed", "steps", "cfg", "size",
                         "out", "thumb", "thumb_size"})
IDENTITY_KEYS = frozenset({"ref", "crop", "scale"})
# The detailer (detail.py), all optional: without detail_faces or detail_hands true there is no
# pass and the render is what it was before the detailer existed.
DETAIL_KEYS = frozenset({"detail_faces", "detail_hands", "detail_face_denoise",
                         "detail_hand_denoise", "detail_size", "detail_face_max",
                         "detail_identity_margin"})
DETAIL_FACE_DENOISE = 0.35
DETAIL_HAND_DENOISE = 0.40
DETAIL_FACE_MAX = 0.18  # a face box over this share of the frame's shorter side has its pixels
DETAIL_FACE_MAX_RANGE = (0.05, 0.5)
DETAIL_IDENTITY_MARGIN = 0.03  # how far her face's cosine to her reference may fall in the pass
DETAIL_IDENTITY_MARGIN_RANGE = (0.0, 1.0)
DETAIL_DENOISE_RANGE = (0.1, 0.7)
DETAIL_SIZE = 1024
DETAIL_SIZES = (768, 896, 1024)
GRID_KEYS = frozenset({"id", "op", "tiles", "tile", "gutter", "out", "thumb", "thumb_size"})
CAPTION_KEYS = frozenset({"id", "op", "base_path", "top", "bottom", "out_path"})
CRITIC_KEYS = frozenset({"id", "op", "path"})  # and optionally ref with crop, both or neither
MAX_CAPTION = 60  # characters a caption line may hold
# The characters the bundled caption font (fonts/Anton-Regular.ttf) draws: printable ASCII,
# Latin-1 and Latin Extended-A, and the common typographic marks. Anything else (an emoji, CJK,
# Cyrillic) would draw as a missing-glyph box, so a caption holding it is a bad request. The mind
# keeps the same set (culture/memes.py CAPTION_CHARS); tests check the font and the drift.
CAPTION_CHARS = (frozenset(map(chr, (*range(0x20, 0x7F), *range(0xA1, 0x180)))) | frozenset("‘’“”–—…•€™")) - {"µ"}  # the micro sign capitalises to a Greek letter the font lacks
MAX_TILES = 16
SQUARE_TOLERANCE = 0.001
PORTRAIT_SIZE = (832, 1216)  # width, height of shape "portrait" (needs size 1024)


class RenderFailed(Exception):
    """A render that failed with a protocol error name (oom, ...) as its message."""


@dataclass(frozen=True)
class Detail:
    """What a render request asks of the detailer (detail.py)."""
    faces: bool = True
    hands: bool = False
    face_denoise: float = DETAIL_FACE_DENOISE
    hand_denoise: float = DETAIL_HAND_DENOISE
    size: int = DETAIL_SIZE
    face_max: float = DETAIL_FACE_MAX
    identity_margin: float = DETAIL_IDENTITY_MARGIN


@dataclass(frozen=True)
class Identity:
    """The reference face: `crop` is a square (l, t, r, b) in fractions of `ref`."""
    ref: str
    crop: tuple[float, float, float, float]
    scale: float


@dataclass(frozen=True)
class Render:
    id: str
    phrases: tuple[tuple[str, int], ...]
    negative: tuple[tuple[str, int], ...]
    seed: int
    steps: int
    cfg: float
    size: int
    out: str
    thumb: str
    thumb_size: int
    nsfw_max: float
    identity: Identity | None = None
    shape: Literal["square", "portrait"] = "square"
    detail: Detail | None = None  # the detailer's pass over the rendered image (detail.py)


@dataclass(frozen=True)
class Grid:
    id: str
    tiles: tuple[str, ...]
    tile: int
    gutter: int
    out: str
    thumb: str
    thumb_size: int


@dataclass(frozen=True)
class Caption:
    """A meme's classic caption over an existing JPEG (caption.py): `top` and `bottom`, either
    may be empty but not both, drawn by Pillow and written to `out_path` unless the captioned
    image scores nsfw_max or more. The text is drawn, never part of a diffusion prompt."""
    id: str
    base_path: str
    top: str
    bottom: str
    out_path: str
    nsfw_max: float


@dataclass(frozen=True)
class Critic:
    """The photo critic's measurements of the JPEG at `path` (critic.py); with `ref` and `crop`
    (a square in fractions of `ref`, as an Identity's) the identity cosine to her reference
    too. Nothing is written."""
    id: str
    path: str
    ref: str | None = None
    crop: tuple[float, float, float, float] | None = None


@dataclass(frozen=True)
class Quit:
    id: str


@dataclass(frozen=True)
class Embed:
    id: str
    ref: str


@dataclass(frozen=True)
class Retry:
    """After a ready refusal that says `retry: true` (vram, the pipeline still on the CPU): try
    the GPU again. Any other time it is a bad request."""
    id: str


def _encodable(v) -> None:
    """Every string in the request must be UTF-8 encodable (JSON allows a lone surrogate)."""
    if isinstance(v, str):
        try:
            v.encode("utf-8")
        except UnicodeEncodeError as e:
            raise ValueError("a string is not valid Unicode") from e
    elif isinstance(v, list):
        for x in v:
            _encodable(x)
    elif isinstance(v, dict):
        for k, x in v.items():
            _encodable(k)
            _encodable(x)


def _int(d: dict, key: str, lo: int, hi: int) -> int:
    v = d[key]
    if type(v) is not int or not lo <= v <= hi:
        raise ValueError(f"{key} is not an integer in {lo}..{hi}")
    return v


def _num(d: dict, key: str, lo: float, hi: float) -> float:
    v = d[key]
    if type(v) not in (int, float) or not lo <= v <= hi:
        raise ValueError(f"{key} is not a number in {lo}..{hi}")
    return float(v)


def _phrases(d: dict, key: str) -> tuple[tuple[str, int], ...]:
    v = d[key]
    if type(v) is not list or len(v) > MAX_PHRASES:
        raise ValueError(f"{key} is not a list of at most {MAX_PHRASES} phrases")
    out = []
    for item in v:
        if (type(item) is not list or len(item) != 2 or type(item[0]) is not str
                or not item[0].strip() or len(item[0]) > MAX_PHRASE_CHARS
                or type(item[1]) is not int or not 0 <= item[1] <= 9):
            raise ValueError(f"{key} holds an entry that is not [text, priority 0-9]")
        out.append((item[0], item[1]))
    return tuple(out)


def _jpg(d: dict, key: str) -> str:
    v = d[key]
    if type(v) is not str or not v.lower().endswith(".jpg") or not PurePath(v).is_absolute():
        raise ValueError(f"{key} is not an absolute .jpg path")
    return v


def _identity(v) -> Identity:
    if type(v) is not dict or set(v) != IDENTITY_KEYS:
        raise ValueError("identity keys are not exactly ref, crop, scale")
    crop = v["crop"]
    if (type(crop) is not list or len(crop) != 4
            or any(type(x) not in (int, float) or not 0 <= x <= 1 for x in crop)):
        raise ValueError("crop is not four fractions 0-1")
    l, t, r, b = map(float, crop)
    if not l < r or not t < b or abs((r - l) - (b - t)) > SQUARE_TOLERANCE + 1e-9:
        raise ValueError("crop is not a square with l < r and t < b")
    return Identity(_jpg(v, "ref"), (l, t, r, b), _num(v, "scale", 0.3, 0.9))


def _bool(d: dict, key: str) -> bool:
    v = d.get(key, False)
    if type(v) is not bool:
        raise ValueError(f"{key} is not true or false")
    return v


def _detail(d: dict) -> Detail | None:
    """The detailer's request, or None without detail_faces or detail_hands true. Every key
    given is checked, asked for or not."""
    faces, hands = _bool(d, "detail_faces"), _bool(d, "detail_hands")
    lo, hi = DETAIL_DENOISE_RANGE
    face = _num(d, "detail_face_denoise", lo, hi) if "detail_face_denoise" in d else DETAIL_FACE_DENOISE
    hand = _num(d, "detail_hand_denoise", lo, hi) if "detail_hand_denoise" in d else DETAIL_HAND_DENOISE
    size = d.get("detail_size", DETAIL_SIZE)
    if type(size) is not int or size not in DETAIL_SIZES:
        raise ValueError("detail_size is not 768, 896 or 1024")
    face_max = (_num(d, "detail_face_max", *DETAIL_FACE_MAX_RANGE) if "detail_face_max" in d
                else DETAIL_FACE_MAX)
    margin = (_num(d, "detail_identity_margin", *DETAIL_IDENTITY_MARGIN_RANGE)
              if "detail_identity_margin" in d else DETAIL_IDENTITY_MARGIN)
    return Detail(faces, hands, face, hand, size, face_max, margin) if faces or hands else None


def _grid(d: dict, rid: str) -> Grid:
    if set(d) != GRID_KEYS:
        raise ValueError("grid keys are not exactly " + ", ".join(sorted(GRID_KEYS)))
    tiles = d["tiles"]
    if type(tiles) is not list or not 1 <= len(tiles) <= MAX_TILES:
        raise ValueError(f"tiles is not a list of 1-{MAX_TILES} paths")
    paths = tuple(_jpg({"t": t}, "t") for t in tiles)
    return Grid(rid, paths, _int(d, "tile", 256, 1024), _int(d, "gutter", 0, 32),
                _jpg(d, "out"), _jpg(d, "thumb"), _int(d, "thumb_size", 128, 1024))


def _caption_line(d: dict, key: str) -> str:
    v = d[key]
    if (type(v) is not str or len(v) > MAX_CAPTION or v != v.strip()
            or any(not c.isprintable() or c not in CAPTION_CHARS for c in v)):
        raise ValueError(f"{key} is not a caption line of at most {MAX_CAPTION} characters the font draws")
    return v


def screening_limit(value) -> float:
    """One validated worker default; omission never disables image screening."""
    return _num({"nsfw_max": value}, "nsfw_max", 0.01, 0.9)


def _caption(d: dict, rid: str, nsfw_max: float) -> Caption:
    if not CAPTION_KEYS <= set(d) or set(d) - CAPTION_KEYS - {"nsfw_max"}:
        raise ValueError("caption keys are " + ", ".join(sorted(CAPTION_KEYS)) +
                         " and optionally nsfw_max")
    top, bottom = _caption_line(d, "top"), _caption_line(d, "bottom")
    if not top and not bottom:
        raise ValueError("a caption needs a top or a bottom line")
    base, out = _jpg(d, "base_path"), _jpg(d, "out_path")
    if PurePath(base) == PurePath(out):
        raise ValueError("a caption never writes over its base")
    return Caption(rid, base, top, bottom, out, screening_limit(d.get("nsfw_max", nsfw_max)))


def _critic(d: dict, rid: str) -> Critic:
    keys = set(d)
    if keys not in (CRITIC_KEYS, CRITIC_KEYS | {"ref", "crop"}):
        raise ValueError("critic keys are id, op, path and optionally ref with crop")
    if "ref" not in d:
        return Critic(rid, _jpg(d, "path"))
    identity = _identity({"ref": d["ref"], "crop": d["crop"], "scale": 0.6})
    return Critic(rid, _jpg(d, "path"), identity.ref, identity.crop)


def parse(line: bytes, *, nsfw_max: float = DEFAULT_NSFW_MAX
          ) -> Render | Grid | Caption | Critic | Embed | Quit | Retry:
    """A request line as a Render, a Grid, a Caption, a Critic, an Embed, a Quit or a Retry;
    ValueError for anything else (a line too long, not JSON, an unknown or missing key, a wrong
    type or range)."""
    if len(line) > MAX_LINE:
        raise ValueError("line too long")
    try:
        d = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as e:
        raise ValueError("not UTF-8 JSON") from e
    _encodable(d)
    if type(d) is not dict:
        raise ValueError("not an object")
    rid = d.get("id")
    if type(rid) is not str or not 1 <= len(rid) <= 32:
        raise ValueError("id is not 1-32 characters")
    if d.get("op") in ("quit", "retry"):
        if set(d) != {"id", "op"}:
            raise ValueError(f"{d['op']} takes only id and op")
        return Quit(rid) if d["op"] == "quit" else Retry(rid)
    if d.get("op") == "grid":
        return _grid(d, rid)
    if d.get("op") == "caption":
        return _caption(d, rid, nsfw_max)
    if d.get("op") == "critic":
        return _critic(d, rid)
    if d.get("op") == "embed":
        if set(d) != {"id", "op", "ref"}:
            raise ValueError("embed keys are id, op, ref")
        return Embed(rid, _jpg(d, "ref"))
    if d.get("op") != "render":
        raise ValueError("unknown op")
    extra = set(d) - RENDER_KEYS
    if not RENDER_KEYS <= set(d) or extra - {"identity", "shape", "nsfw_max"} - DETAIL_KEYS:
        raise ValueError("render keys are not " + ", ".join(sorted(RENDER_KEYS)) +
                         " and optionally identity, shape, nsfw_max, " + ", ".join(sorted(DETAIL_KEYS)))
    size = _int(d, "size", 768, 1024)
    if size not in (768, 896, 1024):
        raise ValueError("size is not 768, 896 or 1024")
    shape = d.get("shape", "square")
    if shape not in ("square", "portrait"):
        raise ValueError("shape is not square or portrait")
    if shape == "portrait" and size != 1024:
        raise ValueError("portrait needs size 1024")
    identity = _identity(d["identity"]) if "identity" in d else None
    return Render(rid, _phrases(d, "phrases"), _phrases(d, "negative"),
                  _int(d, "seed", 0, 4294967295), _int(d, "steps", 10, 60),
                  _num(d, "cfg", 1.0, 12.0), size, _jpg(d, "out"), _jpg(d, "thumb"),
                  _int(d, "thumb_size", 128, 1024), screening_limit(d.get("nsfw_max", nsfw_max)),
                  identity, shape, _detail(d))
