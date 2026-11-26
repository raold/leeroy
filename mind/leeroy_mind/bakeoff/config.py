"""[bakeoff] in mind.toml (the capture switch, read once at the mind's start) and the run's own
candidate file (`bakeoff run --config`). A missing [bakeoff] key means capture on; an invalid or
unknown one, or an unreadable file, means capture off; a candidate file with a mistake is refused
with the key named, never its value."""
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

VARIANTS = ("as_is", "situation")
RESERVED = frozenset({"sent", "first", "live"})  # the run's own baselines
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,39}")
TIMEOUT_RANGE = (10, 900)
THINK_BUDGET_MAX = 4096
DEFAULT_THINK_BUDGET = 1400  # bake-off 1: about what a thinking reply needs before its line
DEFAULT_TIMEOUT = 120
NUM_CTX_RANGE = (2048, 32768)
CTX_STEP = 1024


@dataclass(frozen=True)
class BakeoffCfg:
    capture: bool = True


OFF = BakeoffCfg(capture=False)


def load(path: Path) -> BakeoffCfg:
    try:
        table = tomllib.loads(Path(path).read_text(encoding="utf-8-sig")).get("bakeoff", {})
    except FileNotFoundError:
        return BakeoffCfg()
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        return OFF
    if not isinstance(table, dict) or set(table) - {"capture"}:
        return OFF
    capture = table.get("capture", True)
    return BakeoffCfg(capture) if type(capture) is bool else OFF


@dataclass(frozen=True)
class Candidate:
    """`think`: True or False is sent as the request's flag (False when the key is left out, as
    the live pipeline sends it); None (`think = "default"`) sends no flag, so the model does what
    it does by default (Orion thinks only then, and answers HTTP 400 to think: true). A candidate
    whose think is not False is a thinking one: no captured stop (it would fire inside the
    thinking; the content is cut at its first stop afterwards), num_predict raised by
    think_budget and num_ctx raised to fit (or `num_ctx`)."""
    name: str
    model: str
    think: bool | None = False
    think_budget: int = 0
    prompt_variant: str = "as_is"
    timeout: int = DEFAULT_TIMEOUT
    num_ctx: int | None = None
    beat: bool = False  # ask its own beat first (talk/beat.py; bakeoff/beat.py)

    @property
    def thinks(self) -> bool:
        return self.think is not False

    def context(self, captured: int) -> int:
        """The request's num_ctx: the candidate's own, else for a thinking candidate the captured
        one plus its budget rounded up to CTX_STEP (4096 + 1400: 6144), else the captured one."""
        if self.num_ctx is not None:
            return self.num_ctx
        if not self.thinks:
            return captured
        return -(-(captured + self.think_budget) // CTX_STEP) * CTX_STEP

    def describe(self) -> dict:
        return {"name": self.name, "model": self.model, "think": self.think,
                "think_budget": self.think_budget, "prompt_variant": self.prompt_variant,
                "timeout": self.timeout, "num_ctx": self.num_ctx, "beat": self.beat}


class CandidateError(ValueError):
    pass


_KEYS = {"name", "model", "think", "think_budget", "prompt_variant", "timeout", "num_ctx", "beat"}


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
                             f"and not {', '.join(sorted(RESERVED))}")
    if not isinstance(model, str) or not model.strip() or len(model) > 200:
        raise CandidateError(f"{where}: model must be a model name")
    think = raw.get("think", False)
    if think == "default":
        think = None  # send no flag: the model's own default
    elif type(think) is not bool:
        raise CandidateError(f'{where}: think must be true, false (the default) or "default" '
                             "(no flag: the model's own default)")
    thinks = think is not False
    budget = raw.get("think_budget", DEFAULT_THINK_BUDGET if thinks else 0)
    if type(budget) is not int or not 0 <= budget <= THINK_BUDGET_MAX:
        raise CandidateError(f"{where}: think_budget must be 0-{THINK_BUDGET_MAX}")
    variant = raw.get("prompt_variant", "as_is")
    if variant not in VARIANTS:
        raise CandidateError(f"{where}: prompt_variant must be one of {', '.join(VARIANTS)}")
    timeout = raw.get("timeout", DEFAULT_TIMEOUT)
    if type(timeout) is not int or not TIMEOUT_RANGE[0] <= timeout <= TIMEOUT_RANGE[1]:
        raise CandidateError(f"{where}: timeout must be {TIMEOUT_RANGE[0]}-{TIMEOUT_RANGE[1]} seconds")
    ctx = raw.get("num_ctx")
    if ctx is not None and (type(ctx) is not int or not NUM_CTX_RANGE[0] <= ctx <= NUM_CTX_RANGE[1]):
        raise CandidateError(f"{where}: num_ctx must be {NUM_CTX_RANGE[0]}-{NUM_CTX_RANGE[1]}")
    beat = raw.get("beat", False)
    if type(beat) is not bool:
        raise CandidateError(f"{where}: beat must be true or false")
    return Candidate(name, model.strip(), think, budget if thinks else 0, variant, timeout, ctx, beat)


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
