"""[lab] in mind.toml, read once at the mind's start (and by `lab run`). Missing, invalid or
unknown keys mean the defaults with the lab off; nothing private reaches a diagnostic.

- enabled: the night's lab stages (lab/night.py); default false.
- minutes: the hard budget of one night's lab, propose, battery and grading together (5-180,
  default 40).
- pairs: the battery's (script, member) pairs a night, each run once per arm (2-60, default 12).
- judge: pairs per arm the blind judge reads (0-60, default 6; never more than pairs).
- variants: prompt variants proposed a night (0-5, default 3; 0 only measures).
- overlays: a sandbox mind reads `prompts/lab_<piece>.txt` (talk/pieces.py). Set only in a
  sandbox's copied mind.toml (sandbox/prepare.QUIET); the live mind leaves it off."""
import tomllib
from dataclasses import dataclass
from pathlib import Path

KEYS = frozenset({"enabled", "minutes", "pairs", "judge", "variants", "overlays"})


@dataclass(frozen=True)
class LabCfg:
    enabled: bool = False
    minutes: int = 40
    pairs: int = 12
    judge: int = 6
    variants: int = 3
    overlays: bool = False


def _int(value, low, high):
    return type(value) is int and low <= value <= high


def parse(table) -> LabCfg:
    if not isinstance(table, dict) or set(table) - KEYS:
        return LabCfg()
    d = LabCfg()
    enabled = table.get("enabled", d.enabled)
    overlays = table.get("overlays", d.overlays)
    minutes = table.get("minutes", d.minutes)
    pairs = table.get("pairs", d.pairs)
    judge = table.get("judge", d.judge)
    variants = table.get("variants", d.variants)
    if (type(enabled) is not bool or type(overlays) is not bool or not _int(minutes, 5, 180)
            or not _int(pairs, 2, 60) or not _int(judge, 0, 60) or not _int(variants, 0, 5)):
        return LabCfg()
    return LabCfg(enabled, minutes, pairs, min(judge, pairs), variants, overlays)


def load(path: Path) -> LabCfg:
    try:
        doc = tomllib.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        return LabCfg()
    return parse(doc.get("lab", {}))
