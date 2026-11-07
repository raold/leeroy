"""Which rules a member's profile photos follow (owner, 2026-11-07). A woman whose romance stance
toward the owner is lust, aged SUGGESTIVE_MIN_AGE or more, and whose looks code nobody as young
has the lust style: a flattering, flirtatious photo, still safe for work. Everyone else has the
plain one, the rules as they were.

The photo-kind pass (kind.py) chooses and screens a kind by it, the prompt (prompt.py) renders a
kind the lust rules composed only while she still has it, and later photo kinds ask it too. The
NSFW gate (`[selfies] nsfw_max`) is the same for both styles and for every image."""
import re

from ..personas.schema import Persona
from ..personas.stances import is_woman

PLAIN = "plain"
LUST = "lust"
STYLES = (PLAIN, LUST)
SUGGESTIVE_MIN_AGE = 21  # adults only stays; the suggestive style also never looks under 21
# Adults only, for both styles (controller rulings, rounds 3 to 5): nothing that codes a person
# as young. The kind pass refuses it in every free-text field (kind.screen_kind) and on a
# composed lust kind, and looks that use it keep a woman out of the lust style (suggestive), so
# a lust kind is never composed or rendered for her. A rare over-refusal (a teacher's school,
# braces for trousers) is the accepted cost.
AGE_RE = re.compile(
    r"\b(schools?|schoolgirls?|schoolboys?|schoolkids?|high-?schoolers?|school uniforms?|"
    r"student uniforms?|teens?|teenage|teenaged|teenagers?|teeny|teenyboppers?|pre-?teens?|"
    r"tweens?|youthful|childlike|childish|child|children|kids?|kiddies?|young girls?|"
    r"young[- ]looking|barely legal|lolita|loli|under-?aged?|adolescents?|adolescence|"
    r"jail ?bait|baby[- ]?faced?|babyface|brace-?face|little girls?|cheerleaders?|"
    r"cheerleader uniforms?|pig ?tails?|braces)\b", re.I)


def looks_age_coded(p: Persona | None) -> bool:
    """Her looks' free text (hair colour and style, skin, eyes, build, face details) uses a word
    of AGE_RE."""
    a = p.appearance if p is not None else None
    return a is not None and any(AGE_RE.search(t or "") for t in (
        a.hair_colour, a.hair_style, a.skin_tone, a.eye_colour, a.build, *a.face))


def suggestive(p: Persona | None) -> bool:
    """A woman whose stance toward the owner is lust, SUGGESTIVE_MIN_AGE or older, whose looks
    code nobody as young."""
    return (p is not None and is_woman(p) and p.romance.stance == "lust"
            and p.identity.age >= SUGGESTIVE_MIN_AGE and not looks_age_coded(p))


def style_of(p: Persona | None) -> str:
    return LUST if suggestive(p) else PLAIN
