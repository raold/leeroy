"""What a speaking turn is. A turn is at most 2 typed lines (3 for a bit); the loop guards
count turns, not lines."""
from dataclasses import dataclass
from enum import StrEnum

from leeroy_mind.llm.scheduler import Priority


class TurnKind(StrEnum):
    REPLY_HUMAN = "reply_human"
    CONTINUE = "continue"
    OPEN = "open"
    GREET = "greet"
    GZ = "gz"
    KILL = "kill"
    WIPE_REACT = "wipe_react"
    CALLOUT = "callout"
    URGENT = "urgent"
    BIT = "bit"
    ASIDE = "aside"
    FLIRT = "flirt"
    WHISPER = "whisper"
    GOODNIGHT = "goodnight"


@dataclass(slots=True)
class Turn:
    kind: TurnKind
    speaker: int
    channel: str
    to_guid: int = 0
    addressee: int | None = None
    instruction: str = ""
    deadline: float = 0.0
    priority: Priority = Priority.SOCIAL
    trigger_line: int | None = None
    bit: object | None = None
    max_lines: int = 2
    delay: float = 0.0
    template: str | None = None
