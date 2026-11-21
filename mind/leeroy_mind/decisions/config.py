"""[decisions] in mind.toml, read once at the start. Off by default; an invalid table is off, and
decisions also need [server] enabled (the D2 engine argues them out)."""
from dataclasses import dataclass, fields, replace
import logging
from pathlib import Path
import tomllib

from leeroy_mind.server.config import load_server_config

log = logging.getLogger(__name__)
_warned: set[tuple[Path, str]] = set()
RANGES = {'bank_items_per_day': (0, 50), 'bank_restock_per_week': (0, 60), 'loot_poll_days': (3, 60)}
# Part C's kinds each have a switch of their own, off by default; Part B's lineups and loot rules
# run whenever decisions are enabled. A kind switched off proposes, polls, posts and seeds nothing.
KIND_SWITCHES = {'rank': 'rank_reviews', 'strategy': 'boss_strategy', 'bank': 'bank_requests'}
BOOLS = frozenset({'enabled', *KIND_SWITCHES.values()})


@dataclass(frozen=True, slots=True)
class DecisionsConfig:
    enabled: bool = False
    bank_items_per_day: int = 10
    loot_poll_days: int = 14
    bank_restock_per_week: int = 20
    rank_reviews: bool = False
    boss_strategy: bool = False
    bank_requests: bool = False

    def kind_on(self, kind: str) -> bool:
        switch = KIND_SWITCHES.get(kind)
        return self.enabled and (switch is None or getattr(self, switch))


def _warn(path: Path, code: str, key: str = '') -> None:
    if (path, code + key) not in _warned:
        _warned.add((path, code + key))
        if key:
            log.warning('%s key=%s', code, key)
        else:
            log.warning('%s', code)


def load_decisions_config(path: Path) -> DecisionsConfig:
    path = Path(path)
    try:
        table = tomllib.loads(path.read_bytes().decode('utf-8-sig')).get('decisions', {})
    except FileNotFoundError:
        return DecisionsConfig()
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        _warn(path, 'decisions_config_invalid', 'file')
        return DecisionsConfig()
    if not isinstance(table, dict):
        _warn(path, 'decisions_config_invalid', 'decisions')
        return DecisionsConfig()
    if set(table) - {f.name for f in fields(DecisionsConfig)}:
        _warn(path, 'decisions_config_invalid', 'unknown')
        return DecisionsConfig()
    for key, value in table.items():
        if key in BOOLS:
            valid = type(value) is bool
        else:
            lo, hi = RANGES[key]
            valid = type(value) is int and lo <= value <= hi
        if not valid:
            _warn(path, 'decisions_config_invalid', key)
            return DecisionsConfig()
    cfg = DecisionsConfig(**table)
    if cfg.enabled and not load_server_config(path).enabled:
        _warn(path, 'decisions_need_server')
        return replace(cfg, enabled=False)
    return cfg
