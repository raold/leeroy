"""The post-generation lock of `[heat] public_cap`: a line of hers to him on a channel other
people read must not be plainly explicit, whatever the prompt asked for.

It is the third of three locks (the resolver caps the tier, the loader refuses the private-only
files, and this reads the generated line), so one bug cannot undo the cap. A hit is asked again
once with a hint and never sent; an answer he is owed falls back to a clean generation, which
is screened the same way.

The built-in list is short and holds only words romance.py's own his-heat pattern already
names. The owner can add his own in prompts/heat/public_block.txt, one word or phrase a line.

The voice rule (a line typed instead of voiced) has its own, broader screen here (voice_hit,
VOICE_RE plus everything public_hit catches); it never reaches the public cap."""
import re
from pathlib import Path

from . import overlays

BUILTIN_RE = re.compile(r"\b(?:horny|naked|nude|sex|dick|cock|cum)\b", re.IGNORECASE)
TELL = "heat public"
HINT = ("Other people can read this: keep it playful and flirty at most, with nothing explicit "
        "and nothing you'd only say in private.")
OWED = "other people can read this, so nothing explicit"


def public_hit(text: str, data_dir: Path) -> bool:
    """`text` holds a plainly explicit word (BUILTIN_RE) or one of the owner's block words."""
    if not text:
        return False
    if BUILTIN_RE.search(text):
        return True
    folded = " ".join(text.lower().split())
    return any(re.search(rf"(?<!\w){re.escape(w)}(?!\w)", folded)
               for w in overlays.block_words(data_dir))


# The voice rule's own screen (ruling 5): below heat 10 a line that is itself explicit is typed,
# not voiced. Broader than the public guard's short list, and kept apart from it, so that
# `public_cap` (which refuses a public line outright) is never widened by it: plainly sexual
# words and phrases about bodies, undressing and sex, not flirting, kissing or missing him.
VOICE_RE = re.compile(
    r"\b(?:sexual(?:ly)?|orgasm\w*|masturbat\w*|penetrat\w*|genitals?|erection\w*|aroused"
    r"|arousal|nipples?|boobs|tits|pussy|clit\w*|blowjobs?|lingerie|panties|thong|nudes"
    r"|naughty pics?|undress\w*|strip(?:ping|ped)? (?:off|for (?:you|u|me))"
    r"|(?:take|took|taking) (?:my|your|ur|her|his) clothes off|clothes off"
    r"|wet for (?:you|u|me)|in bed with (?:you|u|me)|bend me over"
    r"|moaning (?:your|ur) name)\b",
    re.IGNORECASE)


def voice_hit(text: str, data_dir: Path) -> bool:
    """The voice rule's screen: `text` holds a word of VOICE_RE, or anything the public guard
    catches (its list and the owner's public_block.txt). Used only to type a voice note."""
    if not text:
        return False
    return bool(VOICE_RE.search(text)) or public_hit(text, data_dir)
