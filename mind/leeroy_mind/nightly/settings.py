"""The night's settings: the knobs P1b's mind.toml already has, the optional [life] dial
(roadmap amendment A2) and P1c's own tunables, which stay constants here; and the night, week
and season helpers. A night runs from 06:00 to the next 06:00 in the clock's zone
(clock.night_of)."""
import tomllib
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, tzinfo
from pathlib import Path

from leeroy_mind.clock import night_of
from leeroy_mind.config import ConfigError, MindConfig

WOW_VS_LIFE = 5  # [life] wow_vs_life when the file, the section or the key is missing
_NIGHT_STARTS = time(6)


@dataclass(frozen=True)
class NightlySettings:
    """From the config: the models, the Ollama URL and the knobs. romance_dial is reported only,
    never read for a decision; female_share is the cast mix the weekly report compares
    against. The rest are P1c's constants."""
    live_model: str
    reflection_model: str
    embedding_model: str
    ollama_url: str
    relationship_speed: int
    romance_dial: int
    jealousy: int
    chattiness: int
    wow_vs_life: int
    female_share: float
    data_dir: Path
    grace_minutes: int = 10
    retry_minutes: int = 60
    max_attempts: int = 3
    parallel: int = 2
    digest_size: int = 60
    min_new_memories: int = 3
    questions: int = 3
    retrieve_k: int = 10
    max_outputs: int = 5
    encounters_per_night: int = 150
    encounters_per_member: int = 3
    volitions_per_member: int = 2
    volition_scenes: int = 40
    gossip_trust_min: float = 50.0
    last_to_know_min: int = 5
    live_rumors: int = 3
    confide_per_night: int = 60
    enrich_per_night: int = 10
    stories_per_night: int = 3
    notes_per_night: int = 300
    wow_lines_per_night: int = 40
    notice_night_hours: float = 6.0
    notice_week_hours: float = 30.0
    notice_members: int = 12
    absent_days: int = 3
    follow_facts: int = 3
    follow_members: int = 3
    jokes_per_night: int = 5
    slang_per_night: int = 5
    lexicon_candidates: int = 3
    adopt_max: int = 8
    nicknames_per_week: int = 1
    feuds_per_night: int = 2
    mentors_max: int = 5
    deep_per_week: int = 1
    critic_per_member: int = 2
    critic_lines: int = 60
    season_weeks: int = 8
    probe_max_rate: float = 0.1
    min_window_hours: float = 6.0
    min_gap_hours: float = 6.0
    budget_floor_hours: float = 2.0
    questions_min_new: int = 0
    profile_per_night: int = 1
    interview_members: int = 8


def load_life(path: Path) -> int:
    """[life] wow_vs_life from the mind.toml at `path`: how present the WoW-vs-life theme is,
    0-10, and 0 turns it off. A missing file, section or key gives WOW_VS_LIFE; a value that is
    not an integer 0-10 (a bool is not) raises ConfigError. P1b's loader skips the section
    (config.EXTENSION_SECTIONS)."""
    path = Path(path)
    if not path.exists():
        return WOW_VS_LIFE
    try:
        data = tomllib.loads(path.read_bytes().decode("utf-8-sig"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        raise ConfigError(f"{path}: {e}") from e
    life = data.get("life", {})
    if not isinstance(life, dict):
        raise ConfigError("life: expected a table")
    v = life.get("wow_vs_life", WOW_VS_LIFE)
    if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= 10:
        raise ConfigError(f"life.wow_vs_life: expected an integer 0-10, got {v!r}")
    return v


def load_settings(cfg: MindConfig, data_dir: Path) -> NightlySettings:
    data_dir = Path(data_dir)
    return NightlySettings(
        live_model=cfg.models.live, reflection_model=cfg.models.reflection,
        embedding_model=cfg.models.embedding, ollama_url=cfg.ollama.url,
        relationship_speed=cfg.memory.relationship_speed, romance_dial=cfg.romance.dial,
        jealousy=cfg.romance.jealousy, chattiness=cfg.talk.chattiness,
        wow_vs_life=load_life(data_dir / "mind.toml"), female_share=cfg.cast.female_share,
        data_dir=data_dir)


def _at_six(day: date, tz: tzinfo) -> float:
    return datetime.combine(day, _NIGHT_STARTS, tzinfo=tz).timestamp()


def night_of_ts(ts: float, tz: tzinfo) -> str:
    return night_of(datetime.fromtimestamp(ts, tz))


def night_bounds(night: str, tz: tzinfo) -> tuple[float, float]:
    """The night's 06:00 to the next day's 06:00, unix seconds."""
    day = date.fromisoformat(night)
    return _at_six(day, tz), _at_six(day + timedelta(days=1), tz)


def iso_week(night: str) -> str:
    """The night's ISO week, such as "2026-W44"."""
    year, week, _ = date.fromisoformat(night).isocalendar()
    return f"{year}-W{week:02d}"


def is_sunday(night: str) -> bool:
    return date.fromisoformat(night).isoweekday() == 7


def week_bounds(week: str, tz: tzinfo) -> tuple[float, float]:
    """An ISO week ("2026-W44") from its Monday 06:00 to the next Monday 06:00."""
    year, number = week.split("-W")
    monday = date.fromisocalendar(int(year), int(number), 1)
    return _at_six(monday, tz), _at_six(monday + timedelta(days=7), tz)


def _last_week(year: int) -> int:
    return date(year, 12, 28).isocalendar().week  # 28 December is always in the last ISO week


def season_of(night: str, weeks: int) -> str:
    """The season of `weeks` ISO weeks the night falls in, such as "2026-S6"; a year's last
    season ends with its last ISO week."""
    year, week, _ = date.fromisoformat(night).isocalendar()
    return f"{year}-S{(week - 1) // weeks + 1}"


def is_season_end(night: str, weeks: int) -> bool:
    """The Sunday of an ISO week whose number is a multiple of `weeks`, or of the year's last
    ISO week."""
    year, week, day = date.fromisoformat(night).isocalendar()
    return day == 7 and (week % weeks == 0 or week == _last_week(year))


def season_bounds(season: str, weeks: int, tz: tzinfo) -> tuple[float, float]:
    """A season ("2026-S6") from its first Monday 06:00 to the Monday 06:00 after it ends."""
    year, number = (int(x) for x in season.split("-S"))
    first = (number - 1) * weeks + 1
    last = min(number * weeks, _last_week(year))
    start = date.fromisocalendar(year, first, 1)
    end = date.fromisocalendar(year, last, 1) + timedelta(days=7)
    return _at_six(start, tz), _at_six(end, tz)
