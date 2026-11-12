"""The learning table fails closed, including unknown settings."""
from dataclasses import dataclass
from pathlib import Path
import tomllib


@dataclass(frozen=True)
class LearnCfg:
    enabled: bool = False


def load_learn_config(path: Path) -> LearnCfg:
    try:
        table = tomllib.loads(path.read_text(encoding="utf-8-sig")).get("learn", {})
        if (type(table) is not dict or set(table) - {"enabled"}
                or type(table.get("enabled", False)) is not bool):
            return LearnCfg()
        return LearnCfg(table.get("enabled", False))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        return LearnCfg()
