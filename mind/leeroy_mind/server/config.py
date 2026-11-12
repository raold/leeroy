"""The server stays off unless its entire table is valid."""
import logging
import re
import tomllib
from dataclasses import dataclass, fields
from datetime import time
from pathlib import Path

log = logging.getLogger(__name__)
_warned: set[tuple[Path, str]] = set()
_TIME = re.compile(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]')


@dataclass(frozen=True, slots=True)
class ServerConfig:
    enabled: bool = False
    quiet_window: tuple[time, time] = (time(3), time(8))
    conversations_per_hour: float = 2.5
    in_game_share: float = .5
    dms_per_day: int = 3
    reactions: bool = True


def _invalid(path: Path, key: str) -> ServerConfig:
    if (path, key) not in _warned:
        _warned.add((path, key))
        log.warning('server_config_invalid key=%s', key)
    return ServerConfig()


def load_server_config(path: Path) -> ServerConfig:
    path = Path(path)
    try:
        table = tomllib.loads(path.read_bytes().decode('utf-8-sig')).get('server', {})
    except FileNotFoundError:
        return ServerConfig()
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        return _invalid(path, 'file')
    if not isinstance(table, dict):
        return _invalid(path, 'server')
    if set(table) - {f.name for f in fields(ServerConfig)}:
        return _invalid(path, 'unknown')
    values = dict(table)
    for key, value in table.items():
        if key in ('enabled', 'reactions'):
            valid = type(value) is bool
        elif key == 'dms_per_day':
            valid = type(value) is int and 0 <= value <= 12
        elif key in ('conversations_per_hour', 'in_game_share'):
            valid = type(value) in (int, float) and 0 <= value <= (12 if key == 'conversations_per_hour' else 1)
            if valid:
                values[key] = float(value)
        else:
            valid = (isinstance(value, list) and len(value) == 2
                     and all(isinstance(v, str) and _TIME.fullmatch(v) for v in value)
                     and value[0] != value[1])
            if valid:
                values[key] = tuple(time.fromisoformat(v) for v in value)
        if not valid:
            return _invalid(path, key)
    return ServerConfig(**values)
