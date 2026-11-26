"""The learning table: its switches are on when missing (but `freeze`, a state); an invalid table,
an unknown setting or an unreadable file fails closed (OFF)."""
from dataclasses import dataclass
from pathlib import Path
import tomllib


@dataclass(frozen=True)
class LearnCfg:
    enabled: bool = True
    coach: bool = True
    auto_lessons: bool = True
    bandit: bool = True
    freeze: bool = False
    frozen_families: tuple[str, ...] = ()
    frozen_members: tuple[int, ...] = ()
    capture_preferences: bool = True
    apply: bool = True
    cycle: bool = True


FLAGS = ("enabled", "coach", "auto_lessons", "bandit", "freeze", "capture_preferences", "apply",
         "cycle")
OFF = LearnCfg(**dict.fromkeys(FLAGS, False))
_DEFAULT = LearnCfg()


def load_learn_config(path: Path) -> LearnCfg:
    try:
        table = tomllib.loads(path.read_text(encoding="utf-8-sig")).get("learn", {})
        from .arms import all_families
        FAMILIES = all_families()
        if (type(table) is not dict or set(table) - {*FLAGS, "frozen_families", "frozen_members"}
                or any(type(table.get(k, False)) is not bool for k in FLAGS)
                or type(table.get("frozen_families", [])) is not list
                or any(type(f) is not str or f not in FAMILIES for f in table.get("frozen_families", []))
                or type(table.get("frozen_members", [])) is not list
                or any(type(g) is not int or g <= 0 for g in table.get("frozen_members", []))):
            return OFF
        return LearnCfg(**{k: table.get(k, getattr(_DEFAULT, k)) for k in FLAGS},
                        frozen_families=tuple(table.get("frozen_families", [])),
                        frozen_members=tuple(table.get("frozen_members", [])))
    except FileNotFoundError:
        return LearnCfg()
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        return OFF
