"""Which rules a member's profile photos follow (owner, 2026-11-07). A woman whose romance stance
toward the owner is lust, aged SUGGESTIVE_MIN_AGE or more, has the lust style: a flattering,
flirtatious photo, still safe for work. Everyone else has the plain one, the rules as they were.

The photo-kind pass (kind.py) chooses and screens a kind by it, the prompt (prompt.py) takes its
negatives from it when it renders, and later photo kinds ask it too. The NSFW gate (`[selfies]
nsfw_max`) is the same for both styles and for every image."""
from ..personas.schema import Persona
from ..personas.stances import is_woman

PLAIN = "plain"
LUST = "lust"
STYLES = (PLAIN, LUST)
SUGGESTIVE_MIN_AGE = 21  # adults only stays; the suggestive style also never looks under 21


def suggestive(p: Persona | None) -> bool:
    """A woman whose stance toward the owner is lust, SUGGESTIVE_MIN_AGE or older."""
    return (p is not None and is_woman(p) and p.romance.stance == "lust"
            and p.identity.age >= SUGGESTIVE_MIN_AGE)


def style_of(p: Persona | None) -> str:
    return LUST if suggestive(p) else PLAIN
