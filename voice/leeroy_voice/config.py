"""voice.toml, the service's settings, and ui-state.json, the voice UI's volumes and mutes.

Both live in the data dir (LEEROY_VOICE_DATA, else voice-data beside the repo). The owner's
device names live only in his local voice.toml (PR15): the defaults name no device."""
import json
import logging
import math
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("leeroy_voice")

DEFAULT_TOML = '''\
[mind]
host = "127.0.0.1"            # loopback only: the mind connects here
port = 7879

[ui]
port = 7880                   # the voice UI, on 127.0.0.1 only

[devices]
input_device = ""             # the microphone by name: no microphone until you name one here
output_device = ""            # by name; "" plays on the Windows default device and follows it

[ptt]
key = "\\\\"                    # push-to-talk: "\\\\" or "vk:0x.." (a Windows virtual-key code)
max_hold_s = 30.0             # a longer hold is cut off here, 1-120 s
min_press_ms = 150            # a shorter tap is typing, not talking (50-1000 ms)

[sound]
duck_db = 8.0                 # WoW turned down this far while members talk, 0-20 dB; 0 is off
comms_grit = 3                # 0 clean to 10 rough: 2009 band-limited mics and a codec
stereo_spread = 0.0           # 0-0.2: how far the voices are panned apart

[tts]
threads = 6                   # CPU threads for Kokoro
cpu_fallback = false          # optional CPU Turbo when GPU admission refuses it
cpu_threads = 12              # live CPU Turbo only, at most 24
cpu_timeout_s = 15.0          # live CPU limit; each line's expiry can shorten it

[stt]
recognizer = "parakeet"       # "parakeet" on the CPU, or "auto": Whisper on the GPU when it fits
threads = 4                   # CPU threads for Parakeet
min_confidence = 0.4          # 0-1: a transcript below this is dropped

[gpu]
margin_gb = 1.0               # VRAM kept free beside Ollama's models before a GPU model loads

[debug]
record = false                # keep each push-to-talk clip 24 hours in voice-data/debug
'''

HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
RECOGNIZERS = ("parakeet", "auto")
BACKSLASH_VK = 0xDC  # VK_OEM_5, the \ key on a US keyboard
UI_DB_LIMIT = 12.0  # per-member volume in the voice UI, ± this many dB
_VK = re.compile(r"vk:0x([0-9a-f]{1,2})", re.IGNORECASE)


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class VoiceConfig:
    mind_host: str
    mind_port: int
    ui_port: int
    input_device: str
    output_device: str
    ptt_key: str
    ptt_vk: int
    min_press_ms: int
    max_hold_s: float
    duck_db: float
    comms_grit: int
    stereo_spread: float
    tts_threads: int
    recognizer: str
    stt_threads: int
    min_confidence: float
    gpu_margin_gb: float
    debug_record: bool
    tts_cpu_fallback: bool = False
    tts_cpu_threads: int = 12
    tts_cpu_timeout_s: float = 15.0


def vk_for(key: str) -> int:
    """The Windows virtual-key code for a [ptt] key: "\\" is VK_OEM_5 (0xDC); "vk:0x87" any
    other key by its code."""
    if key == "\\":
        return BACKSLASH_VK
    m = _VK.fullmatch(key) if isinstance(key, str) else None
    if m is not None and 0x01 <= int(m.group(1), 16) <= 0xFE:
        return int(m.group(1), 16)
    raise ConfigError(f'ptt.key: expected "\\" or "vk:0x01"-"vk:0xfe", got {key!r}')


def _int_in(lo, hi):
    return lambda v: type(v) is int and lo <= v <= hi


def _num_in(lo, hi):
    return lambda v: type(v) in (int, float) and math.isfinite(v) and lo <= v <= hi


def _is_vk(v) -> bool:
    try:
        vk_for(v)
    except ConfigError:
        return False
    return True


def _device(v) -> bool:
    return isinstance(v, str) and len(v) <= 256 and v.isprintable()


