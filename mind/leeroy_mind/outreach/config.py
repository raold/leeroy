import logging
import tomllib
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)
_warned: set[tuple[Path, str]] = set()


@dataclass(frozen=True, slots=True)
class OutreachConfig:
    enabled: bool = False


def load_outreach_config(path: Path) -> OutreachConfig:
    """Read once on startup; an invalid table disables outreach as a whole."""
    path = Path(path)
    key = None
    try:
        table = tomllib.loads(path.read_bytes().decode('utf-8-sig')).get('outreach', {})
    except FileNotFoundError:
        return OutreachConfig()
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        table, key = {}, 'file'
    if not isinstance(table, dict):
        key = 'outreach'
    elif set(table) - {'enabled'}:
        key = 'unknown'
    elif 'enabled' in table and type(table['enabled']) is not bool:
        key = 'enabled'
    if key is not None:
        if (path, key) not in _warned:
            _warned.add((path, key))
            log.warning('outreach_config_invalid key=%s', key)
        return OutreachConfig()
    return OutreachConfig(**table)
