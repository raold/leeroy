import logging
import tomllib
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)
_warned: set[tuple[Path, str]] = set()


@dataclass(frozen=True, slots=True)
class PhotosConfig:
    enabled: bool = False
    send: bool = False


def load_photos_config(path: Path) -> PhotosConfig:
    path = Path(path)
    key = None
    try:
        table = tomllib.loads(path.read_bytes().decode('utf-8-sig')).get('photos', {})
    except FileNotFoundError:
        return PhotosConfig()
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        table, key = {}, 'file'
    if not isinstance(table, dict):
        key = 'photos'
    elif set(table) - {'enabled', 'send'}:
        key = 'unknown'
    else:
        key = key or next((k for k, v in table.items() if type(v) is not bool), None)
    if key is not None:
        if (path, key) not in _warned:
            _warned.add((path, key))
            log.warning('photos_config_invalid key=%s', key)
        return PhotosConfig()
    return PhotosConfig(**table)
