"""[lab] in mind.toml, read once at the mind's start (and by `lab run`). Missing keys mean the
defaults (the lab on); invalid or unknown keys, or an unreadable file, mean the defaults with the
lab off (OFF); nothing private reaches a diagnostic.

- enabled: the night's lab stages (lab/night.py); default true.
- minutes: the hard budget of one night's lab, propose, battery and grading together (5-180,
  default 40).
- pairs: the battery's (script, member) pairs a night, each run once per arm (2-60, default 12).
- judge: pairs per arm the blind judge reads (0-60, default 6; never more than pairs).
- variants: prompt variants proposed a night (0-5, default 3; 0 only measures).
- grader: what grades her answers: "rubric" (lab/grade.py's 0-3 rubric on the reflection model;
  it does not agree with a strong grader at all) or "tells" (lab/judge.py, on the live model:
  it agrees better, but not yet well enough to be the default; lab/README.md, "The judge").
  Default "rubric".
- promote: a variant that wins becomes an arm of the learn bandit (lab/promote.py). Default
  false (owner ruling 2026-11-25: stays off): the lab measures and reports, and proposes nothing
  to the weekly gate, until its judge agrees with a strong grader (lab/README.md, "The judge"). Only with grader "tells":
  `promote = true` with the rubric is refused like any bad value (the table reads as off), and
  load() logs `lab_config_refused reason=promote_with_rubric` once a process.
- overlays: a sandbox mind reads `prompts/lab_<piece>.txt` (talk/pieces.py). Set only in a
  sandbox's copied mind.toml (sandbox/prepare.QUIET); the live mind leaves it off."""
import logging
import tomllib
from dataclasses import dataclass
from pathlib import Path

from leeroy_mind.logs import log

KEYS = frozenset({"enabled", "minutes", "pairs", "judge", "variants", "overlays", "grader",
                  "promote"})
GRADERS = ("rubric", "tells")
PROMOTE_WITH_RUBRIC = "promote_with_rubric"
_logged: set[str] = set()   # refusals logged by this process


@dataclass(frozen=True)
class LabCfg:
    enabled: bool = True
    minutes: int = 40
    pairs: int = 12
    judge: int = 6
    variants: int = 3
    overlays: bool = False
    grader: str = "rubric"
    promote: bool = False


OFF = LabCfg(enabled=False)


def _int(value, low, high):
    return type(value) is int and low <= value <= high


def refusal(table) -> str | None:
    """Why a [lab] table reads as off although each value is of its kind: `promote = true` with
    the rubric grader (it does not agree with a strong grader, so nothing it ranks is proposed)."""
    if isinstance(table, dict) and table.get("promote") is True and table.get("grader", "rubric") == "rubric":
        return PROMOTE_WITH_RUBRIC
    return None


def parse(table) -> LabCfg:
    """The table as the lab reads it: OFF when it is not valid (`valid`)."""
    return _parse(table) or OFF


def valid(table) -> bool:
    return _parse(table) is not None


def _parse(table) -> LabCfg | None:
    if not isinstance(table, dict) or set(table) - KEYS or refusal(table) is not None:
        return None
    d = LabCfg()
    enabled = table.get("enabled", d.enabled)
    overlays = table.get("overlays", d.overlays)
    minutes = table.get("minutes", d.minutes)
    pairs = table.get("pairs", d.pairs)
    judge = table.get("judge", d.judge)
    variants = table.get("variants", d.variants)
    grader = table.get("grader", d.grader)
    promote = table.get("promote", d.promote)
    if type(promote) is not bool or grader not in GRADERS:
        return None
    if (type(enabled) is not bool or type(overlays) is not bool or not _int(minutes, 5, 180)
            or not _int(pairs, 2, 60) or not _int(judge, 0, 60) or not _int(variants, 0, 5)):
        return None
    return LabCfg(enabled, minutes, pairs, min(judge, pairs), variants, overlays, grader, promote)


def load(path: Path) -> LabCfg:
    try:
        doc = tomllib.loads(Path(path).read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return LabCfg()
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        return OFF
    table = doc.get("lab", {})
    reason = refusal(table)
    if reason is not None and reason not in _logged:
        _logged.add(reason)
        log("lab_config_refused", level=logging.WARNING, reason=reason)
    return parse(table)
