"""A line's attitude and its speaker's state as Kokoro's levers (draft section 8): speed,
stress on the word aimed at him, ellipsis pauses and gain.

The values are the listening kit's ATTITUDES (Task 1), to be tuned by ear: speed none 1.0,
fond 1.0, drawn 0.92, lust 0.86. The states are the draft's: tipsy and high x0.95, drunk x0.90
with one more pause, wired x1.05, hungover x0.95 and 2 dB quieter. Raid calls (callout, urgent)
sound the same from everyone: x1.08 like the kit's control lines, with no stress, pause or gain
change, whatever the attitude and the state (P1b's RAID_CALLS types them straight whatever the
night has been).

kokoro_markup writes them into the text for misaki: "[you](+2)" stresses the word (misaki
changes only unstressed or secondary-stressed words, which you, your, you're and me are), and
an ellipsis is a pause with a falling contour."""
import re
from dataclasses import dataclass

from .lexicon import MARKUP
from .wire import ATTITUDES, STATES

RAID_CALLS = frozenset({"callout", "urgent"})  # P1b's TurnKind values
RAID_CALL_SPEED = 1.08
SPEED = {"none": 1.0, "fond": 1.0, "drawn": 0.92, "lust": 0.86}
STRESS = {"none": 0, "fond": 1, "drawn": 2, "lust": 2}
ELLIPSES = {"none": 0, "fond": 0, "drawn": 1, "lust": 2}
STATE_SPEED = {"sober": 1.0, "tipsy": 0.95, "drunk": 0.90, "high": 0.95, "wired": 1.05,
               "hungover": 0.95}
STATE_GAIN_DB = {"hungover": -2.0}
DRUNK_ELLIPSES = 1

_AIMED = re.compile(r"(?<![\w'\u2019-])(?:you['\u2019]re|your|you|me)(?![\w'\u2019-])",
                    re.IGNORECASE)
_COMMA = re.compile(r"(?<!\d),|,(?!\d)")  # not a thousands separator (1,000)


@dataclass(frozen=True, slots=True)
class Levers:
    speed_mult: float  # times the voice's own speed
    stress: int  # misaki's +n on the first word aimed at him; 0 for none
    ellipses: int  # how many commas become pauses
    gain_db: float


RAID_CALL_LEVERS = Levers(RAID_CALL_SPEED, 0, 0, 0.0)


def levers_for(attitude: str, state: str, kind: str) -> Levers:
    """The levers for a speak message's attitude, state and kind (the mind's turn kind); a raid
    call takes RAID_CALL_LEVERS whatever its attitude and state."""
    if attitude not in ATTITUDES:
        raise ValueError(f"attitude: expected one of {', '.join(ATTITUDES)}, got {attitude!r}")
    if state not in STATES:
        raise ValueError(f"state: expected one of {', '.join(STATES)}, got {state!r}")
    if kind in RAID_CALLS:
        return RAID_CALL_LEVERS
    speed = SPEED[attitude] * STATE_SPEED[state]
    pauses = ELLIPSES[attitude] + (DRUNK_ELLIPSES if state == "drunk" else 0)
    return Levers(round(speed, 4), STRESS[attitude], pauses, STATE_GAIN_DB.get(state, 0.0))


def kokoro_markup(text: str, levers: Levers, *, to_human: bool) -> str:
    """`levers.ellipses` commas become "...": the one after an opening interjection ("So, I")
    comes first in the line, then the next ones. With `to_human`, the first you, your, you're
    or me takes misaki's stress mark. Markup already there (the lexicon's) is left alone."""
    spans = [m.span() for m in MARKUP.finditer(text)]

    def free(i: int) -> bool:
        return not any(a <= i < b for a, b in spans)
    edits = [(m.start(), m.end(), "...") for m in _COMMA.finditer(text) if free(m.start())]
    edits = edits[:levers.ellipses]
    if to_human and levers.stress:
        word = next((m for m in _AIMED.finditer(text) if free(m.start())), None)
        if word is not None:
            edits.append((word.start(), word.end(), f"[{word.group()}](+{levers.stress})"))
    for a, b, new in sorted(edits, reverse=True):
        text = text[:a] + new + text[b:]
    return text
