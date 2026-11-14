"""Culture is enabled only by two complete valid tables."""
from dataclasses import dataclass, fields
from pathlib import Path
import tomllib
from leeroy_mind.server.config import load_server_config

@dataclass(frozen=True, slots=True)
class CultureConfig:
    enabled: bool = False
    bots: bool = True
    memes_per_week: int = 3


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
