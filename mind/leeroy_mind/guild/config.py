"""Optional guild-life settings, read without opening any game connection."""

import dataclasses
import math
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from leeroy_mind.config import ConfigError


@dataclass(frozen=True)
class DkpConfig:
    kill: float = 10.0
    attendance: float = 5.0
    standby: float = 5.0
    decay: float = 0.10
    decay_weekday: int = 0
    decay_hour: int = 4
    claim_window_s: int = 20
    prices: Mapping[str, int] = field(default_factory=lambda: {
        "weapon_2h": 80, "weapon_1h": 50, "trinket": 50, "token": 40, "other": 30})

    def __post_init__(self):
        object.__setattr__(self, "prices", MappingProxyType(dict(self.prices)))


@dataclass(frozen=True)
class GuildConfig:
    raid_call_delay_s: int = 60
    raid_call_jitter_s: int = 15
    pick_window_min: int = 10
    assemble_timeout_min: int = 20
    ready_check_delay_s: int = 30
    dc_start_delay_s: int = 20
    disband_after_logout_min: int = 5
    offline_raids_per_day: int = 1
    offline_window: tuple[str, str] = ("13:00", "18:00")
    offline_min_away_min: int = 60
    offline_rotation: tuple[str, ...] = ("icc25", "icc10", "toc25", "ulduar25")
    offline_max_wipes: int = 6
    offline_max_wipes_per_boss: int = 3
    offline_timeout_min: int = 180
    friends_cap: int = 40
    friends_add: float = 0.45
    friends_remove: float = 0.30
    gear_up_ilvl: int = 264
    gear_up_below_avg_ilvl: int = 245
    theorycrafter_ilvl: int = 277
    level_to: int = 80
    late_summon_s: tuple[int, int] = (60, 180)
    ranks: Mapping[str, int] = field(default_factory=lambda: {
        "officer": 1, "raider": 2, "social": 3})
    worldserver_conf: Path = Path(r"F:\azerothcore\build\bin\RelWithDebInfo\configs\worldserver.conf")
    mysql_exe: Path = Path(r"C:\Program Files\MySQL\MySQL Server 8.4\bin\mysql.exe")
    dkp: DkpConfig = field(default_factory=DkpConfig)

    def __post_init__(self):
        object.__setattr__(self, "ranks", MappingProxyType(dict(self.ranks)))


def _int(value, lo, hi=None):
    return type(value) is int and value >= lo and (hi is None or value <= hi)


def _number(value, lo, hi=None):
    try:
        return (type(value) in (int, float) and math.isfinite(value)
                and value >= lo and (hi is None or value <= hi))
    except OverflowError:
        return False


def _bad(section, key, want):
    # Paths and other local values can contain private data: name the key only.
    raise ConfigError(f"{section}.{key}: expected {want}")


def _rotation_key(value):
    # Task8's catalog/split_key is not installed yet. Keep this bounded contract
    # local until that module becomes available: canonical keys, not chat aliases.
    if type(value) is not str:
        return False
    match = re.fullmatch(r"(icc|toc|ulduar|naxx|os|eoe|ony|rs)(10|25)(h?)", value)
    return match is not None and (not match[3] or match[1] in {"icc", "toc", "rs"})


def _table(data, section, defaults):
    given = data.get(section, {})
    if type(given) is not dict:
        raise ConfigError(f"{section}: expected a table")
    known = {f.name for f in dataclasses.fields(defaults)} - {"dkp"}
    for key in given:
        if key not in known:
            _bad(section, key, "a known setting")
    return {key: given.get(key, getattr(defaults, key)) for key in known}


def _mapping(section, key, value, defaults, lo, hi=None):
    if not isinstance(value, Mapping) or any(k not in defaults for k in value):
        _bad(section, key, "a table of known keys")
    result = {**defaults, **value}
    for name, item in result.items():
        if not _int(item, lo, hi):
            _bad(section, f"{key}.{name}", f"an integer >= {lo}" if hi is None
                 else f"an integer {lo}-{hi}")
    return result


