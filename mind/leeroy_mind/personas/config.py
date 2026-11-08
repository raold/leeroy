"""Optional automatic newcomer generation; malformed settings mean off at attach."""
import tomllib
from dataclasses import dataclass
from pathlib import Path

from leeroy_mind.config import ConfigError


@dataclass(frozen=True)
class PersonasCfg:
    auto_newcomers: bool = False


def load_personas_config(path: Path) -> PersonasCfg:
    try:
        table = tomllib.loads(path.read_bytes().decode("utf-8-sig")).get("personas", {})
    except FileNotFoundError:
        return PersonasCfg()
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        raise ConfigError("personas_config_unreadable") from None
    if (not isinstance(table, dict) or set(table) - {"auto_newcomers"}
            or type(table.get("auto_newcomers", False)) is not bool):
        raise ConfigError("personas_config_invalid")
    return PersonasCfg(auto_newcomers=table.get("auto_newcomers", False))
