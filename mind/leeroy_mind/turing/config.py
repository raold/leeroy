"""[turing] in mind.toml, read once at the mind's start. Missing, invalid or unknown keys mean
off with the defaults; nothing private reaches a diagnostic."""
import tomllib
from dataclasses import dataclass
from pathlib import Path

WINDOWS_MAX = 60
DAYS_MAX = 90


@dataclass(frozen=True)
class TuringCfg:
    enabled: bool = False
    # blind-judge windows a night (the budget may lower it); 8 keeps a night whose every call
    # fails under health's 10 failed units a stage
    windows: int = 8
    days: int = 14     # the stylometric window, in days back from the night's end


def _int(value, low, high):
    return type(value) is int and low <= value <= high


def load(path: Path) -> TuringCfg:
    try:
        table = tomllib.loads(Path(path).read_text(encoding="utf-8-sig")).get("turing", {})
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        return TuringCfg()
    if not isinstance(table, dict) or set(table) - {"enabled", "windows", "days"}:
        return TuringCfg()
    enabled = table.get("enabled", False)
    windows = table.get("windows", TuringCfg.windows)
    days = table.get("days", TuringCfg.days)
    if type(enabled) is not bool or not _int(windows, 0, WINDOWS_MAX) or not _int(days, 1, DAYS_MAX):
        return TuringCfg()
    return TuringCfg(enabled, windows, days)
