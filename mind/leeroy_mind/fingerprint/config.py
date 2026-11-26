"""[fingerprint] enabled: on when missing (no file, no table, no key); an unreadable file or an
invalid table is off; no private TOML in diagnostics."""
import tomllib
from pathlib import Path


def enabled(path: Path) -> bool:
    try:
        obj = tomllib.loads(path.read_text(encoding='utf-8-sig')).get('fingerprint', {})
    except FileNotFoundError:
        return True
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        return False
    return (isinstance(obj, dict) and not set(obj)-{'enabled'}
            and type(obj.get('enabled', True)) is bool and obj.get('enabled', True))
