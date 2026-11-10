"""Invalid or missing optional settings are off; no private TOML in diagnostics."""
import tomllib
from pathlib import Path


def enabled(path: Path) -> bool:
    try:
        obj = tomllib.loads(path.read_text(encoding='utf-8-sig')).get('fingerprint', {})
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        return False
    return (isinstance(obj, dict) and not set(obj)-{'enabled'}
            and type(obj.get('enabled', False)) is bool and obj.get('enabled', False))
