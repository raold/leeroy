import ipaddress
import logging
import os
import tomllib
import urllib.parse
from dataclasses import dataclass, field
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
whispers_per_hour = 4         # members whispering him first, per hour of his play, at chattiness 4
openers_per_hour = 2          # members starting a conversation in party or guild, per hour, likewise
openers_to_him = 0.35         # 0-1: the share of those openers addressed to him; the rest are guild business
selfies = 3                    # 0-10, member photos in your DMs and guild channels (rendered at night)
aware = false                  # include a bounded card of visible observations in talk
realism = false                # bounded member typing, small bursts and rare corrections
texture = false                # members type like people: apostrophes, run-ons, shorthand and slips some of the time
dm_own_time = false            # members answer his Discord DMs when their presence says they'd see them
discord_words = 90             # 0-200: most words in a member's Discord channel message (0: one short line, as in game)
dm_words = 200                 # 0-200: most words in a member's DM to him (0: one short line)
think_private = true           # a short structured beat before answering his whispers and DMs (read at mind start)
think_budget = 240             # 120-320: the beat's num_predict, in tokens
recall_diversity = false       # memories: past plans read as past, no repeats in a conversation, tired subjects rest
intent = false                 # her want for each turn, drawn by code, in the beat's block on every channel
situation = false              # the prompt as her situation: hard rules only, "Right now" by the instruction
shape = false                  # each reply's shape drawn by code: length, burst, question back, late excuse, left on read
dm_mirror = 5                  # 0-10, with shape on: how closely a DM's length follows his (0: as full as she likes)
coherence = false              # conversations hold together (talk/README.md): long game lines kept, her want and place hold, every waiting whisper answered; read at start and at his login
wow_grounding = true           # raid and guild turns get facts (her class and role, the raid, its boss, the last wipe, loot and DKP), raid words and her own earlier lines; never a whisper or a DM
wow_exemplars_before = "2026-11-19T14:22:45-05:00"  # her example lines are from before this (the live model's switch from Orion)
invites = true                 # his invitations in game ("come to the inn", "let's go fishing"): her answer and her body agree (invites/README.md)

[life]
availability = 0              # 0-10: how often members are away, busy or slow to answer by their day (0: always there)
state = false                 # her feeling through the day
dm_wait_cap_s = 75            # the longest his DM waits for her answer, whatever her day (0: no cap; asleep: twice this)
wow_vs_life = 5               # 0-10: how present the WoW-vs-life theme is in the night's stories (0: off)

[romance]
dial = 3                      # 1 milquetoast .. 10 filth
jealousy = 5                  # 0-10: women's rivalry and the men's envy
activities = false            # private shared moments, moods and inn invitations
realism = false               # her romance DMs and whispers answer him first and escalate only when he does

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
MAX_PER_HOUR = 60  # [talk] whispers_per_hour and openers_per_hour
DISCORD_WORDS_MAX = 200  # [talk] discord_words and dm_words
DM_MIRROR_MAX = 10  # [talk] dm_mirror
DM_WAIT_CAP_MAX = 3600  # [life] dm_wait_cap_s
THINK_BUDGET_RANGE = (120, 320)  # [talk] think_budget: the beat's tokens; 320 still fits a full DM prompt
_WARNED: set[str] = set()  # invalid [talk] and [life] values already logged in this process
# The live model's switch from Orion (mind.log: the first start with another live model,
# 2026-11-19 14:22:45 local): the WoW grounding's example lines are hers from before it
EXEMPLARS_BEFORE = "2026-11-19T14:22:45-05:00"
EXTENSION_SECTIONS = frozenset({"console", "guild", "dkp", "voice", "training", "realm",
                                "control", "selfies", "health", "claims", "personas", "guards",
                                "tuning", "playstyle", "signals", "fingerprint", "callbacks", "world", "learn", "looks", "server",
                                "banter", "photos", "voice_notes", "groups", "culture", "outreach",
                                "godmode", "decisions", "raidlead", "raidplay", "drama", "turing",
                                "bakeoff", "lab", "prune", "heat"})
_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


class ConfigError(ValueError):
    pass


