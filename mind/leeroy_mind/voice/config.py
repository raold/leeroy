"""The [voice] table of mind.toml. config.py leaves it alone (EXTENSION_SECTIONS); it is read
here, and only here, so a mind without the section behaves exactly as before."""
import tomllib
from dataclasses import dataclass, fields
from pathlib import Path

from leeroy_mind.config import _HOSTS, ConfigError, log


@dataclass(frozen=True)
class VoiceCfg:
    enabled: bool = False
    host: str = "127.0.0.1"  # loopback only
    port: int = 7879
    autostart: bool = False  # start leeroy-voice when nothing listens on the port
    on_comms_raider: float = 0.9  # chance a raider is on comms tonight (Task 13)
    on_comms_social: float = 0.6
    hot_mic_per_month: float = 1.0
    elsewhere_lines_per_10min: int = 6


def _bool(v) -> bool:
    return type(v) is bool


def _num(lo: float, hi: float):
    return lambda v: type(v) in (int, float) and lo <= v <= hi


def _int(lo: int, hi: int):
    return lambda v: type(v) is int and lo <= v <= hi


_RULES = {
    "enabled": (_bool, "true or false"),
    "host": (lambda v: v in _HOSTS, f"one of {sorted(_HOSTS)}"),
    "port": (_int(1, 65535), "an integer 1-65535"),
    "autostart": (_bool, "true or false"),
    "on_comms_raider": (_num(0, 1), "a number 0-1"),
    "on_comms_social": (_num(0, 1), "a number 0-1"),
    "hot_mic_per_month": (_num(0, 1000), "a number 0-1000"),
    "elsewhere_lines_per_10min": (_int(0, 1000), "an integer 0-1000"),
}
_FLOATS = {"on_comms_raider", "on_comms_social", "hot_mic_per_month"}


def load_voice_config(path: Path) -> VoiceCfg:
    """The [voice] table of the mind.toml at `path`; a missing file or table means the
    defaults. Raises ConfigError for a file it cannot read or a bad value."""
    path = Path(path)
    if not path.exists():
        return VoiceCfg()
    try:
        data = tomllib.loads(path.read_bytes().decode("utf-8-sig"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        raise ConfigError(f"{path}: cannot read [voice]: {e.__class__.__name__}") from e
    table = data.get("voice")
    if table is None:
        return VoiceCfg()
    if not isinstance(table, dict):
        raise ConfigError("voice: expected a table")
    values = {}
    for key, v in table.items():
        rule = _RULES.get(key)
        if rule is None:
            log.warning("%s: unknown key voice.%s", path, key)
            continue
        if not rule[0](v):
            raise ConfigError(f"voice.{key}: expected {rule[1]}, got {v!r}")
        values[key] = float(v) if key in _FLOATS else v
    assert set(values) <= {f.name for f in fields(VoiceCfg)}
    return VoiceCfg(**values)