# (section, key) -> (VoiceConfig field, check, what it must be, convert)
_RULES = {
    ("mind", "host"): ("mind_host", lambda v: isinstance(v, str) and v in HOSTS,
                       "one of 127.0.0.1, localhost or ::1 (loopback only)", str),
    ("mind", "port"): ("mind_port", _int_in(1, 65535), "an integer 1-65535", int),
    ("ui", "port"): ("ui_port", _int_in(1, 65535), "an integer 1-65535", int),
    ("devices", "input_device"): ("input_device", _device, "a device name, or \"\"", str.strip),
    ("devices", "output_device"): ("output_device", _device, "a device name, or \"\"", str.strip),
    ("ptt", "key"): ("ptt_key", _is_vk, '"\\" or "vk:0x.." (a virtual-key code)', str),
    ("ptt", "max_hold_s"): ("max_hold_s", _num_in(1, 120), "a number 1-120", float),
    ("ptt", "min_press_ms"): ("min_press_ms", _int_in(50, 1000), "an integer 50-1000", int),
    ("sound", "duck_db"): ("duck_db", _num_in(0, 20), "a number 0-20", float),
    ("sound", "comms_grit"): ("comms_grit", _int_in(0, 10), "an integer 0-10", int),
    ("sound", "stereo_spread"): ("stereo_spread", _num_in(0, 0.2), "a number 0-0.2", float),
    ("tts", "threads"): ("tts_threads", _int_in(1, 64), "an integer 1-64", int),
    ("tts", "cpu_fallback"): ("tts_cpu_fallback", lambda v: type(v) is bool, "true or false", bool),
    ("tts", "cpu_threads"): ("tts_cpu_threads", _int_in(1, 24), "an integer 1-24", int),
    ("tts", "cpu_timeout_s"): ("tts_cpu_timeout_s", _num_in(1, 15), "a number 1-15", float),
    ("stt", "recognizer"): ("recognizer", lambda v: v in RECOGNIZERS,
                            "one of " + ", ".join(RECOGNIZERS), str),
    ("stt", "threads"): ("stt_threads", _int_in(1, 64), "an integer 1-64", int),
    ("stt", "min_confidence"): ("min_confidence", _num_in(0, 1), "a number 0-1", float),
    ("gpu", "margin_gb"): ("gpu_margin_gb", _num_in(0, 24), "a number 0-24", float),
    ("debug", "record"): ("debug_record", lambda v: type(v) is bool, "true or false", bool),
}
_SECTIONS = frozenset(s for s, _ in _RULES)


def _shown(section: str, v) -> str:
    """A bad value as an error shows it; a device name only by its shape (PR15)."""
    if section != "devices":
        return repr(v)
    return f"a {len(v)}-character string" if isinstance(v, str) else f"a {type(v).__name__}"


def _build(data: dict, source: str) -> VoiceConfig:
    defaults = tomllib.loads(DEFAULT_TOML)
    for section, table in data.items():
        if section not in _SECTIONS:
            log.warning("%s: unknown section [%s]", source, section)
            continue
        if not isinstance(table, dict):
            raise ConfigError(f"{section}: expected a table")
        for key in table:
            if (section, key) not in _RULES:
                log.warning("%s: unknown key %s.%s", source, section, key)
    values = {}
    for (section, key), (name, check, want, convert) in _RULES.items():
        v = data.get(section, {}).get(key, defaults[section][key])
        if not check(v):
            raise ConfigError(f"{section}.{key}: expected {want}, got {_shown(section, v)}")
        values[name] = convert(v)
    if values["ui_port"] == values["mind_port"]:
        raise ConfigError(f"ui.port: must differ from mind.port ({values['mind_port']})")
    return VoiceConfig(**values, ptt_vk=vk_for(values["ptt_key"]))


def load_config(path: Path) -> VoiceConfig:
    """A missing file means the defaults; one that cannot be read, decoded as UTF-8 or parsed,
    or holds a bad value, raises ConfigError naming the key."""
    path = Path(path)
    if not path.exists():
        return _build({}, str(path))
    try:
        raw = path.read_bytes()
    except OSError as e:
        raise ConfigError(f"{path}: cannot read it: {e.strerror or e}") from e
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        raise ConfigError(f"{path}: not UTF-8 (byte {e.start}); save it as UTF-8") from e
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: {e}") from e
    return _build(data, str(path))


def default_data_dir() -> Path:
    env = os.environ.get("LEEROY_VOICE_DATA")
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[2] / "voice-data"


@dataclass(frozen=True)
class UiState:
    """The voice UI's per-member volume in dB (±12) and the members he muted, by guid."""
    volumes: dict[int, float] = field(default_factory=dict)
    mutes: frozenset[int] = frozenset()


def _guid(v) -> int | None:
    if type(v) is int and v > 0:
        return v
    if isinstance(v, str) and v.isdecimal() and v.isascii() and int(v) > 0:
        return int(v)
    return None


def _clean(volumes, mutes) -> UiState:
    """Keep the entries that make sense, volumes clamped to ±12 dB; drop the rest."""
    vols = {}
    for k, db in (volumes.items() if isinstance(volumes, dict) else ()):
        g = _guid(k)
        if g is not None and type(db) in (int, float) and math.isfinite(db):
            vols[g] = max(-UI_DB_LIMIT, min(UI_DB_LIMIT, float(db)))
    muted = frozenset()
    if isinstance(mutes, (list, tuple, set, frozenset)):
        muted = frozenset(m for m in mutes if type(m) is int and m > 0)
    return UiState(vols, muted)


def load_ui_state(path: Path) -> UiState:
    """ui-state.json; a missing or damaged file is an empty state (a damaged one is logged)."""
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return UiState()
    except (OSError, ValueError, RecursionError) as e:
        log.warning("ui_state_unreadable: %s", e.__class__.__name__)
        return UiState()
    if not isinstance(data, dict):
        log.warning("ui_state_unreadable: not an object")
        return UiState()
    return _clean(data.get("volumes", {}), data.get("mutes", []))


def save_ui_state(path: Path, state: UiState) -> None:
    """Write the state whole, through a temporary file, so a crash never leaves half a file."""
    path = Path(path)
    state = _clean(state.volumes, list(state.mutes))
    data = {"volumes": {str(g): db for g, db in sorted(state.volumes.items())},
            "mutes": sorted(state.mutes)}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=1) + "\n", encoding="utf-8")
    os.replace(tmp, path)
