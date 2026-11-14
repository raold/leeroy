"""Culture is enabled only by two complete valid tables."""
from dataclasses import dataclass, fields
from pathlib import Path
import tomllib
from leeroy_mind.server.config import load_server_config

@dataclass(frozen=True, slots=True)
class CultureConfig:
    enabled: bool = False
    # Part C's switches, each off by default inside enabled culture (bots was reserved as true
    # and never read; it is off now so that an enabled culture gains nothing until asked).
    bots: bool = False
    memes_per_week: int = 3
    calendar: bool = False
    loot: bool = False
    emoji: bool = False
    memes: bool = False


def load_culture_config(path: Path) -> CultureConfig:
    path = Path(path)
    try:
        table = tomllib.loads(path.read_bytes().decode("utf-8-sig")).get("culture", {})
        if not isinstance(table, dict) or set(table) - {f.name for f in fields(CultureConfig)}:
            return CultureConfig()
        for key, value in table.items():
            if key == "memes_per_week":
                if type(value) is not int or not 0 <= value <= 14:
                    return CultureConfig()
            elif type(value) is not bool:
                return CultureConfig()
        cfg = CultureConfig(**table)
        if cfg.enabled and not load_server_config(path).enabled:
            return CultureConfig()
        return cfg
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        return CultureConfig()


def enabled(ctx):
    return bool(getattr(ctx.settings, "culture_enabled", False))


def app_enabled(app):
    """The startup choice is shared by live and nightly callers until restart."""
    return getattr(app, "culture_config", CultureConfig()).enabled


def app_calendar(app):
    """Part B's server calendar: its own switch inside enabled culture."""
    cfg = getattr(app, "culture_config", CultureConfig())
    return cfg.enabled and cfg.calendar


def app_loot(app):
    """Part B's loot talk and item tooltips: its own switch inside enabled culture."""
    cfg = getattr(app, "culture_config", CultureConfig())
    return cfg.enabled and cfg.loot


def app_bots(app):
    """Part C's server bots: their own switch inside enabled culture."""
    cfg = getattr(app, "culture_config", CultureConfig())
    return cfg.enabled and cfg.bots


def app_emoji(app):
    """Part C's custom role and class emoji: their own switch inside enabled culture."""
    cfg = getattr(app, "culture_config", CultureConfig())
    return cfg.enabled and cfg.emoji


def app_memes(app):
    """Part C's memes (text memes, and image memes when the selfie worker renders): their own
    switch inside enabled culture. memes_per_week 0 still allows text memes."""
    cfg = getattr(app, "culture_config", CultureConfig())
    return cfg.enabled and cfg.memes
