"""The post-generation lock of `[heat] public_cap`: a line of hers to him on a channel other
people read must not be plainly explicit, whatever the prompt asked for.

It is the third of three locks (the resolver caps the tier, the loader refuses the private-only
files, and this reads the generated line), so one bug cannot undo the cap. A hit is asked again
once with a hint and never sent; an answer he is owed falls back to a clean generation, which
is screened the same way.

The built-in list is short and holds only words romance.py's own his-heat pattern already
names. The owner can add his own in prompts/heat/public_block.txt, one word or phrase a line."""
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