def load_guild_config(path: Path) -> GuildConfig:
    """Read just [guild]/[dkp]; missing file or tables use approved defaults."""
    try:
        with path.open("rb") as source:
            data = tomllib.load(source)
    except FileNotFoundError:
        return GuildConfig()
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"guild: could not read configuration ({type(exc).__name__})") from None

    defaults = GuildConfig()
    guild = _table(data, "guild", defaults)
    dkp = _table(data, "dkp", defaults.dkp)
    for key in ("raid_call_delay_s", "raid_call_jitter_s", "ready_check_delay_s",
                "dc_start_delay_s", "disband_after_logout_min", "offline_raids_per_day",
                "offline_min_away_min"):
        if not _int(guild[key], 0):
            _bad("guild", key, "a nonnegative integer")
    for key in ("pick_window_min", "assemble_timeout_min"):
        if not _int(guild[key], 1):
            _bad("guild", key, "a positive integer")
    for key, lo, hi in (("offline_max_wipes", 1, 20), ("offline_max_wipes_per_boss", 1, 10),
                        ("offline_timeout_min", 10, 360), ("friends_cap", 1, 50),
                        ("gear_up_ilvl", 1, 400), ("gear_up_below_avg_ilvl", 0, 400),
                        ("level_to", 1, 80)):
        if not _int(guild[key], lo, hi):
            _bad("guild", key, f"an integer {lo}-{hi}")
    value = guild["theorycrafter_ilvl"]
    if not (_int(value, 200, 300) or type(value) is int and value == 0):
        _bad("guild", "theorycrafter_ilvl", "0 or an integer 200-300")
    for key in ("friends_add", "friends_remove"):
        if not _number(guild[key], 0, 1):
            _bad("guild", key, "a finite number 0-1")
    if guild["friends_remove"] >= guild["friends_add"]:
        _bad("guild", "friends_remove", "a value below guild.friends_add")

    window = guild["offline_window"]
    if (not isinstance(window, (list, tuple)) or len(window) != 2
            or any(type(t) is not str or not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", t)
                   for t in window)):
        _bad("guild", "offline_window", "two HH:MM times")
    guild["offline_window"] = tuple(window)
    late = guild["late_summon_s"]
    if (not isinstance(late, (list, tuple)) or len(late) != 2
            or not all(_int(t, 0, 600) for t in late) or late[0] > late[1]):
        _bad("guild", "late_summon_s", "two integers with 0 <= lo <= hi <= 600")
    guild["late_summon_s"] = tuple(late)
    rotation = guild["offline_rotation"]
    if (not isinstance(rotation, (list, tuple)) or not rotation
            or not all(_rotation_key(key) for key in rotation)):
        _bad("guild", "offline_rotation", "raid rotation keys, excluding voa")
    guild["offline_rotation"] = tuple(rotation)
    guild["ranks"] = _mapping("guild", "ranks", guild["ranks"], defaults.ranks, 1, 9)
    for key in ("worldserver_conf", "mysql_exe"):
        value = guild[key]
        if not isinstance(value, Path):
            if type(value) is not str or not value.strip() or "\0" in value:
                _bad("guild", key, "a nonempty path")
            guild[key] = Path(value)

    for key in ("kill", "attendance", "standby"):
        if not _number(dkp[key], 0):
            _bad("dkp", key, "a finite nonnegative number")
    if not _number(dkp["decay"], 0, 1) or dkp["decay"] == 1:
        _bad("dkp", "decay", "a finite number in [0, 1)")
    for key, lo, hi in (("decay_weekday", 0, 6), ("decay_hour", 0, 23)):
        if not _int(dkp[key], lo, hi):
            _bad("dkp", key, f"an integer {lo}-{hi}")
    if not _int(dkp["claim_window_s"], 0):
        _bad("dkp", "claim_window_s", "a nonnegative integer")
    dkp["prices"] = _mapping("dkp", "prices", dkp["prices"], defaults.dkp.prices, 0)
    return GuildConfig(**guild, dkp=DkpConfig(**dkp))