def exemplars_time(value) -> float | None:
    """[talk] wow_exemplars_before as Unix seconds: an ISO date and time that names its UTC
    offset ("2026-11-19T14:22:45-05:00"); None for anything else."""
    from datetime import datetime
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        when = datetime.fromisoformat(value)
    except ValueError:
        return None
    return when.timestamp() if when.tzinfo is not None else None


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
    whispers_per_hour: float = 4
    openers_per_hour: float = 2
    openers_to_him: float = 0.35  # the share of the openers addressed to him (talk.engine)
    selfies: int = 3
    aware: bool = False
    realism: bool = False
    texture: bool = False  # [talk] texture; talk/style.py texture_of
    dm_own_time: bool = False
    discord_words: int = 90  # [talk] discord_words; talk/discord_length.py
    dm_words: int = 200  # [talk] dm_words: his DMs, as full as she likes
    think_private: bool = True  # [talk] think_private: talk/beat.py, read at mind start
    think_budget: int = 240  # [talk] think_budget: the beat's num_predict
    recall_diversity: bool = False  # [talk] recall_diversity: memory/diversity.py
    intent: bool = False  # [talk] intent: talk/intent.py's want on every eligible turn (core Task 8)
    situation: bool = False  # [talk] situation: talk/prompt.py's situation mode (core Task 8)
    shape: bool = False  # [talk] shape: talk/shape.py's drawn reply shape (core Task 9)
    dm_mirror: int = 5  # [talk] dm_mirror: with shape on, a DM's length follows his (0: today's)
    coherence: bool = False  # [talk] coherence: talk/length.py, talk/position.py (anything but true: off)
    # [talk] wow_grounding: talk/grounding.py on raid and guild turns (anything but true: off)
    wow_grounding: bool = True
    # [talk] wow_exemplars_before: an ISO time with its offset; her example lines are older
    wow_exemplars_before: str = EXEMPLARS_BEFORE
    invites: bool = True  # [talk] invites: invites/service.py (a non-boolean: off, logged once)


@dataclass(frozen=True)
class LifeCfg:
    """[life]: her day (life/), and the night's WoW-vs-life theme (nightly.settings.load_life)."""
    availability: int = 0  # 0-10; 0: always present, today's behaviour (life/config.py pin)
    state: bool = False  # her feeling through the day
    # the longest his DM (and a group DM line that names her) waits for her answer, in
    # seconds, whatever her day; asleep twice this; 0: no cap (discord/owntime.py)
    dm_wait_cap_s: int = 75
    wow_vs_life: int = 5


@dataclass(frozen=True)
class RomanceCfg:
    dial: int
    jealousy: int
    activities: bool = False
    realism: bool = False  # [romance] realism: talk/romance.py REALISM_LEAD


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
    life: LifeCfg = field(default_factory=LifeCfg)


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
        "aware": (lambda v: type(v) is bool, "a boolean"),
        "realism": (lambda v: type(v) is bool, "a boolean"),
        "texture": (lambda v: type(v) is bool, "a boolean"),
        "dm_own_time": (lambda v: type(v) is bool, "a boolean"),
        "chattiness": (lambda v: _int_in(v, 0, 10), "an integer 0-10"),
        "raid_rule": (lambda v: v in RAID_RULES, f"one of {sorted(RAID_RULES)}"),
        "selfies": (lambda v: _int_in(v, 0, 10), "an integer 0-10"),
        "discord_words": (lambda v: _int_in(v, 0, DISCORD_WORDS_MAX), f"an integer 0-{DISCORD_WORDS_MAX}"),
        "dm_words": (lambda v: _int_in(v, 0, DISCORD_WORDS_MAX), f"an integer 0-{DISCORD_WORDS_MAX}"),
        "think_private": (lambda v: type(v) is bool, "a boolean"),
        "recall_diversity": (lambda v: type(v) is bool, "a boolean"),
        "think_budget": (lambda v: _int_in(v, *THINK_BUDGET_RANGE), "an integer %d-%d" % THINK_BUDGET_RANGE),
        "intent": (lambda v: type(v) is bool, "a boolean"),
        "situation": (lambda v: type(v) is bool, "a boolean"),
        "shape": (lambda v: type(v) is bool, "a boolean"),
        "dm_mirror": (lambda v: _int_in(v, 0, DM_MIRROR_MAX), f"an integer 0-{DM_MIRROR_MAX}"),
        "coherence": (lambda v: type(v) is bool, "a boolean"),
        "wow_grounding": (lambda v: type(v) is bool, "a boolean"),
        "wow_exemplars_before": (lambda v: exemplars_time(v) is not None,
                                 "an ISO date and time with its UTC offset"),
        "invites": (lambda v: type(v) is bool, "a boolean"),
        **{k: (lambda v: _num_in(v, 0, MAX_PER_HOUR), f"a number 0-{MAX_PER_HOUR}")
           for k in ("whispers_per_hour", "openers_per_hour")},
        "openers_to_him": (lambda v: _num_in(v, 0, 1), "a number 0-1"),
    },
    "life": {
        "availability": (lambda v: _int_in(v, 0, 10), "an integer 0-10"),
        "state": (lambda v: type(v) is bool, "a boolean"),
        "dm_wait_cap_s": (lambda v: _int_in(v, 0, DM_WAIT_CAP_MAX),
                          f"an integer 0-{DM_WAIT_CAP_MAX}"),
        "wow_vs_life": (lambda v: _int_in(v, 0, 10), "an integer 0-10"),
    },
    "romance": {
        "dial": (lambda v: _int_in(v, 1, 10), "an integer 1-10"),
        "jealousy": (lambda v: _int_in(v, 0, 10), "an integer 0-10"),
        "activities": (lambda v: type(v) is bool, "a boolean"),
        "realism": (lambda v: type(v) is bool, "a boolean"),
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
    "life": LifeCfg,
}


