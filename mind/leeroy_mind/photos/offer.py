"""One reserved photo carried by an ordinary Discord turn."""
from dataclasses import dataclass
from typing import Callable

from leeroy_mind.discord.store import AttachmentIn
from leeroy_mind.selfies.snaps import SnapRow
from leeroy_mind.selfies.kind import EXPLICIT_RE, WEAR_RE
from leeroy_mind.selfies.style import AGE_RE


@dataclass
class PhotoOffer:
    snap: SnapRow
    instruction: str
    on_sent: Callable[[int, int], None]
    release: Callable[[], None]
    # SnapRow stores pixel dimensions but not file size/path. The sender verifies the
    # actual file before offering it, then the transport revalidates before committing.
    attachment: AttachmentIn | None = None
    valid: Callable[[], bool] = lambda: True
    reason: str = 'withdrawn'


def caption_ok(text: str) -> bool:
    return not any(pattern.search(text) for pattern in (EXPLICIT_RE, WEAR_RE, AGE_RE))
