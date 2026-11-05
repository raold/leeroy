"""Tuning lines: what a wording dial adds to the speaker's rules block when it sits two or more
from the persona as written (effective.band). The rules block is the same on every turn for a
speaker (talk.prompt.comms_rules), so a changed dial costs one prompt-cache miss and no reload.

The built-in lines are fixed, generic and non-explicit at every setting. The owner may replace
one with a local overlay, mind-data/prompts/tuning_<dial>_<band>.txt, read through
prompts.load_prompt and capped like the romance overlays. The console never authors prompt
text: dials are numbers only."""
import logging
from pathlib import Path

from leeroy_mind.prompts import load_prompt
from leeroy_mind.text import estimate_tokens, truncate_words

from .dials import MEMBER_DIALS
from .effective import band, base_of, dials_of

log = logging.getLogger("leeroy_mind.tuning")

OVERLAY_CAP_TOKENS = 200  # the romance overlays' cap (talk.romance.OVERLAY_CAP_TOKENS)

BUILTIN_LINES: dict[tuple[str, str], str] = {
    ("argumentative", "high"): ("You argue your corner: when you disagree you say so, and you "
                                "don't back down easily."),
    ("argumentative", "low"): ("You're easygoing: you let most things go and rarely pick an "
                               "argument."),
    ("humour", "high"): "You joke a lot: you look for the funny side of most things.",
    ("humour", "low"): "You rarely joke: you mostly keep it straight.",
    ("theorycraft", "high"): ("You love the numbers side of the game: stats, gear and rotations "
                              "come up whenever they fit."),
    ("theorycraft", "low"): ("The numbers side of the game bores you: you leave stats and "
                             "rotations to others."),
    ("lore", "high"): ("You love the story of the world: you bring up its lore whenever it "
                       "fits."),
    ("lore", "low"): "The game's story doesn't interest you much: you skip the lore.",
}
OVERLAY_NAMES = tuple(f"tuning_{d}_{b}" for d, b in BUILTIN_LINES)
_warned: set[str] = set()


def _capped(text: str, name: str, data_dir: Path) -> str:
    if estimate_tokens(text) <= OVERLAY_CAP_TOKENS:
        return text
    key = f"cap:{name}:{Path(data_dir).resolve()}"
    if key not in _warned:
        _warned.add(key)
        log.warning("the %s overlay is capped at %d estimated tokens (it has %d)", name,
                    OVERLAY_CAP_TOKENS, estimate_tokens(text))
    return truncate_words(text, OVERLAY_CAP_TOKENS * 4)


def line_for(dial: str, which: str, data_dir: Path | None = None) -> str:
    builtin = BUILTIN_LINES[(dial, which)]
    if data_dir is None:
        return builtin
    name = f"tuning_{dial}_{which}"
    return " ".join(_capped(load_prompt(name, data_dir, builtin), name, data_dir).split())


def tuning_lines(p, data_dir: Path | None = None) -> list[str]:
    """The persona's tuning lines, in the dials' order ([] for a stored persona and for one
    whose wording dials all sit near their own value). `data_dir` None: the built-in lines
    only (the critic's and the evaluation's reading of who the member is)."""
    mt = dials_of(p)
    if not mt.values:
        return []
    base = base_of(p)
    out = []
    for dial, d in MEMBER_DIALS.items():
        if d.wording and (which := band(dial, base, mt)) is not None:
            out.append(line_for(dial, which, data_dir))
    return out


def tuned_card(card: str, p) -> str:
    """A card with the member's tuning lines after it (built-in wording), for the readers that
    judge who the member is (critic, evaluation, core judge, the night's scenes). The card
    itself when there are none."""
    lines = tuning_lines(p)
    return card if not lines else card + "\n" + " ".join(lines)
