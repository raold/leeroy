"""Voice specs and Kokoro style blends (draft section 7).

A member's voice is a recipe, mind-data/voices/<guid>.json (VoiceSpec), never a stored vector:
StyleBank rebuilds the style vector from the sha-pinned voices file (voices-v1.0.bin).

Kokoro's style for a voice is a pack of 510 rows, one per phoneme count, of 256 values: the
first 128 drive the decoder (the timbre), the last 128 the prosody predictor (duration, F0 and
energy). A spec's `timbre` is a weighted blend of English base voices (GROUPS); a non-empty
`prosody` is a second blend whose prosody half replaces the timbre's.

What a spec keeps, checked whenever one is made (SpecError otherwise):
- every base is an English voice in GROUPS, each blend's weights are above 0 and sum to 1, and
  every base is of the member's one gender (a voice follows the player, draft 7.1);
- a US voice (`lang` "a") blends US bases only; a British one ("b") takes British prosody;
- the British recipe (draft 7.2): a British voice whose timbre holds US bases, with a British
  prosody blend. style() refuses it until the owner decides it (Decision.uk_timbre_mix).

Attitude (draft section 8): with Decision.prosody_swap, a line of attitude fond, drawn or lust
moves a woman's prosody half toward a donor voice of her accent by the donor's weight
(SWAP_DONORS); an all-male timbre ignores the attitude (M13)."""
import hashlib
import io
import math
import re
from dataclasses import asdict, dataclass, fields
from pathlib import Path

import numpy as np

from .decision import ENGINES, Decision
from .wire import ATTITUDES

SPEC_VERSION = 1
LANGS = ("a", "b")  # Kokoro's own letters: a US English, b British English
STYLE_SHAPE = (510, 1, 256)
HALF = 128  # [..., :HALF] the timbre, [..., HALF:] the prosody
SPEED_RANGE = (0.9, 1.15)
PITCH_LIMIT_ST = 2.0
GAIN_LIMIT_DB = 3.0
WEIGHT_SUM_TOLERANCE = 0.01  # rounded weights may miss 1 a little; a blend is scaled to 1
NAME_MAX = 64
REF_MAX = 255

