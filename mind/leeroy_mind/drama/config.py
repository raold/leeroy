"""`[drama]` in mind.toml, read once at startup; an invalid table turns it all off."""
import logging
import tomllib
from dataclasses import dataclass, fields
from pathlib import Path

log = logging.getLogger(__name__)
_warned: set[tuple[Path, str]] = set()


@dataclass(frozen=True, slots=True)
class DramaConfig:
    member_romance: bool = False


def load_drama_config(path: Path) -> DramaConfig:
    """The [drama] table. A missing file or table is the default (off); an unknown key or a
    value that is not a bool is logged once by code and turns the whole table off."""
    path = Path(path)
    key = None
    try:
        table = tomllib.loads(path.read_bytes().decode("utf-8-sig")).get("drama", {})
    except FileNotFoundError:
        return DramaConfig()
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        table, key = {}, "file"
    if key is None:
        if not isinstance(table, dict):
            key = "drama"
        elif set(table) - {f.name for f in fields(DramaConfig)}:
            key = "unknown"
        elif any(type(v) is not bool for v in table.values()):
            key = "value"
    if key is not None:
        if (path, key) not in _warned:
            _warned.add((path, key))
            log.warning("drama_config_invalid key=%s", key)
        return DramaConfig()
    return DramaConfig(**table)


def member_romance(ctx) -> bool:
    """The night's switch (NightlySettings.member_romance, read at mind start)."""
    return bool(getattr(ctx.settings, "member_romance", False))
