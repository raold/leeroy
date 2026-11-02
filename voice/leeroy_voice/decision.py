"""decision.toml: what the listening test and the owner settled (Task 18), read at start.

A missing file is the default: Kokoro for everyone, every flag off, nothing traded. The file
holds top-level keys only; `featured` is a table of guid strings to an expressive engine
("101" = "chatterbox"), for a few members voiced by that engine."""
import logging
import math
import tomllib
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

log = logging.getLogger("leeroy_voice")

ENGINES = ("kokoro", "chatterbox", "chatterbox-turbo", "indextts2")
CLONING = ("chatterbox", "chatterbox-turbo", "indextts2")  # the engines that speak from a reference
# where each member's reference voice comes from
SOURCES = ("kokoro", "qwen3-voicedesign", "voxcpm2")
UK_SHARES = ("keep", "lower")
NO_TRADE = "none"


class DecisionError(ValueError):
    pass


@dataclass(frozen=True)
class Decision:
    decided: str = ""  # the day it was decided, YYYY-MM-DD; "" while the owner has not answered
    engine: str = "kokoro"
    featured: dict[int, str] = field(default_factory=dict)  # guid -> its expressive engine
    prosody_swap: bool = False  # attitude moves a woman's prosody toward a donor voice
    uk_timbre_mix: bool = False  # British women may take a US timbre with a British prosody
    voice_source: str = "kokoro"
    uk_share: str = "keep"
    uk_lower_to: float = 0.0  # with uk_share "lower": the British share of the women, 0-1
    vram_trade: str = NO_TRADE  # what live play gave up to free VRAM for an expressive engine


def _fail(key: str, want: str, v) -> DecisionError:
    return DecisionError(f"{key}: expected {want}, got {v!r}")


def _iso_day(v) -> bool:
    try:
        date.fromisoformat(v)
    except (TypeError, ValueError):
        return False
    return len(v) == 10


def _featured(raw) -> dict[int, str]:
    if not isinstance(raw, dict):
        raise _fail("featured", "a table of guid strings to an engine", raw)
    out = {}
    for k, engine in raw.items():
        if not (k.isascii() and k.isdecimal() and int(k) > 0):
            raise _fail(f"featured.{k}", "a member's guid as the key", k)
        if engine not in CLONING:
            raise _fail(f"featured.{k}", "one of " + ", ".join(CLONING), engine)
        out[int(k)] = engine
    return out


def _build(data: dict, source: str) -> Decision:
    known = {f for f in Decision.__dataclass_fields__}
    for key in data:
        if key not in known:
            log.warning("%s: unknown key %s", source, key)
    d = Decision()
    get = lambda key: data.get(key, getattr(d, key))  # noqa: E731
    decided, engine = get("decided"), get("engine")
    if not (decided == "" or (isinstance(decided, str) and _iso_day(decided))):
        raise _fail("decided", 'a day as "YYYY-MM-DD", or ""', decided)
    if engine not in ENGINES:
        raise _fail("engine", "one of " + ", ".join(ENGINES), engine)
    featured = _featured(get("featured"))
    flags = {}
    for key in ("prosody_swap", "uk_timbre_mix"):
        if type(get(key)) is not bool:
            raise _fail(key, "true or false", get(key))
        flags[key] = get(key)
    source_, share, lower_to, trade = (get("voice_source"), get("uk_share"), get("uk_lower_to"),
                                       get("vram_trade"))
    if source_ not in SOURCES:
        raise _fail("voice_source", "one of " + ", ".join(SOURCES), source_)
    if share not in UK_SHARES:
        raise _fail("uk_share", "one of " + ", ".join(UK_SHARES), share)
    if type(lower_to) not in (int, float) or not math.isfinite(lower_to) or not 0 <= lower_to <= 1:
        raise _fail("uk_lower_to", "a number 0-1", lower_to)
    if share == "lower" and not 0 < lower_to < 1:
        raise _fail("uk_lower_to", 'a share above 0 and below 1 with uk_share "lower"', lower_to)
    if not (isinstance(trade, str) and trade.strip()):
        raise _fail("vram_trade", f'what was traded for VRAM, or "{NO_TRADE}"', trade)
    if (engine != "kokoro" or featured) and trade == NO_TRADE:
        what = engine if engine != "kokoro" else "featured voices"
        raise DecisionError(f"vram_trade: an expressive engine ({what}) needs the VRAM trade "
                            "the owner granted")
    if source_ != "kokoro":  # I11: a designed voice is spoken by a cloning engine on the GPU
        if engine not in CLONING:
            raise _fail("engine", f"a cloning engine ({', '.join(CLONING)}) with voice_source "
                                  f"{source_!r}", engine)
        if trade == NO_TRADE:
            raise DecisionError(f"vram_trade: voice_source {source_!r} needs a VRAM trade")
    return Decision(decided=decided, engine=engine, featured=featured, voice_source=source_,
                    uk_share=share, uk_lower_to=float(lower_to), vram_trade=trade, **flags)


def load_decision(path: Path) -> Decision:
    """A missing file means Decision(); a file that cannot be read or parsed, or that breaks a
    rule, raises DecisionError naming the key."""
    path = Path(path)
    if not path.exists():
        return Decision()
    try:
        text = path.read_bytes().decode("utf-8-sig")
    except OSError as e:
        raise DecisionError(f"{path}: cannot read it: {e.strerror or e}") from e
    except UnicodeDecodeError as e:
        raise DecisionError(f"{path}: not UTF-8 (byte {e.start}); save it as UTF-8") from e
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise DecisionError(f"{path}: {e}") from e
    return _build(data, str(path))
