"""`[heat]` in mind.toml, read once at the mind's start (restart to change it). On by default,
and an invalid table means off: every reader then takes the path it took before the dial.

- `enabled` (true): the heat dial is in force.
- `public_cap` (false; owner ruling 2026-11-25: stays off): cap every channel other people read
  at the flirty tier. Off is the behaviour as it was (the owner's 2026-10-30 "whatever channel"
  ruling)."""
import logging
import tomllib
from dataclasses import dataclass, fields
from pathlib import Path

log = logging.getLogger(__name__)
_warned: set[tuple[Path, str]] = set()


@dataclass(frozen=True, slots=True)
class HeatConfig:
    enabled: bool = True
    public_cap: bool = False


OFF = HeatConfig(enabled=False)


def load_heat_config(path: Path) -> HeatConfig:
    """The [heat] table. A missing file or table is the default (on); an unknown key or a value
    that is not a bool is logged once, by code, and turns the whole table off."""
    path = Path(path)
    key = None
    try:
        table = tomllib.loads(path.read_bytes().decode("utf-8-sig")).get("heat", {})
    except FileNotFoundError:
        return HeatConfig()
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        table, key = {}, "file"
    if key is None:
        if not isinstance(table, dict):
            key = "heat"
        elif set(table) - {f.name for f in fields(HeatConfig)}:
            key = "unknown"
        elif any(type(v) is not bool for v in table.values()):
            key = "value"
    if key is not None:
        if (path, key) not in _warned:
            _warned.add((path, key))
            log.warning("heat_config_invalid key=%s", key)
        return OFF
    return HeatConfig(**table)
