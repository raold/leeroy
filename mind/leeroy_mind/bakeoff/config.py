"""[bakeoff] in mind.toml (the capture switch, read once at the mind's start) and the run's own
candidate file (`bakeoff run --config`). A missing, invalid or unknown [bakeoff] key means capture
off; a candidate file with a mistake is refused with the key named, never its value."""
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

VARIANTS = ("as_is", "situation")
RESERVED = frozenset({"sent", "live"})  # the run's own baselines
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,39}")
TIMEOUT_RANGE = (10, 900)
THINK_BUDGET_MAX = 2048
DEFAULT_THINK_BUDGET = 160  # the addendum's starting budget (section 7)
DEFAULT_TIMEOUT = 120


@dataclass(frozen=True)
class BakeoffCfg:
    capture: bool = False


def load(path: Path) -> BakeoffCfg:
    try:
        table = tomllib.loads(Path(path).read_text(encoding="utf-8-sig")).get("bakeoff", {})
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        return BakeoffCfg()
    if not isinstance(table, dict) or set(table) - {"capture"}:
        return BakeoffCfg()
    capture = table.get("capture", False)
    return BakeoffCfg(capture) if type(capture) is bool else BakeoffCfg()


@dataclass(frozen=True)
class Candidate:
    name: str
    model: str
    think: bool = False
    think_budget: int = 0
    prompt_variant: str = "as_is"
    timeout: int = DEFAULT_TIMEOUT

    def describe(self) -> dict:
        return {"name": self.name, "model": self.model, "think": self.think,
                "think_budget": self.think_budget, "prompt_variant": self.prompt_variant,
                "timeout": self.timeout}


class CandidateError(ValueError):
    pass


_KEYS = {"name", "model", "think", "think_budget", "prompt_variant", "timeout"}


def _candidate(i: int, raw) -> Candidate:
    where = f"candidate {i + 1}"
    if not isinstance(raw, dict):
        raise CandidateError(f"{where}: expected a table")
    unknown = sorted(set(raw) - _KEYS)
    if unknown:
        raise CandidateError(f"{where}: unknown key {unknown[0]}")
    name, model = raw.get("name"), raw.get("model")
    if not isinstance(name, str) or not NAME.fullmatch(name) or name.casefold() in RESERVED:
        raise CandidateError(f"{where}: name must be 1-40 letters, digits, '.', '_' or '-' "
                             f"and not {' or '.join(sorted(RESERVED))}")
    if not isinstance(model, str) or not model.strip() or len(model) > 200:
        raise CandidateError(f"{where}: model must be a model name")
    think = raw.get("think", False)
    if type(think) is not bool:
        raise CandidateError(f"{where}: think must be true or false")
    budget = raw.get("think_budget", DEFAULT_THINK_BUDGET if think else 0)
    if type(budget) is not int or not 0 <= budget <= THINK_BUDGET_MAX:
        raise CandidateError(f"{where}: think_budget must be 0-{THINK_BUDGET_MAX}")
    variant = raw.get("prompt_variant", "as_is")
    if variant not in VARIANTS:
        raise CandidateError(f"{where}: prompt_variant must be one of {', '.join(VARIANTS)}")
    timeout = raw.get("timeout", DEFAULT_TIMEOUT)
    if type(timeout) is not int or not TIMEOUT_RANGE[0] <= timeout <= TIMEOUT_RANGE[1]:
        raise CandidateError(f"{where}: timeout must be {TIMEOUT_RANGE[0]}-{TIMEOUT_RANGE[1]} seconds")
    return Candidate(name, model.strip(), think, budget if think else 0, variant, timeout)


def load_candidates(path: Path) -> list[Candidate]:
    """The candidates of a bake-off file: `[[candidate]]` tables, at least one, unique names."""
    try:
        data = tomllib.loads(Path(path).read_text(encoding="utf-8-sig"))
    except OSError as e:
        raise CandidateError(f"cannot read the config ({type(e).__name__})") from None
    except (UnicodeError, tomllib.TOMLDecodeError) as e:
        raise CandidateError(f"the config is not valid TOML ({type(e).__name__})") from None
    unknown = sorted(set(data) - {"candidate"})
    if unknown:
        raise CandidateError(f"unknown table or key {unknown[0]} (only [[candidate]])")
    raw = data.get("candidate")
    if not isinstance(raw, list) or not raw:
        raise CandidateError("no [[candidate]] tables")
    out = [_candidate(i, c) for i, c in enumerate(raw)]
    names = [c.name.casefold() for c in out]
    if len(set(names)) != len(names):
        raise CandidateError("two candidates share a name")
    return out
