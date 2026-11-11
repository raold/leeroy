"""The learning table fails closed, including unknown settings."""
from dataclasses import dataclass
from pathlib import Path
import tomllib


@dataclass(frozen=True)
class LearnCfg:
    enabled: bool = False
    capture_preferences: bool = False


def load_learn_config(path: Path) -> LearnCfg:
    try:
        table = tomllib.loads(path.read_text(encoding="utf-8-sig")).get("learn", {})
        keys = {"enabled", "capture_preferences"}
        if (type(table) is not dict or set(table) - keys
                or any(type(table.get(key, False)) is not bool for key in keys)):
            return LearnCfg()
        return LearnCfg(**table)
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        return LearnCfg()