def _build(data: dict, source: str) -> MindConfig:
    defaults = tomllib.loads(DEFAULT_TOML)
    if "life" in data and not isinstance(data["life"], dict):
        # [life] never blocks the rest of mind.toml: read as empty, logged once a value
        if ("life", repr(data["life"])) not in _WARNED:
            _WARNED.add(("life", repr(data["life"])))
            log.warning("life_invalid want=a table")
        data = dict(data, life={})
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
        if section == "talk" and type(given.get("aware", False)) is not bool:
            log.warning("talk_aware_invalid key=aware")
            given = dict(given, aware=False)
        values = {}
        for key, (check, want) in rules.items():
            v = given.get(key, defaults[section][key])
            if section == "talk" and key in ("realism", "texture", "dm_own_time",
                                             "think_private", "recall_diversity",
                                             "coherence") and type(v) is not bool:
                v = False
            if section == "talk" and key == "wow_grounding" and type(v) is not bool:
                if (key, repr(v)) not in _WARNED:  # off, logged once (each login reads it again)
                    _WARNED.add((key, repr(v)))
                    log.warning("talk_wow_grounding_invalid key=wow_grounding want=a boolean "
                                "default=false")
                v = False
            if section == "talk" and key == "wow_exemplars_before" and not check(v):
                if (key, repr(v)) not in _WARNED:  # the default, logged once
                    _WARNED.add((key, repr(v)))
                    log.warning("talk_wow_exemplars_before_invalid key=wow_exemplars_before "
                                "want=%s default=%s", want, defaults[section][key])
                v = defaults[section][key]
            if (section == "talk" and key in ("intent", "situation", "shape", "invites")
                    and type(v) is not bool):
                if (key, repr(v)) not in _WARNED:  # off, logged once (each login reads it again)
                    _WARNED.add((key, repr(v)))
                    log.warning("talk_%s_invalid key=%s want=%s default=false", key, key, want)
                v = False
            if section == "talk" and key in ("discord_words", "dm_words", "think_budget",
                                             "dm_mirror") and not check(v):
                if (key, repr(v)) not in _WARNED:  # the default, logged once (each login reads it again)
                    _WARNED.add((key, repr(v)))
                    log.warning("talk_%s_invalid key=%s want=%s default=%s", key, key,
                                want, defaults[section][key])
                v = defaults[section][key]
            if section == 'romance' and key in ('activities', 'realism') and type(v) is not bool:
                v = False
            if section == "life" and not check(v):
                # the default (availability 0, state false, dm_wait_cap_s 75, wow_vs_life 5),
                # logged once a value
                # (each login reads it again); the night's load_life still refuses wow_vs_life
                if ("life." + key, repr(v)) not in _WARNED:
                    _WARNED.add(("life." + key, repr(v)))
                    log.warning("life_%s_invalid key=%s want=%s default=%s", key, key, want,
                                defaults[section][key])
                v = defaults[section][key]
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
