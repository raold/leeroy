"""Voice protocol v1, the mind's own copy (stdlib only; the voice service keeps
leeroy_voice.wire with the same names). NDJSON between the mind and leeroy-voice on
127.0.0.1:7879, one JSON object per line, at most MAX_LINE_BYTES before the newline. Every
message carries "v": 1, "type" and "ts" (unix ms), then its fields in declaration order,
encoded with separators (",", ":") and ensure_ascii. A Python field `from_` is JSON `from`.
Unknown fields are ignored. Both suites test their codecs against
voice/tests/fixtures/wire_golden.json: keep the two copies in step."""
import dataclasses
import json
import math
import typing
from dataclasses import dataclass

PROTOCOL_VERSION = 1
MAX_LINE_BYTES = 65536
SERVER_NAME = "leeroy-voice"  # hello.server

# The messages leeroy-voice answers a bad line with (the bridge's words)
BAD_JSON = "bad json"
ANOTHER_MIND = "another mind is connected"
LINE_TOO_LONG = "line too long"
UNSUPPORTED_VERSION = "unsupported version"
UNKNOWN_TYPE = "unknown type: "
BAD_FIELD = "bad field: "

TIERS = ("p0", "p1", "p2", "p3", "p4")  # most urgent first
ATTITUDES = ("none", "fond", "drawn", "lust")
STATES = ("sober", "tipsy", "drunk", "high", "wired", "hungover")
PHASES = ("none", "trash", "combat", "wipe_silence", "wipe_debrief", "kill")
REFUSALS = ("locked", "full", "unknown", "offline")
PLAY_STATES = ("started", "done", "dropped", "cut")
DROP_REASONS = ("expired", "moved", "not_his_channel", "no_voice", "muted", "empty", "raid",
                "after_dropped", "offline", "cancelled", "engine_error", "mind_gone")
MIC_STATES = ("ok", "missing", "unset", "closed")
STT_STATES = ("parakeet", "whisper", "off")


class WireError(ValueError):
    """A line or message that breaks the protocol. `reply` is the error message leeroy-voice
    answers it with: one of the constants above, with the type or the field named."""

    def __init__(self, reply: str, detail: str = ""):
        super().__init__(f"{reply} ({detail})" if detail else reply)
        self.reply = reply


# from the mind


@dataclass(frozen=True, slots=True)
class Session:
    online: bool
    human_guid: int
    live_model: str
    embed_model: str


@dataclass(frozen=True, slots=True)
class Room:
    """One room of the tree in `channels`; not a message of its own."""
    id: str
    name: str
    parent: str  # "" for a top-level room
    locked: bool
    allowed: tuple[int, ...]
    capacity: int  # 0: unlimited


@dataclass(frozen=True, slots=True)
class Channels:
    channels: tuple[Room, ...]


@dataclass(frozen=True, slots=True)
class Moved:
    guid: int
    channel: str  # "": off comms
    by: int = 0  # 0: the rules


@dataclass(frozen=True, slots=True)
class Invite:
    from_: int
    channel: str


@dataclass(frozen=True, slots=True)
class Refused:
    channel: str
    reason: str


@dataclass(frozen=True, slots=True)
class VoiceDef:
    """Type "voice": a member's voice spec (Task 4's VoiceSpec, as its JSON object)."""
    guid: int
    name: str
    spec: dict


@dataclass(frozen=True, slots=True)
class Speak:
    id: str
    guid: int
    text: str
    channel: str  # a room id, or "whisper"
    to_human: bool
    priority: str
    kind: str
    attitude: str
    state: str
    expires_ms: int  # absolute unix ms
    after: str = ""  # the id of the line this one answers; "" for none
    template: bool = False  # composed without the model: never held while he talks (R1)


@dataclass(frozen=True, slots=True)
class Cancel:
    """Exactly one of the three is set; `below` is a tier: it and every less urgent one."""
    id: str = ""
    guid: int = 0
    below: str = ""


