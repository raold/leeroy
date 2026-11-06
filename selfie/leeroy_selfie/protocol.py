"""The worker's request lines: one strict JSON form per op, parsed here and nowhere else.

The mind speaks this protocol from its own code (it never imports this package); the shapes
and limits are the contract."""
import json
from dataclasses import dataclass
from pathlib import PurePath

MAX_LINE = 65536
MAX_PHRASES = 64
MAX_PHRASE_CHARS = 500
RENDER_KEYS = frozenset({"id", "op", "phrases", "negative", "seed", "steps", "cfg", "size",
                         "out", "thumb", "thumb_size", "nsfw_max"})


class IdentityUnavailable(ValueError):
    """A request that carries the reserved `identity` key (P4's adapter does not exist yet)."""


class RenderFailed(Exception):
    """A render that failed with a protocol error name (oom, ...) as its message."""


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


@dataclass(frozen=True)
class Quit:
    id: str


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


def parse(line: bytes) -> Render | Quit | Retry:
    """A request line as a Render, a Quit or a Retry; ValueError for anything else (a line too
    long, not JSON, an unknown or missing key, a wrong type or range), IdentityUnavailable
    (a ValueError) for the reserved `identity` key."""
    if len(line) > MAX_LINE:
        raise ValueError("line too long")
    try:
        d = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as e:
        raise ValueError("not UTF-8 JSON") from e
    _encodable(d)
    if type(d) is not dict:
        raise ValueError("not an object")
    if "identity" in d:
        raise IdentityUnavailable("identity")
    rid = d.get("id")
    if type(rid) is not str or not 1 <= len(rid) <= 32:
        raise ValueError("id is not 1-32 characters")
    if d.get("op") in ("quit", "retry"):
        if set(d) != {"id", "op"}:
            raise ValueError(f"{d['op']} takes only id and op")
        return Quit(rid) if d["op"] == "quit" else Retry(rid)
    if d.get("op") != "render":
        raise ValueError("unknown op")
    if set(d) != RENDER_KEYS:
        raise ValueError("render keys are not exactly " + ", ".join(sorted(RENDER_KEYS)))
    size = _int(d, "size", 768, 1024)
    if size not in (768, 896, 1024):
        raise ValueError("size is not 768, 896 or 1024")
    return Render(rid, _phrases(d, "phrases"), _phrases(d, "negative"),
                  _int(d, "seed", 0, 4294967295), _int(d, "steps", 10, 60),
                  _num(d, "cfg", 1.0, 12.0), size, _jpg(d, "out"), _jpg(d, "thumb"),
                  _int(d, "thumb_size", 128, 1024), _num(d, "nsfw_max", 0.01, 0.9))
