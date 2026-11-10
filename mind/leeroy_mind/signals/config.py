"""Missing, invalid and unknown settings all mean disabled."""
from dataclasses import dataclass
from pathlib import Path
import tomllib


@dataclass(frozen=True)
class SignalsCfg:
    enabled: bool = False


def load_signals_config(path: Path) -> SignalsCfg:
    try:
        table = tomllib.loads(path.read_text(encoding='utf-8-sig')).get('signals', {})
        if (type(table) is not dict or set(table) - {'enabled'}
                or type(table.get('enabled', False)) is not bool):
            return SignalsCfg()
        return SignalsCfg(table.get('enabled', False))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        return SignalsCfg()
