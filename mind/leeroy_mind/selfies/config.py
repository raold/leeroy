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
    guidance: float = 5.0  # the protocol's cfg; TOML defaults FLUX to native guidance 3.5
    size: int = 1024
    min_free_gib: float = 11.5  # the worker refuses to load with less VRAM free
    snaps: bool = False  # situational photos and the guild photo (the identity worker)
    booth: bool = False  # profile sets between nights, while the owner is offline
    booth_wait_minutes: int = 10
    booth_sets_per_day: int = 6
    identity_scale: float = 0.6  # how strongly the adapter holds the reference face
    identity_min_free_gib: float = 13.5  # the identity worker loads the adapter too
    variety: bool = False  # balanced lust choices and recent plain-photo observations
    varied_sets: bool = False  # varied.py: four looks and poses in one set for the lust style
    identity_sets: bool = False  # profile sets carry her chosen photo's face (the identity worker)
    romance_styling: bool = False  # clothed wardrobe and expressions in private situational photos
    # The photo critic (critic.py): detectors and a vision judge measure every rendered
    # situational, request and profile-set photo; a defective situational or request photo is
    # never sent. best_of renders that many seeds of a situational or request photo and sends
    # the best (needs critic; 1 = one seed, as before). param_bandit lets the critic and his
    # picks choose identity scale and steps (params.py; needs critic).
    # How much body shows (safe.py, the photo audit of 2026-11-17). body_shot_share: the share
    # of a varied set's four candidates that are three-quarter or full-length (the views from
    # behind first; the rest waist-up) and of the lust profile kinds that are three-quarter
    # (else waist-up); 1.0, four body views, is the owner's ask and what the labels support.
    # snap_body_share: the share of situational photos that keep a waist-up or wider framing,
    # the rest head and shoulders (29% of the waist-up ones defective, none of the closer ones).
    body_shot_share: float = 1.0
    snap_body_share: float = 0.5
    critic: bool = False
    best_of: int = 1
    critic_model: str = "qwen2.5vl:7b"  # pulled by the operator; absent: detectors only
    param_bandit: bool = False
    # The detailer (the worker's detail.py): after a render, each small face is drawn again at
    # a higher resolution (crossed eyes), and with detailer_hands each hand too (six fingers;
    # needs the worker's palm and handpose models fetched). detailer_hands needs detailer.
    detailer: bool = False
    detailer_hands: bool = False
    backend: str = "realvisxl"  # opt-in FLUX.1-dev; the existing renderer stays the default


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
    "snaps": (_bool, "true or false"),
    "booth": (_bool, "true or false"),
    "booth_wait_minutes": (_int(1, 60), "an integer 1-60"),
    "booth_sets_per_day": (_int(1, 120), "an integer 1-120"),
    "identity_scale": (_num(0.3, 0.9), "a number 0.3-0.9"),
    "identity_min_free_gib": (_num(4, 23), "a number 4-23"),
    "variety": (_bool, "true or false"),
    "varied_sets": (_bool, "true or false"),
    "identity_sets": (_bool, "true or false"),
    "romance_styling": (_bool, "true or false"),
    "body_shot_share": (_num(0, 1), "a number 0-1"),
    "snap_body_share": (_num(0, 1), "a number 0-1"),
    "critic": (_bool, "true or false"),
    "best_of": (_int(1, 4), "an integer 1-4"),
    "critic_model": (lambda v: type(v) is str and 1 <= len(v) <= 100 and v.strip() == v
                     and v.isprintable(), "an Ollama model name"),
    "param_bandit": (_bool, "true or false"),
    "detailer": (_bool, "true or false"),
    "detailer_hands": (_bool, "true or false"),
    "backend": (lambda v: type(v) is str and v in ("realvisxl", "flux1-dev"),
                "realvisxl or flux1-dev"),
}
_FLOATS = {"nsfw_max", "guidance", "min_free_gib", "identity_scale", "identity_min_free_gib",
           "body_shot_share", "snap_body_share"}
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
            raise ConfigError(f"selfies.{key}: expected {rule[1]}")
        values[key] = float(v) if key in _FLOATS else Path(v) if key in _PATHS else v
    assert set(values) <= {f.name for f in fields(SelfieCfg)}
    if set(table) - _RULES.keys():
        values["variety"] = False  # an invalid/unknown table never opts into new behaviour
        values["varied_sets"] = False
        values["romance_styling"] = False
        values["critic"] = False
        values["best_of"] = 1
        values["param_bandit"] = False
        values["detailer"] = False
        values["detailer_hands"] = False
        values["backend"] = "realvisxl"
    if values.get("backend") == "flux1-dev":
        values.setdefault("guidance", 3.5)
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


def worker_command(cfg: SelfieCfg, *, identity: bool = False, embedding: bool = False) -> list[str]:
    """The night worker, isolated (-I: no PYTHON* variables, no user site, no script dir). With
    `identity` it loads the identity adapter too and asks for identity_min_free_gib."""
    free = cfg.identity_min_free_gib if identity else cfg.min_free_gib
    return [str(worker_python(cfg)), "-I", "-X", "utf8", "-m", "leeroy_selfie", "worker",
            *(["--embed-only"] if embedding else []),
            *(["--identity"] if identity else []),
            *(["--backend", cfg.backend] if cfg.backend != "realvisxl" and not embedding else []),
            "--data", str(worker_data(cfg)), "--min-free-gib", f"{free:g}"]


def worker_env(cfg: SelfieCfg) -> dict[str, str]:
    """A copy of this process's environment with the model hub kept offline: the worker loads
    only what fetch-models pinned and never phones home."""
    return {**os.environ, "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
            "HF_HUB_DISABLE_TELEMETRY": "1", "LEEROY_SELFIE_DATA": str(worker_data(cfg))}
