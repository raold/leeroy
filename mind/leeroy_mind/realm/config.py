"""Optional realm settings and the fixed pacing limits from the realm design."""

import re
import tomllib
from dataclasses import dataclass, fields
from datetime import date
from pathlib import Path

from leeroy_mind.config import ConfigError


@dataclass(frozen=True)
class RealmConfig:
    enabled: bool = False
    gank_human: bool = True
    trial_rank: int = 4
    member_cap: int = 120
    his_birthday: str = ""
    guild_anniversary: str = ""


@dataclass(frozen=True, slots=True)
class RealmLimits:
    hour_s: int = 3600
    day_s: int = 86400
    week_s: int = 604800
    rival_guild_lines_per_hour: int = 2
    rival_realm_lines_per_hour: int = 6
    rival_line_gap_s: int = 180
    rival_consecutive_lines: int = 2
    rival_line_deadline_s: int = 20
    rival_human_whispers_per_day: int = 1
    member_reactions_per_hour: int = 10
    poach_offers_per_week: int = 1
    self_events_per_week: int = 2
    self_event_asides: int = 20
    quest_offer_gap_s: int = 1800
    quest_offers_per_woman_per_session: int = 1
    quest_help_duration_s: int = 300
    wg_cast_cap: int = 25
    wg_ganker_cap: int = 15
    ganks_per_week: int = 2
    gank_window_s: int = 1800
    gank_login_guard_s: int = 7200
    recruit_gap_s: int = 2419200
    recruit_silence_s: int = 259200
    live_issues: int = 3
    mail_texts_per_day: int = 3
    mail_delay_min_s: int = 600
    mail_delay_max_s: int = 3600
    absence_letter_after_s: int = 604800
    calendar_creator_live_cap: int = 25
    calendar_native_live_cap: int = 30
    notable_memories_per_member_per_day: int = 1
    night_light_calls: int = 11
    realm_units: int = 24
    day_blocks_min: int = 2
    day_blocks_max: int = 4
    day_block_min_s: int = 3600
    day_block_max_s: int = 10800
    body_commands_per_second: int = 2
    recall_repeat_s: int = 45
    recall_cooldown_s: int = 120
    restore_defaults_s: int = 1800
    disabled_probe_s: int = 600
    grant_retry_s: int = 600
    meeting_postpone_step_s: int = 900
    meeting_postpone_max_s: int = 10800


LIMITS = RealmLimits()


def _bad(key: str, want: str) -> None:
    # Birthdays and other local values must never be echoed in validation errors.
    raise ConfigError(f"realm.{key}: expected {want}")


def _month_day(value: object) -> bool:
    if type(value) is not str:
        return False
    if value == "":
        return True
    if re.fullmatch(r"[0-9]{2}-[0-9]{2}", value) is None:
        return False
    try:
        # Leap-day settings are valid; execution later matches the actual local date.
        date(2000, int(value[:2]), int(value[3:]))
    except ValueError:
        return False
    return True


def load_realm_config(path: Path) -> RealmConfig:
    """Read only [realm]. Missing file/section uses defaults; errors expose no values."""
    try:
        text = Path(path).read_bytes().decode("utf-8-sig")
        data = tomllib.loads(text)
    except FileNotFoundError:
        return RealmConfig()
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(
            f"realm: could not read configuration ({type(exc).__name__})") from None

    given = data.get("realm", {})
    if type(given) is not dict:
        raise ConfigError("realm: expected a table")
    defaults = RealmConfig()
    known = {field.name for field in fields(defaults)}
    for key in given:
        if key not in known:
            _bad(key, "a known setting")
    values = {key: given.get(key, getattr(defaults, key)) for key in known}
    for key in ("enabled", "gank_human"):
        if type(values[key]) is not bool:
            _bad(key, "a boolean")
    if type(values["trial_rank"]) is not int or not 2 <= values["trial_rank"] <= 9:
        _bad("trial_rank", "an integer 2-9")
    # Root's conservative configuration range; this is not a core-capacity claim.
    if type(values["member_cap"]) is not int or not 1 <= values["member_cap"] <= 1000:
        _bad("member_cap", "an integer 1-1000")
    for key in ("his_birthday", "guild_anniversary"):
        if not _month_day(values[key]):
            _bad(key, "an empty string or a valid MM-DD date")
    return RealmConfig(**values)
