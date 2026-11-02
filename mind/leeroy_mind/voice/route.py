"""A line's tier and expiry, and the speak message that carries it (the protocol's tiers, I1)."""
from leeroy_mind.talk.turns import Turn, TurnKind
from leeroy_mind.voice import wire
from leeroy_mind.voice.rooms import PRIVATE, RAID_ROOMS

EXPIRY_MS = {"p0": 3000, "p1": 3000, "p2": 5000, "p3": 20000, "p4": 15000}
WHISPER_MS = 30000
ASIDE_MS = 8000
HOTMIC = "hotmic"  # meta.why of a hot-mic moment, and its speak.kind (Task 20)
_RAID_CALLS = frozenset({TurnKind.CALLOUT, TurnKind.REPLY_HUMAN, TurnKind.KILL,
                         TurnKind.WIPE_REACT})


def tier_of(turn: Turn, *, room: str, lead: int | None, officer: bool, in_raid: bool,
            human: int) -> str:
    """`room`: a room id, or "whisper". `officer`: the speaker is one. `in_raid`: the speaker is
    in a formed raid. URGENT is p1 for anyone, the lead included; a p0 is the lead's CALLOUT in
    a raid room, and with no lead there is none."""
    if turn.kind is TurnKind.URGENT:
        return "p1"
    is_lead = lead is not None and turn.speaker == lead
    if is_lead and turn.kind is TurnKind.CALLOUT and room in RAID_ROOMS:
        return "p0"
    if in_raid and (is_lead or officer) and turn.kind in _RAID_CALLS:
        return "p2"
    if room == "whisper" or room in PRIVATE or _to_him(turn, human):
        return "p3"
    return "p4"


def _to_him(turn: Turn, human: int) -> bool:
    return bool(human) and human in (turn.addressee, turn.to_guid)


def _expiry_ms(turn: Turn, room: str, tier: str) -> int:
    if tier == "p3" and room == "whisper":
        return WHISPER_MS
    if tier == "p4" and turn.kind is TurnKind.ASIDE:
        return ASIDE_MS
    return EXPIRY_MS[tier]


def speak_for(turn: Turn, meta, text: str, *, speak_id: str, room: str, tier: str, attitude: str,
              state: str, after: str, now: float, human: int = 0) -> wire.Speak:
    """The one speak of a turn (settled item 8). `text` is what he will hear, passed apart from
    what the chat log keeps; `room` a room id or "whisper"; `now` in seconds, as the clock
    gives it. `template`: no model wrote it, so the service never holds it while he talks."""
    return wire.Speak(speak_id, turn.speaker, text, room, _to_him(turn, human), tier,
                      HOTMIC if meta.why == HOTMIC else turn.kind.value, attitude, state,
                      int(now * 1000) + _expiry_ms(turn, room, tier), after,
                      turn.template is not None)
