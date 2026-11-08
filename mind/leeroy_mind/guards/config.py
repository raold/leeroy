"""Standalone guard settings; malformed input disables the entire package."""
from dataclasses import dataclass, field, fields
import math
from pathlib import Path
import tomllib

from leeroy_mind.eval.talk import nonsequitur, repetition

REPEAT_SECONDS = 3 * 86400
GAG_DAILY_CAP = 2
STOCK_DAILY_CAP = 3


@dataclass(frozen=True)
class GuardsCfg:
    enabled: bool = False
    # The inherited relevance thresholds were measured with word-hash vectors.
    # Semantic enforcement needs its own explicit opt-in after caller calibration.
    semantic_enabled: bool = False
    repeat_overlap: float = repetition.NGRAM_OVERLAP
    repeat_cosine: float = repetition.EMBEDDING_COSINE
    parent_cosine: float = nonsequitur.PARENT_COSINE
    thread_cosine: float = nonsequitur.THREAD_COSINE
    repeat_seconds: int = REPEAT_SECONDS
    gag_daily_cap: int = GAG_DAILY_CAP
    stock_daily_cap: int = STOCK_DAILY_CAP
    gag_caps: dict[int, int] = field(default_factory=dict)


def parse_config(table) -> GuardsCfg:
    try:
        if not isinstance(table, dict) or set(table) - {f.name for f in fields(GuardsCfg)}:
            return GuardsCfg()
        values = dict(table)
        for name in ('enabled', 'semantic_enabled'):
            if name in values and type(values[name]) is not bool:
                return GuardsCfg()
        for name in ('repeat_overlap', 'repeat_cosine', 'parent_cosine', 'thread_cosine'):
            value = values.get(name, getattr(GuardsCfg(), name))
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1:
                return GuardsCfg()
        for name, maximum in (('repeat_seconds', 7*86400), ('gag_daily_cap', 100), ('stock_daily_cap', 100)):
            value = values.get(name, getattr(GuardsCfg(), name))
            if type(value) is not int or not 1 <= value <= maximum:
                return GuardsCfg()
        caps = values.get('gag_caps', {})
        if not isinstance(caps, dict) or len(caps) > 100:
            return GuardsCfg()
        parsed = {}
        for key, value in caps.items():
            if (isinstance(key, bool) or not str(key).isdecimal() or not 0 < int(key) <= 0x7FFFFFFF
                    or type(value) is not int or not 1 <= value <= 100):
                return GuardsCfg()
            parsed[int(key)] = value
        values['gag_caps'] = parsed
        return GuardsCfg(**values)
    except (ValueError, TypeError):
        return GuardsCfg()


def load_guards_config(path: Path) -> GuardsCfg:
    try:
        return parse_config(tomllib.loads(Path(path).read_text(encoding='utf-8-sig')).get('guards', {}))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        return GuardsCfg()
