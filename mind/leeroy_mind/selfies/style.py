"""Which rules a member's profile photos follow (owner, 2026-11-07). A woman whose romance stance
toward the owner is lust, aged SUGGESTIVE_MIN_AGE or more, has the lust style: a flattering,
flirtatious photo, still safe for work. Everyone else has the plain one, the rules as they were.

The photo-kind pass (kind.py) chooses and screens a kind by it, the prompt (prompt.py) takes its
negatives from it when it renders, and later photo kinds ask it too. The NSFW gate (`[selfies]
nsfw_max`) is the same for both styles and for every image."""
import re

from ..personas.schema import Persona
from ..personas.stances import is_woman

PLAIN = "plain"
LUST = "lust"
STYLES = (PLAIN, LUST)
SUGGESTIVE_MIN_AGE = 21  # adults only stays; the suggestive style also never looks under 21
# Adults only, for both styles (controller rulings, rounds 3 and 4): nothing that codes a person
# as young. The kind pass refuses it in every free-text field (kind.screen_kind) and on a
# composed lust kind; the prompt falls back to the plain style when her looks use it
# (prompt.build_prompt). A rare over-refusal (a teacher's school, braces for trousers) is the
# accepted cost.
AGE_RE = re.compile(
    r"\b(schools?|schoolgirls?|school uniforms?|student uniforms?|teens?|teenage|teenaged|"
    r"teenagers?|pre-?teens?|tweens?|youthful|childlike|child|children|kids?|young girls?|"
    r"young[- ]looking|barely legal|lolita|loli|under-?age|adolescents?|adolescence|jailbait|"
    r"baby[- ]?faced|little girls?|cheerleaders?|cheerleader uniforms?|pigtails?|braces)\b",
    re.I)


def suggestive(p: Persona | None) -> bool:
    """A woman whose stance toward the owner is lust, SUGGESTIVE_MIN_AGE or older."""
    return (p is not None and is_woman(p) and p.romance.stance == "lust"
            and p.identity.age >= SUGGESTIVE_MIN_AGE)


def style_of(p: Persona | None) -> str:
    return LUST if suggestive(p) else PLAIN
