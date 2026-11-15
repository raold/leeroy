"""`[drama]` in mind.toml, read once at mind start and pinned until a restart (nightly.wiring
pins it into every night's settings, as `learn_enabled` is, and here for live talk's cards);
an invalid table turns it all off."""
import logging
import tomllib
from dataclasses import dataclass, fields
from pathlib import Path

log = logging.getLogger(__name__)
_warned: set[tuple[Path, str]] = set()
_PINNED = False  # the startup value (pin), for readers without a night context


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


def pin(on: bool) -> None:
    """The mind's startup choice (nightly.wiring.attach); off until set."""
    global _PINNED
    _PINNED = bool(on)


def pinned() -> bool:
    """The switch as the mind started with it: live talk's cards read this."""
    return _PINNED


def member_romance(ctx) -> bool:
    """The night's switch (NightlySettings.member_romance, pinned at mind start)."""
    return bool(getattr(ctx.settings, "member_romance", False))
