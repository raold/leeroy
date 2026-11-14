"""[raidlead] in mind.toml: `enabled` (default false), read once at the mind's start. A table that
is not exactly that (a key it does not know, a value that is no boolean) or a mind.toml that
cannot be read means off."""

from dataclasses import dataclass
import logging
from pathlib import Path
import tomllib

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RaidLeadConfig:
    enabled: bool = False


def load(path) -> RaidLeadConfig:
    try:
        data = tomllib.loads(Path(path).read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return RaidLeadConfig()
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        log.warning("raidlead_config_unreadable")
        return RaidLeadConfig()
    table = data.get("raidlead", {})
    if (not isinstance(table, dict) or set(table) - {"enabled"}
            or type(table.get("enabled", False)) is not bool):
        log.warning("raidlead_config_invalid")
        return RaidLeadConfig()
    return RaidLeadConfig(enabled=table.get("enabled", False))
