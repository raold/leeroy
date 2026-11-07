"""Only [claims] enabled=true admits the extension. Malformed tables fail closed."""
import tomllib
from dataclasses import dataclass
from pathlib import Path

from leeroy_mind.config import ConfigError


@dataclass(frozen=True)
class ClaimsCfg:
    enabled: bool = False


def load_claims_config(path: Path) -> ClaimsCfg:
    if not path.exists():
        return ClaimsCfg()
    try:
        table = tomllib.loads(path.read_bytes().decode('utf-8-sig')).get('claims', {})
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        raise ConfigError('claims_config_unreadable') from None
    if (not isinstance(table, dict) or set(table) - {'enabled'}
            or type(table.get('enabled', False)) is not bool):
        raise ConfigError('claims_config_invalid')
    return ClaimsCfg(enabled=table.get('enabled', False))
