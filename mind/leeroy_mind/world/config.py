from dataclasses import dataclass, fields
from pathlib import Path
import tomllib


@dataclass(frozen=True)
class WorldCfg:
    enabled: bool = False
    realm: bool = False
    selfies: bool = False
    world_per_night: int = 12
    world_selfies_per_night: int = 10


def load(path):
    try:
        table = tomllib.loads(Path(path).read_text(encoding="utf-8-sig")).get("world",{})
        if not isinstance(table,dict) or set(table)-{f.name for f in fields(WorldCfg)}:
            return WorldCfg()
        for key,value in table.items():
            if key in ("enabled","realm","selfies"):
                if type(value) is not bool:
                    return WorldCfg()
            elif type(value) is not int or not 1 <= value <= 50:
                return WorldCfg()
        return WorldCfg(**table)
    except (OSError,ValueError,UnicodeError):
        return WorldCfg()
