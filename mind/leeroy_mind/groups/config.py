import logging
import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class GroupsConfig:
    enabled: bool = False
    max_open: int = 6
    member_starts_per_week: int = 1


def load_groups_config(path: Path) -> GroupsConfig:
    try:
        table = tomllib.loads(Path(path).read_text(encoding='utf-8-sig')).get('groups', {})
    except FileNotFoundError:
        return GroupsConfig()
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        table = None
    if (type(table) is not dict or set(table) - {'enabled','max_open','member_starts_per_week'}
            or type(table.get('enabled',False)) is not bool
            or type(table.get('max_open',6)) is not int or not 1 <= table.get('max_open',6) <= 12
            or type(table.get('member_starts_per_week',1)) is not int
            or not 0 <= table.get('member_starts_per_week',1) <= 3):
        logging.getLogger(__name__).warning('groups_config_invalid')
        return GroupsConfig()
    return GroupsConfig(**table)
