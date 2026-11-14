"""[godmode] in mind.toml: the owner's edits of members' minds and his intents (D6). Read once at
the mind's start; missing, invalid or false means off."""
import logging
import tomllib
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)
_warned: set[tuple[Path, str]] = set()


@dataclass(frozen=True, slots=True)
class GodmodeConfig:
    enabled: bool = False


def load_godmode_config(path: Path) -> GodmodeConfig:
    """An invalid table disables godmode as a whole (logged once per file and key)."""
    path = Path(path)
    key = None
    try:
        table = tomllib.loads(path.read_bytes().decode("utf-8-sig")).get("godmode", {})
    except FileNotFoundError:
        return GodmodeConfig()
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        table, key = {}, "file"
    if key is None:
        if not isinstance(table, dict):
            key = "godmode"
        elif set(table) - {"enabled"}:
            key = "unknown"
        elif "enabled" in table and type(table["enabled"]) is not bool:
            key = "enabled"
    if key is not None:
        if (path, key) not in _warned:
            _warned.add((path, key))
            log.warning("godmode_config_invalid key=%s", key)
        return GodmodeConfig()
    return GodmodeConfig(**table)


def app_enabled(app) -> bool:
    cfg = getattr(app, "godmode_config", None)
    return cfg is not None and cfg.enabled
