"""[watchdog] is read at service startup. Missing means on; invalid, or an unreadable file, means
off."""
import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class WatchdogCfg:
    enabled: bool = True


OFF = WatchdogCfg(enabled=False)


def load(path: Path) -> WatchdogCfg:
    try:
        table = tomllib.loads(path.read_text(encoding="utf-8-sig")).get("watchdog", {})
    except FileNotFoundError:
        return WatchdogCfg()
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        return OFF
    if type(table) is not dict or set(table) - {"enabled"}:
        return OFF
    enabled = table.get("enabled", True)
    return WatchdogCfg(enabled) if type(enabled) is bool else OFF
