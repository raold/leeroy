"""Optional training settings. Reading them does not arm or attach anything."""

import dataclasses
import math
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from leeroy_mind.config import ConfigError
from leeroy_mind.guild.raids import split_key


@dataclass(frozen=True, slots=True)
class TrainingConfig:
    enabled: bool = False
    telemetry: bool = True
    observe_days: int = 28
    act: bool = True
    experiments: bool = True
    lab: bool = True
    lab_window: tuple[str, str] = ("02:00", "12:00")
    lab_bots: int = 10
    spread: bool = True
    benchmark: bool = True
    benchmark_weekday: int = 2
    benchmark_window: tuple[str, str] = ("08:00", "12:00")
    benchmark_raids: tuple[str, ...] = ("icc10", "toc10")
    benchmark_min_ilvl: int = 232
    lead_voice: bool = True
    gate_pulls: int = 12
    gate_max_ms: float = 1.0
    playbooks: bool = False
    core_dir: Path = Path(r"F:\azerothcore\azerothcore-wotlk")


def _bad(key, expected):
    # Never echo a local value (paths can contain personal information).
    raise ConfigError(f"training.{key}: expected {expected}")


def load_training_config(path: Path) -> TrainingConfig:
    try:
        with path.open("rb") as source:
            data = tomllib.load(source)
    except FileNotFoundError:
        return TrainingConfig()
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"training: could not read configuration ({type(exc).__name__})") from None
    section = data.get("training", {})
    if type(section) is not dict:
        raise ConfigError("training: expected a table")
    defaults = TrainingConfig()
    known = {f.name for f in dataclasses.fields(defaults)}
    for key in section:
        if key not in known:
            _bad(key, "a known setting")
    values = {key: section.get(key, getattr(defaults, key)) for key in known}
    for key in ("enabled", "telemetry", "act", "experiments", "lab", "spread",
                "benchmark", "lead_voice", "playbooks"):
        if type(values[key]) is not bool:
            _bad(key, "a boolean")
    for key, lo, hi in (("observe_days", 0, 365), ("lab_bots", 1, 10),
                        ("benchmark_weekday", 0, 6), ("benchmark_min_ilvl", 0, 300),
                        ("gate_pulls", 4, 100)):
        if type(values[key]) is not int or not lo <= values[key] <= hi:
            _bad(key, f"an integer {lo}-{hi}")
    value = values["gate_max_ms"]
    try:
        valid = type(value) in (int, float) and math.isfinite(value) and 0.1 <= value <= 10
    except OverflowError:
        valid = False
    if not valid:
        _bad("gate_max_ms", "a finite number 0.1-10")
    values["gate_max_ms"] = float(value)
    for key in ("lab_window", "benchmark_window"):
        window = values[key]
        if (not isinstance(window, (list, tuple)) or len(window) != 2
                or any(type(t) is not str or not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", t)
                       for t in window)):
            _bad(key, "two HH:MM times")
        values[key] = tuple(window)
    raids = values["benchmark_raids"]
    if not isinstance(raids, (list, tuple)) or not raids:
        _bad("benchmark_raids", "raid keys excluding voa")
    normalized = []
    for key in raids:
        try:
            raid, size, heroic = split_key(key)
        except (TypeError, ValueError):
            _bad("benchmark_raids", "raid keys excluding voa")
        if raid.key == "voa":
            _bad("benchmark_raids", "raid keys excluding voa")
        normalized.append(f"{raid.key}{size}{'h' if heroic else ''}")
    values["benchmark_raids"] = tuple(normalized)
    value = values["core_dir"]
    if not isinstance(value, Path):
        if type(value) is not str or not value.strip() or "\0" in value:
            _bad("core_dir", "a nonempty path")
        values["core_dir"] = Path(value)
    return TrainingConfig(**values)
