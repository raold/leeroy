"""[signals] enabled: on when missing; invalid and unknown settings, or an unreadable file, mean
disabled."""
from dataclasses import dataclass
from pathlib import Path
import tomllib


@dataclass(frozen=True)
class SignalsCfg:
    enabled: bool = True


OFF = SignalsCfg(enabled=False)


def load_signals_config(path: Path) -> SignalsCfg:
    try:
        table = tomllib.loads(path.read_text(encoding='utf-8-sig')).get('signals', {})
        if (type(table) is not dict or set(table) - {'enabled'}
                or type(table.get('enabled', True)) is not bool):
            return OFF
        return SignalsCfg(table.get('enabled', True))
    except FileNotFoundError:
        return SignalsCfg()
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        return OFF
