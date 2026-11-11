"""The learning table fails closed, including unknown settings."""
from dataclasses import dataclass
from pathlib import Path
import tomllib


@dataclass(frozen=True)
class LearnCfg:
    enabled: bool = False
    bandit: bool = False
    freeze: bool = False
    frozen_families: tuple[str, ...] = ()
    frozen_members: tuple[int, ...] = ()


def load_learn_config(path: Path) -> LearnCfg:
    try:
        table = tomllib.loads(path.read_text(encoding="utf-8-sig")).get("learn", {})
        from .arms import FAMILIES
        if (type(table) is not dict or set(table) - {"enabled", "bandit", "freeze", "frozen_families", "frozen_members"}
                or any(type(table.get(k, False)) is not bool for k in ("enabled", "bandit", "freeze"))
                or type(table.get("frozen_families", [])) is not list
                or any(type(f) is not str or f not in FAMILIES for f in table.get("frozen_families", []))
                or type(table.get("frozen_members", [])) is not list
                or any(type(g) is not int or g <= 0 for g in table.get("frozen_members", []))):
            return LearnCfg()
        return LearnCfg(table.get("enabled", False), table.get("bandit", False), table.get("freeze", False),
                        tuple(table.get("frozen_families", [])), tuple(table.get("frozen_members", [])))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        return LearnCfg()
