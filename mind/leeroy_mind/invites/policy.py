"""Whether she goes along with his invitation: pure, from what the mind knows of her now. The draw
is seeded by his line and her guid, so one line always decides the same way.

She says yes more readily the better she likes him (affinity toward him), a little more when she
is fond of him, drawn to him or in lust; never when she dislikes him (affinity -30 or below).
With [life] availability above 0 her day weighs in: at work or busy at home she mostly can't
(GLANCING), asleep, out or driving she can't (NONE), stepped away mid-conversation she can't.
The mind's own fences come first (`blocked`): she or he inside an instance or a battleground, she
AFK, an invitation of his to her still being carried out, the guild's hourly moves spent.

Limits (owner, 2026-11-20): at most MAX_MEMBERS members per invitation, MOVES_PER_HOUR members
moved an hour across the guild, one invitation pending per member."""
from dataclasses import dataclass
import random

from leeroy_mind.life.presence import Attention

MAX_MEMBERS = 2
MOVES_PER_HOUR = 6
HOUR_S = 3600.0
PENDING_S = 90.0  # an invitation she is carrying out holds her this long at most
DISLIKE = -30.0

# yes, by her attention in game ([life] availability above 0; None: always there)
_ATTENTION = {Attention.FULL: 1.0, Attention.HALF: 0.85, Attention.GLANCING: 0.3,
              Attention.NONE: 0.0}


@dataclass(frozen=True, slots=True)
class Facts:
    affinity: float = 0.0          # toward him, through the owner's lens
    stance: str = "none"           # her romance stance toward him
    attention: int | None = None   # life.presence Attention in game; None at availability 0
    away: bool = False             # she stepped away mid-conversation (life.interrupt)
    blocked: str = ""              # the mind's own fence code, "" for none


def willingness(f: Facts) -> float:
    """The chance she says yes, 0 to 0.98."""
    if f.blocked or f.away or f.affinity <= DISLIKE:
        return 0.0
    base = 0.55 if f.affinity < 0 else 0.85 if f.affinity < 20 else 0.95
    if f.stance in ("drawn", "lust"):
        base += 0.05
    elif f.stance == "fond":
        base += 0.03
    if f.attention is not None:
        base *= _ATTENTION.get(Attention(f.attention), 1.0)
    return max(0.0, min(0.98, base))


def reason(f: Facts) -> str:
    """Why she says no: the fence, her day, her feelings, or simply not now."""
    if f.blocked:
        return f.blocked
    if f.away or f.attention == Attention.NONE:
        return "away"
    if f.attention == Attention.GLANCING:
        return "busy"
    if f.affinity <= DISLIKE:
        return "dislike"
    return "not_now"


def seeded(line: int, guid: int) -> random.Random:
    return random.Random(f"invite:{line}:{guid}")


def decide(f: Facts, *, line: int, guid: int) -> tuple[bool, str]:
    """(yes, "") or (no, reason), the same for the same line and member."""
    if seeded(line, guid).random() < willingness(f):
        return True, ""
    return False, reason(f)


def how_many(line: int) -> int:
    """How many members an open invitation ("anyone up for ...") takes: one, or two."""
    return 1 if random.Random(f"invite:{line}:n").random() < 0.6 else 2


def moves_left(moves: list[float], now: float) -> int:
    """Moves the guild may still make this hour, from the times of the last ones."""
    return max(0, MOVES_PER_HOUR - sum(1 for t in moves if now - t < HOUR_S))