@dataclass(frozen=True, slots=True)
class Raid:
    phase: str
    lead: int


# from the voice service


@dataclass(frozen=True, slots=True)
class Hello:
    server: str
    version: str
    engines: tuple[str, ...]
    input_device: str
    output_device: str
    ptt_key: str


@dataclass(frozen=True, slots=True)
class Join:
    channel: str


@dataclass(frozen=True, slots=True)
class Played:
    id: str
    state: str
    reason: str = ""


@dataclass(frozen=True, slots=True)
class Ptt:
    down: bool
    channel: str


@dataclass(frozen=True, slots=True)
class Heard:
    text: str
    raw: str
    confidence: float
    channel: str
    speech_ms: int
    latency_ms: int


@dataclass(frozen=True, slots=True)
class Status:
    ok: bool  # the output is open and the engine ready; the mic is reported apart (M22)
    mic: str
    gpu: str  # "none", or the model holding VRAM
    stt: str
    ui: bool  # the voice UI has a page open
    queue: int
    errors: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Error:
    message: str


Message = (Session | Channels | Moved | Invite | Refused | VoiceDef | Speak | Cancel | Raid
           | Hello | Join | Played | Ptt | Heard | Status | Error)

_TYPES: dict[str, type] = {
    "session": Session, "channels": Channels, "moved": Moved, "invite": Invite,
    "refused": Refused, "voice": VoiceDef, "speak": Speak, "cancel": Cancel, "raid": Raid,
    "hello": Hello, "join": Join, "played": Played, "ptt": Ptt, "heard": Heard,
    "status": Status, "error": Error,
}
_NAMES = {cls: name for name, cls in _TYPES.items()}
MESSAGE_TYPES = tuple(_TYPES)
FROM_MIND = frozenset(MESSAGE_TYPES[:9])
FROM_VOICE = frozenset(MESSAGE_TYPES[9:])
_HINTS = {cls: typing.get_type_hints(cls) for cls in (*_TYPES.values(), Room)}


def type_name(msg) -> str:
    """The wire type of a message: "speak", or "voice" for a VoiceDef."""
    name = _NAMES.get(type(msg))
    if name is None:
        raise WireError(BAD_FIELD + "type", f"not a wire message: {type(msg).__name__}")
    return name


def _key(field_name: str) -> str:
    return field_name[:-1] if field_name.endswith("_") else field_name


def _value(raw, hint, where: str):
    if hint is bool or hint is int or hint is str or hint is dict:
        if type(raw) is hint:
            return raw
    elif hint is float:
        if type(raw) in (int, float) and math.isfinite(raw):
            return float(raw)
    elif typing.get_origin(hint) is tuple:
        if type(raw) is list:
            item = typing.get_args(hint)[0]
            return tuple(_value(v, item, f"{where}[{i}]") for i, v in enumerate(raw))
    elif dataclasses.is_dataclass(hint):
        return _build(hint, raw, where)
    else:
        raise WireError(BAD_FIELD + where, f"unsupported type {hint}")
    raise WireError(BAD_FIELD + where)


def _build(cls: type, obj, where: str = ""):
    if type(obj) is not dict:
        raise WireError(BAD_FIELD + where, "expected an object")
    hints = _HINTS[cls]
    kwargs = {}
    for f in dataclasses.fields(cls):
        key = _key(f.name)
        path = f"{where}.{key}" if where else key
        if key not in obj:
            if f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING:
                raise WireError(BAD_FIELD + path, "missing")
            continue
        kwargs[f.name] = _value(obj[key], hints[f.name], path)
    return cls(**kwargs)


def _one_of(value: str, allowed: tuple[str, ...], key: str) -> None:
    if value not in allowed:
        raise WireError(BAD_FIELD + key, f"{value!r} is not one of {', '.join(allowed)}")


