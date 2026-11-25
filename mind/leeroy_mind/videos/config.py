import logging
import tomllib
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)
_warned: set[tuple[Path, str]] = set()


@dataclass(frozen=True, slots=True)
class VideosConfig:
    enabled: bool = False  # his video uploads; needs [photos] enabled and ffmpeg/ffprobe on PATH


def load_videos_config(path: Path) -> VideosConfig:
    """[videos] in mind.toml. A missing file or table is off; a malformed one is off and warned
    once (by key name, never a value)."""
    path = Path(path)
    try:
        table = tomllib.loads(path.read_bytes().decode('utf-8-sig')).get('videos', {})
    except FileNotFoundError:
        return VideosConfig()
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        table, key = None, 'file'
    else:
        key = ('videos' if not isinstance(table, dict) else 'unknown' if set(table) - {'enabled'}
               else next((k for k, v in table.items() if type(v) is not bool), None))
    if key is not None:
        if (path, key) not in _warned:
            _warned.add((path, key))
            log.warning('videos_config_invalid key=%s', key)
        return VideosConfig()
    return VideosConfig(**table)