# Kokoro v1.0's English voices by (gender, lang), with VOICES.md's overall grades (af_heart has
# only its target grade); hexgrad/Kokoro-82M @ f3ff3571791e39611d31c381e3a41a3af07b4987
GROUPS: dict[tuple[str, str], dict[str, str]] = {
    ("f", "a"): {"af_heart": "A", "af_bella": "A-", "af_nicole": "B-", "af_aoede": "C+",
                 "af_kore": "C+", "af_sarah": "C+", "af_alloy": "C", "af_nova": "C",
                 "af_sky": "C-", "af_jessica": "D", "af_river": "D"},
    ("f", "b"): {"bf_emma": "B-", "bf_isabella": "C", "bf_alice": "D", "bf_lily": "D"},
    ("m", "a"): {"am_fenrir": "C+", "am_michael": "C+", "am_puck": "C+", "am_echo": "D",
                 "am_eric": "D", "am_liam": "D", "am_onyx": "D", "am_santa": "D-",
                 "am_adam": "F+"},
    ("m", "b"): {"bm_fable": "C", "bm_george": "C", "bm_lewis": "D+", "bm_daniel": "D"},
}
BASES: dict[str, tuple[str, str]] = {v: group for group, grades in GROUPS.items() for v in grades}
# attitude -> (the donor voice by lang, its weight); the listening kit's values (Task 1)
SWAP_DONORS: dict[str, tuple[dict[str, str], float]] = {
    "fond": ({"a": "af_bella", "b": "bf_isabella"}, 0.5),
    "drawn": ({"a": "af_nicole", "b": "bf_lily"}, 0.6),
    "lust": ({"a": "af_nicole", "b": "bf_lily"}, 0.85),
}
_MIC = re.compile(r"[a-z][a-z0-9_]{0,31}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_NOT_IN_A_NAME = frozenset('<>:"/\\|?*')  # Windows' reserved characters: a folder, drive, URL


class SpecError(ValueError):
    pass


@dataclass(frozen=True)
class VoiceSpec:
    """A member's voice; checked when made, by spec_from_dict or replace() alike. Numbers come
    out as floats and the spec owns its blends (copies of the ones it was given)."""
    version: int
    guid: int
    name: str
    engine: str
    lang: str
    timbre: dict[str, float]
    prosody: dict[str, float]  # empty: the timbre's own
    speed: float
    pitch_st: float
    mic: str
    gain_db: float
    locked: bool
    voices_sha256: str
    ref: str  # a cloning engine's reference clip, a bare file name; "" for Kokoro

    def __post_init__(self):
        for key, value in _checked(self).items():
            object.__setattr__(self, key, value)


FIELDS = tuple(f.name for f in fields(VoiceSpec))


def _fail(key: str, want: str, v) -> SpecError:
    return SpecError(f"{key}: expected {want}, got {v!r}")


def _number(key: str, v, lo: float, hi: float) -> float:
    if type(v) not in (int, float) or not math.isfinite(v) or not lo <= v <= hi:
        raise _fail(key, f"a number {lo:g} to {hi:g}", v)
    return float(v)


def _text(key: str, v, limit: int, *, empty_ok: bool) -> str:
    """A name or a file name; an error shows its shape, not its text."""
    if (isinstance(v, str) and len(v) <= limit and v.isprintable() and v == v.strip()
            and (v or empty_ok)):
        return v
    got = f"a {len(v)}-character string" if isinstance(v, str) else f"a {type(v).__name__}"
    raise SpecError(f"{key}: expected printable text of at most {limit} characters, without "
                    f"surrounding spaces{'' if empty_ok else ', not empty'}; got {got}")


def _blend(key: str, v, *, empty_ok: bool) -> dict[str, float]:
    if not isinstance(v, dict):
        raise _fail(key, "a table of base voices to weights", v)
    if not v and not empty_ok:
        raise SpecError(f"{key}: expected at least one base voice")
    out = {}
    for base, w in v.items():
        if base not in BASES:
            raise SpecError(f"{key}: {base!r} is not an English base voice (GROUPS)")
        if type(w) not in (int, float) or not math.isfinite(w) or w <= 0:
            raise _fail(f"{key}.{base}", "a weight above 0", w)
        out[base] = float(w)
    if out and abs(sum(out.values()) - 1) > WEIGHT_SUM_TOLERANCE:
        raise SpecError(f"{key}: the weights sum to {sum(out.values()):g}, not 1")
    return out


def _checked(s: VoiceSpec) -> dict:
    """The fields a spec stores, normalised; SpecError naming the first one that is wrong."""
    if type(s.version) is not int or s.version != SPEC_VERSION:
        raise _fail("version", str(SPEC_VERSION), s.version)
    if type(s.guid) is not int or s.guid <= 0:
        raise _fail("guid", "a member's guid", s.guid)
    name = _text("name", s.name, NAME_MAX, empty_ok=False)
    if s.engine not in ENGINES:
        raise _fail("engine", "one of " + ", ".join(ENGINES), s.engine)
    if s.lang not in LANGS:
        raise _fail("lang", '"a" (US) or "b" (UK)', s.lang)
    timbre = _blend("timbre", s.timbre, empty_ok=False)
    prosody = _blend("prosody", s.prosody, empty_ok=True)
    if len({BASES[b][0] for b in (*timbre, *prosody)}) > 1:
        raise SpecError("timbre, prosody: every base is of the member's one gender")
    if s.lang == "a":
        british = [b for b in (*timbre, *prosody) if BASES[b][1] != "a"]
        if british:
            raise SpecError(f"timbre, prosody: a US voice blends US bases only, not "
                            f"{', '.join(british)}")
    else:
        us = [b for b in prosody if BASES[b][1] != "b"]
        if us:
            raise SpecError(f"prosody: a British voice takes British prosody, not {', '.join(us)}")
        if not prosody and any(BASES[b][1] != "b" for b in timbre):
            raise SpecError("prosody: a British voice with US bases in its timbre (the British "
                            "recipe) takes a British prosody blend")
    speed = _number("speed", s.speed, *SPEED_RANGE)
    pitch_st = _number("pitch_st", s.pitch_st, -PITCH_LIMIT_ST, PITCH_LIMIT_ST)
    if not (isinstance(s.mic, str) and _MIC.fullmatch(s.mic)):
        raise _fail("mic", "a mic profile's name (lowercase letters, digits and _)", s.mic)
    gain_db = _number("gain_db", s.gain_db, -GAIN_LIMIT_DB, GAIN_LIMIT_DB)
    if type(s.locked) is not bool:
        raise _fail("locked", "true or false", s.locked)
    if not (isinstance(s.voices_sha256, str) and _SHA256.fullmatch(s.voices_sha256)):
        raise _fail("voices_sha256", "the voices file's sha256, 64 lowercase hex digits",
                    s.voices_sha256)
    ref = _text("ref", s.ref, REF_MAX, empty_ok=True)
    if s.engine == "kokoro" and ref:
        raise SpecError('ref: Kokoro takes no reference clip; expected ""')
    if s.engine != "kokoro" and not ref:
        raise SpecError(f"ref: {s.engine} speaks from a reference clip; expected its file name")
    if ref and (set(ref) & _NOT_IN_A_NAME or not ref.strip(".")):
        raise SpecError(f"ref: expected a bare file name (no folder, drive, stream or URL); "
                        f"got a {len(ref)}-character string")
    return {"name": name, "timbre": timbre, "prosody": prosody, "speed": speed,
            "pitch_st": pitch_st, "gain_db": gain_db, "ref": ref}


def spec_from_dict(d) -> VoiceSpec:
    """A spec from its JSON object; every key required, none unknown."""
    if not isinstance(d, dict):
        raise SpecError(f"a voice spec is a JSON object, got a {type(d).__name__}")
    missing = [k for k in FIELDS if k not in d]
    if missing:
        raise SpecError(f"missing key(s): {', '.join(missing)}")
    unknown = [str(k) for k in d if k not in FIELDS]
    if unknown:
        raise SpecError(f"unknown key(s): {', '.join(unknown)}")
    return VoiceSpec(**d)


def spec_to_dict(spec: VoiceSpec) -> dict:
    """The spec's JSON object, keys in field order: what mind-data/voices/<guid>.json holds.
    A spec whose blends were changed in place since it was made raises SpecError."""
    d = asdict(spec)
    spec_from_dict(d)
    return d


class StyleBank:
    """Kokoro's voices file (an npz of one float32 (510, 1, 256) pack per voice, as kokoro-onnx
    loads it), read and hashed once."""

    def __init__(self, voices_bin: Path):
        data = Path(voices_bin).read_bytes()
        self.sha256 = hashlib.sha256(data).hexdigest()
        with np.load(io.BytesIO(data), allow_pickle=False) as npz:
            self._packs = {name: npz[name] for name in npz.files}
        for name, pack in self._packs.items():
            if pack.shape != STYLE_SHAPE or pack.dtype != np.float32:
                raise ValueError(f"{voices_bin}: {name} is {pack.dtype} {pack.shape}, not "
                                 f"float32 {STYLE_SHAPE}")
        self._cache: dict[tuple[int, str], tuple[tuple, np.ndarray]] = {}

    def names(self) -> list[str]:
        return sorted(self._packs)

    def style(self, spec: VoiceSpec, attitude: str, decision: Decision) -> np.ndarray:
        """The spec's style for a line of this attitude: float32 (510, 1, 256), read-only,
        cached per guid and attitude (and rebuilt when the spec changes). SpecError when the
        spec was made on another voices file, needs a voice this file lacks, or is the British
        recipe before the owner decided it."""
        if attitude not in ATTITUDES:
            raise ValueError(f"attitude: expected one of {', '.join(ATTITUDES)}, got {attitude!r}")
        if spec.voices_sha256 != self.sha256:
            raise SpecError(f"voices_sha256: the spec was made on another voices file "
                            f"({spec.voices_sha256[:12]}), not this one ({self.sha256[:12]})")
        if (spec.lang == "b" and any(BASES[b][1] == "a" for b in spec.timbre)
                and not decision.uk_timbre_mix):
            raise SpecError("timbre: US bases in a British voice (the British recipe) wait for "
                            "the owner's decision, uk_timbre_mix")
        donor = self._donor(spec, attitude, decision)
        if donor is None and attitude != "none":  # the attitude changes nothing: the same array
            return self.style(spec, "none", decision)
        key, sig = (spec.guid, attitude), (asdict(spec), donor)
        hit = self._cache.get(key)
        if hit is not None and hit[0] == sig:
            return hit[1]
        out = self._mix("timbre", spec.timbre)
        if spec.prosody:
            out[..., HALF:] = self._mix("prosody", spec.prosody)[..., HALF:]
        if donor is not None:
            voice, w = donor
            pack = self._pack(f"the {attitude} donor", voice)
            out[..., HALF:] = (1 - w) * out[..., HALF:] + w * pack[..., HALF:]
        out.flags.writeable = False
        self._cache[key] = (sig, out)
        return out

    @staticmethod
    def _donor(spec: VoiceSpec, attitude: str, decision: Decision) -> tuple[str, float] | None:
        if attitude == "none" or not decision.prosody_swap:
            return None
        if all(BASES[b][0] == "m" for b in spec.timbre):  # M13
            return None
        voices, weight = SWAP_DONORS[attitude]
        return voices[spec.lang], weight

    def _pack(self, what: str, voice: str) -> np.ndarray:
        pack = self._packs.get(voice)
        if pack is None:
            raise SpecError(f"{what}: {voice} is not in this voices file")
        return pack

    def _mix(self, what: str, weights: dict[str, float]) -> np.ndarray:
        total = sum(weights.values())
        out = np.zeros(STYLE_SHAPE, dtype=np.float32)
        for voice in sorted(weights):
            out += np.float32(weights[voice] / total) * self._pack(what, voice)
        return out