def _check(msg) -> None:
    """The protocol's word lists and its rules beyond types."""
    match msg:
        case Speak():
            _one_of(msg.priority, TIERS, "priority")
            _one_of(msg.attitude, ATTITUDES, "attitude")
            _one_of(msg.state, STATES, "state")
        case Cancel():
            if (msg.id != "") + (msg.guid != 0) + (msg.below != "") != 1:
                raise WireError(BAD_FIELD + "id/guid/below", "set exactly one")
            if msg.below:
                _one_of(msg.below, TIERS, "below")
        case Raid():
            _one_of(msg.phase, PHASES, "phase")
        case Refused():
            _one_of(msg.reason, REFUSALS, "reason")
        case Played():
            _one_of(msg.state, PLAY_STATES, "state")
            if msg.state == "dropped":
                _one_of(msg.reason, DROP_REASONS, "reason")
            elif msg.state == "cut" and msg.reason:
                _one_of(msg.reason, DROP_REASONS, "reason")
            elif msg.state in ("started", "done") and msg.reason:
                raise WireError(BAD_FIELD + "reason", f"{msg.state} takes no reason")
        case Status():
            _one_of(msg.mic, MIC_STATES, "mic")
            _one_of(msg.stt, STT_STATES, "stt")
        case Channels():
            for i, room in enumerate(msg.channels):
                if room.capacity < 0:
                    raise WireError(BAD_FIELD + f"channels[{i}].capacity", "0 or more")


def _no_constant(name: str):
    raise ValueError(f"{name} is not JSON")


def parse(line: bytes) -> Message:
    """One line, with or without its newline. Raises WireError and nothing else."""
    try:
        raw = line.encode("utf-8") if isinstance(line, str) else bytes(line)
        raw = raw.rstrip(b"\r\n")
        if len(raw) > MAX_LINE_BYTES:
            raise WireError(LINE_TOO_LONG, f"{len(raw)} bytes")
        try:
            obj = json.loads(raw.decode("utf-8"), parse_constant=_no_constant)
        except (ValueError, RecursionError) as e:  # UnicodeDecodeError is a ValueError
            raise WireError(BAD_JSON, e.__class__.__name__) from None
        if type(obj) is not dict:
            raise WireError(BAD_JSON, "not an object")
        v = obj.get("v")
        if type(v) is not int or v != PROTOCOL_VERSION:
            raise WireError(UNSUPPORTED_VERSION)
        name = obj.get("type")
        cls = _TYPES.get(name) if type(name) is str else None
        if cls is None:
            raise WireError(UNKNOWN_TYPE + (name[:64] if type(name) is str else ""))
        if type(obj.get("ts")) is not int:
            raise WireError(BAD_FIELD + "ts")
        msg = _build(cls, obj)
        _check(msg)
        return msg
    except WireError:
        raise
    except Exception as e:
        raise WireError(BAD_JSON, f"unparseable: {e.__class__.__name__}") from None


def _jsonable(value):
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {_key(f.name): _jsonable(getattr(value, f.name))
                for f in dataclasses.fields(value)}
    if isinstance(value, (tuple, list)):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    return value


def as_fields(msg) -> dict:
    """A message's fields as its JSON object holds them, without the envelope."""
    type_name(msg)
    return _jsonable(msg)


def encode(msg, ts_ms: int) -> bytes:
    """The message's line, newline included. Raises WireError for a message parse would refuse
    (a wrong type or word, a line over MAX_LINE_BYTES) and for anything not a message."""
    name = type_name(msg)
    if type(ts_ms) is not int:
        raise WireError(BAD_FIELD + "ts")
    checked = _build(type(msg), as_fields(msg))  # the types parse checks, floats as floats
    _check(checked)
    body = {"v": PROTOCOL_VERSION, "type": name, "ts": ts_ms, **as_fields(checked)}
    try:
        line = json.dumps(body, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    except (TypeError, ValueError) as e:
        raise WireError(BAD_FIELD + "spec", e.__class__.__name__) from None
    if len(line) > MAX_LINE_BYTES:
        raise WireError(LINE_TOO_LONG, f"{len(line)} bytes")
    return line.encode("ascii") + b"\n"
