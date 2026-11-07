"""The [selfies] table of mind.toml, and the worker's command line and environment. config.py
leaves the table alone (EXTENSION_SECTIONS); it is read here, and only here, so a mind without
it behaves exactly as before. The worker is the selfie/ package in its own venv: the mind runs
it as a subprocess and never imports it."""
import os
import tomllib
from dataclasses import dataclass, fields
from pathlib import Path

from leeroy_mind.config import ConfigError, log

CHECKOUT = Path(__file__).resolve().parents[3]
SIZES = (768, 896, 1024)


@dataclass(frozen=True)
class SelfieCfg:
    enabled: bool = False
    python: Path | None = None  # the worker's interpreter; None: <checkout>/selfie/.venv's
    data: Path | None = None  # its models; None: LEEROY_SELFIE_DATA, else <checkout>/selfie-data
    nsfw_max: float = 0.2  # an image scoring this or more is never written
    steps: int = 30
    guidance: float = 5.0  # the CFG scale (the protocol's cfg)
    size: int = 1024
    min_free_gib: float = 11.5  # the worker refuses to load with less VRAM free


def _bool(v) -> bool:
    return type(v) is bool


def _num(lo: float, hi: float):
    return lambda v: type(v) in (int, float) and lo <= v <= hi


def _int(lo: int, hi: int):
    return lambda v: type(v) is int and lo <= v <= hi


def _absolute(v) -> bool:
    """A path the worker is started from must never depend on the mind's working directory or
    on PATH (lifecycle.py: never select Python by name alone)."""
    return type(v) is str and v.strip() == v != "" and Path(v).is_absolute()


_RULES = {
    "enabled": (_bool, "true or false"),
    "python": (_absolute, "an absolute path"),
    "data": (_absolute, "an absolute path"),
    "nsfw_max": (_num(0.01, 0.9), "a number 0.01-0.9"),
    "steps": (_int(10, 60), "an integer 10-60"),
    "guidance": (_num(1, 12), "a number 1-12"),
    "size": (lambda v: type(v) is int and v in SIZES, "768, 896 or 1024"),
    "min_free_gib": (_num(4, 23), "a number 4-23"),
}
_FLOATS = {"nsfw_max", "guidance", "min_free_gib"}
_PATHS = {"python", "data"}


def load_selfie_config(path: Path) -> SelfieCfg:
    """The [selfies] table of the mind.toml at `path`; a missing file or table means the
    defaults (off). Raises ConfigError for a file it cannot read or a bad value, naming the
    key."""
    path = Path(path)
    if not path.exists():
        return SelfieCfg()
    try:
        data = tomllib.loads(path.read_bytes().decode("utf-8-sig"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        raise ConfigError(f"{path}: cannot read [selfies]: {e.__class__.__name__}") from e
    table = data.get("selfies")
    if table is None:
        return SelfieCfg()
    if not isinstance(table, dict):
        raise ConfigError("selfies: expected a table")
    values = {}
    for key, v in table.items():
        rule = _RULES.get(key)
        if rule is None:
            log.warning("%s: unknown key selfies.%s", path, key)
            continue
        if not rule[0](v):
            raise ConfigError(f"selfies.{key}: expected {rule[1]}, got {v!r}")
        values[key] = float(v) if key in _FLOATS else Path(v) if key in _PATHS else v
    assert set(values) <= {f.name for f in fields(SelfieCfg)}
    return SelfieCfg(**values)


def worker_python(cfg: SelfieCfg) -> Path:
    if cfg.python is not None:
        return cfg.python
    venv = CHECKOUT / "selfie" / ".venv"
    return venv / "Scripts" / "python.exe" if os.name == "nt" else venv / "bin" / "python"


def worker_data(cfg: SelfieCfg) -> Path:
    if cfg.data is not None:
        return cfg.data
    env = os.environ.get("LEEROY_SELFIE_DATA")
    return Path(env) if env else CHECKOUT / "selfie-data"


def worker_command(cfg: SelfieCfg) -> list[str]:
    """The night worker, isolated (-I: no PYTHON* variables, no user site, no script dir)."""
    return [str(worker_python(cfg)), "-I", "-X", "utf8", "-m", "leeroy_selfie", "worker",
            "--data", str(worker_data(cfg)), "--min-free-gib", f"{cfg.min_free_gib:g}"]


def worker_env(cfg: SelfieCfg) -> dict[str, str]:
    """A copy of this process's environment with the model hub kept offline: the worker loads
    only what fetch-models pinned and never phones home."""
    return {**os.environ, "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
            "HF_HUB_DISABLE_TELEMETRY": "1", "LEEROY_SELFIE_DATA": str(worker_data(cfg))}
