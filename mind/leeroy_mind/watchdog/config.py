"""[watchdog] is read at service startup. Missing or invalid means off."""
import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class WatchdogCfg:
    enabled: bool = False


def load(path: Path) -> WatchdogCfg:
    try:
        table = tomllib.loads(path.read_text(encoding="utf-8-sig")).get("watchdog", {})
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        return WatchdogCfg()
    if type(table) is not dict or set(table) - {"enabled"}:
        return WatchdogCfg()
    enabled = table.get("enabled", False)
    return WatchdogCfg(enabled) if type(enabled) is bool else WatchdogCfg()
