from dataclasses import dataclass, fields
from pathlib import Path
import tomllib


@dataclass(frozen=True)
class WorldCfg:
    enabled: bool = True
    realm: bool = True
    selfies: bool = True
    world_per_night: int = 12
    world_selfies_per_night: int = 10


OFF = WorldCfg(enabled=False, realm=False, selfies=False)  # invalid, or an unreadable file


def load(path):
    try:
        table = tomllib.loads(Path(path).read_text(encoding="utf-8-sig")).get("world",{})
        if not isinstance(table,dict) or set(table)-{f.name for f in fields(WorldCfg)}:
            return OFF
        for key,value in table.items():
            if key in ("enabled","realm","selfies"):
                if type(value) is not bool:
                    return OFF
            elif type(value) is not int or not 1 <= value <= 50:
                return OFF
        return WorldCfg(**table)
    except FileNotFoundError:
        return WorldCfg()
    except (OSError,ValueError,UnicodeError):
        return OFF
