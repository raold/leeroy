import ipaddress
import logging
import os
import tomllib
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("leeroy_mind")

DEFAULT_TOML = '''\
[models]
# Owner's blind test 2026-10-29: Orion 4/8, Cydonia 2/8, qwen3:14b 1/8, gemma4:12b 1/8.
live = "hf.co/bartowski/TheDrummer_Orion-26B-A4B-v1.1-GGUF:IQ4_XS"
live_fallback = "hf.co/bartowski/TheDrummer_Cydonia-24B-v4.3-GGUF:IQ4_XS"  # owner's swap-in, never loaded on its own
reflection = "qwen3.8:27b-q4_K_M"
embedding = "qwen3-embedding:0.6b"

[talk]
chattiness = 4                # 0-10, social chat outside raids
raid_rule = "leaders_and_urgent"

[romance]
dial = 3                      # 1 milquetoast .. 10 filth
jealousy = 5                  # 0-10: women's rivalry and the men's envy

[cast]
female_share = 0.7
introvert_share = 0.7

[memory]
depth_days = 5                # recency half-life tau
relationship_speed = 10       # max per-dimension change per pair per night

[bridge]
host = "127.0.0.1"            # loopback only
port = 7878

[human]
nickname = ""                # what the guild calls the owner in game
real_name = ""               # known to close members; keep it only in the local mind.toml
pronouns = ""                # how the guild refers to the owner, e.g. "he" (read as he/him)

[ollama]
url = "http://127.0.0.1:11434"  # plain http on this machine only
max_inflight = 3              # 1-4 of Ollama's 4 slots
'''

RAID_RULES = frozenset({"leaders_and_urgent"})
EXTENSION_SECTIONS = frozenset({"guild", "dkp"})
_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class ModelsCfg:
    live: str
    live_fallback: str
    reflection: str
    embedding: str


@dataclass(frozen=True)
class TalkCfg:
    chattiness: int
    raid_rule: str


@dataclass(frozen=True)
class RomanceCfg:
    dial: int
    jealousy: int


@dataclass(frozen=True)
class CastCfg:
    female_share: float
    introvert_share: float


@dataclass(frozen=True)
class MemoryCfg:
    depth_days: float
    relationship_speed: int


@dataclass(frozen=True)
class BridgeCfg:
    host: str
    port: int


@dataclass(frozen=True)
class HumanCfg:
    nickname: str
    real_name: str
    pronouns: str


@dataclass(frozen=True)
class OllamaCfg:
    url: str
    max_inflight: int


@dataclass(frozen=True)
class MindConfig:
    models: ModelsCfg
    talk: TalkCfg
    romance: RomanceCfg
    cast: CastCfg
    memory: MemoryCfg
    bridge: BridgeCfg
    ollama: OllamaCfg
    human: HumanCfg


def _int_in(v, lo, hi):
    return isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi


def _num_in(v, lo, hi):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and lo <= v <= hi


def _is_str(v):
    return isinstance(v, str) and v != ""


def _short_str(v):
    return isinstance(v, str) and len(v.strip()) <= 24 and (v == "" or v.strip() != "")


def _loopback_http(v):
    """http://, on 127.0.0.1, localhost or ::1: the client reads with select (wrong for TLS),
    and the guild's chat must never leave the machine."""
    if not isinstance(v, str):
        return False
    try:
        u = urllib.parse.urlsplit(v)
        u.port  # noqa: B018 - raises on a bad port
    except ValueError:
        return False
    if u.scheme != "http" or not u.hostname:
        return False
    if u.hostname == "localhost":
        return True
    try:
        return ipaddress.ip_address(u.hostname).is_loopback
    except ValueError:
        return False


_INF = float("inf")

# section -> key -> (type check, constraint text)
_RULES = {
    "models": {k: (_is_str, "a non-empty string") for k in ("live", "live_fallback", "reflection", "embedding")},
    "talk": {
        "chattiness": (lambda v: _int_in(v, 0, 10), "an integer 0-10"),
        "raid_rule": (lambda v: v in RAID_RULES, f"one of {sorted(RAID_RULES)}"),
    },
    "romance": {
        "dial": (lambda v: _int_in(v, 1, 10), "an integer 1-10"),
        "jealousy": (lambda v: _int_in(v, 0, 10), "an integer 0-10"),
    },
    "cast": {
        "female_share": (lambda v: _num_in(v, 0, 1), "a number 0-1"),
        "introvert_share": (lambda v: _num_in(v, 0, 1), "a number 0-1"),
    },
    "memory": {
        "depth_days": (lambda v: _num_in(v, 0, _INF) and v > 0, "a number > 0"),
        "relationship_speed": (lambda v: _int_in(v, 1, 100), "an integer 1-100"),
    },
    "bridge": {
        "host": (lambda v: v in _HOSTS, f"one of {sorted(_HOSTS)}"),
        "port": (lambda v: _int_in(v, 1, 65535), "an integer 1-65535"),
    },
    "ollama": {
        "url": (_loopback_http, "an http:// URL on 127.0.0.1, localhost or [::1]"),
        "max_inflight": (lambda v: _int_in(v, 1, 4), "an integer 1-4"),
    },
    "human": {k: (_short_str, "a string of at most 24 characters")
              for k in ("nickname", "real_name", "pronouns")},
}

_CLASSES = {
    "models": ModelsCfg, "talk": TalkCfg, "romance": RomanceCfg, "cast": CastCfg,
    "memory": MemoryCfg, "bridge": BridgeCfg, "ollama": OllamaCfg, "human": HumanCfg,
}


def _build(data: dict, source: str) -> MindConfig:
    defaults = tomllib.loads(DEFAULT_TOML)
    for section in data:
        if section in EXTENSION_SECTIONS:
            continue
        if section not in _RULES:
            log.warning("%s: unknown section [%s]", source, section)
            continue
        if not isinstance(data[section], dict):
            raise ConfigError(f"{section}: expected a table")
        for key in data[section]:
            if key not in _RULES[section]:
                log.warning("%s: unknown key %s.%s", source, section, key)
    built = {}
    for section, rules in _RULES.items():
        given = data.get(section, {})
        values = {}
        for key, (check, want) in rules.items():
            v = given.get(key, defaults[section][key])
            if not check(v):
                raise ConfigError(f"{section}.{key}: expected {want}, got {_shown(section, v)}")
            values[key] = v.strip() if section == "human" else v
        built[section] = _CLASSES[section](**values)
    return MindConfig(**built)


def _shown(section: str, v) -> str:
    """A bad value as an error shows it: [human] holds the owner's names, so only its shape."""
    if section != "human":
        return repr(v)
    return f"a {len(v)}-character string" if isinstance(v, str) else f"a {type(v).__name__}"


def load_config(path: Path) -> MindConfig:
    """A missing file means the defaults; one that cannot be read, decoded as UTF-8 or parsed
    raises ConfigError (the login reload keeps the running config on it)."""
    path = Path(path)
    if not path.exists():
        return _build({}, str(path))
    try:
        raw = path.read_bytes()
    except OSError as e:
        raise ConfigError(f"{path}: cannot read it: {e.strerror or e}") from e
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        raise ConfigError(f"{path}: not UTF-8 (byte {e.start}); save it as UTF-8") from e
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: {e}") from e
    return _build(data, str(path))


def write_default_config(path: Path) -> None:
    with open(path, "x", encoding="utf-8", newline="\n") as f:
        f.write(DEFAULT_TOML)


def default_data_dir() -> Path:
    env = os.environ.get("LEEROY_MIND_DATA")
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[2] / "mind-data"
