"""Bridge protocol v1: NDJSON messages between the mind and mod-leeroy."""

import dataclasses
import json
import typing
import unicodedata
from dataclasses import dataclass

PROTOCOL_VERSION = 1
MAX_LINE_BYTES = 65536
MAX_TEXT_BYTES = 255
CHAT_CHANNELS = frozenset(
    {"guild", "officer", "party", "raid", "raid_warning", "say", "yell", "whisper", "emote"})
SAY_CHANNELS = frozenset({"guild", "officer", "party", "raid", "say", "yell", "whisper"})


class ProtocolError(ValueError):
    pass


class VersionError(ProtocolError):
    pass


class UnknownType(ProtocolError):
    pass


@dataclass(frozen=True, slots=True)
class Member:
    guid: int
    name: str
    race: int
    class_: int
    gender: int
    level: int
    rank: int
    online: bool
    map: int
    zone: int
    spec_tab: int


@dataclass(frozen=True, slots=True)
class LootItem:
    item_id: int
    name: str
    quality: int
    index: int


@dataclass(frozen=True, slots=True)
class Hello:
    ts: int
    server: str
    version: str
    guild_id: int
    human_guid: int


@dataclass(frozen=True, slots=True)
class Cast:
    ts: int
    members: tuple[Member, ...]


@dataclass(frozen=True, slots=True)
class Login:
    ts: int
    guid: int
    human: bool


@dataclass(frozen=True, slots=True)
class Logout:
    ts: int
    guid: int
    human: bool


@dataclass(frozen=True, slots=True)
class Chat:
    ts: int
    channel: str
    from_guid: int
    from_name: str
    to_guid: int
    text: str
    map: int
    zone: int
    via_bridge: bool = False


@dataclass(frozen=True, slots=True)
class TextEmote:
    ts: int
    guid: int
    emote: str
    target_guid: int
    via_bridge: bool = False


@dataclass(frozen=True, slots=True)
class AfkEvent:
    ts: int
    guid: int
    on: bool


@dataclass(frozen=True, slots=True)
class Boss:
    ts: int
    map: int
    instance_id: int
    boss_id: int
    boss_name: str
    state: str
    members: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class Death:
    ts: int
    guid: int
    map: int
    killer_entry: int


@dataclass(frozen=True, slots=True)
class Resurrect:
    ts: int
    guid: int


@dataclass(frozen=True, slots=True)
class LootDrop:
    ts: int
    map: int
    instance_id: int
    source_entry: int
    items: tuple[LootItem, ...]


@dataclass(frozen=True, slots=True)
class LootWon:
    ts: int
    guid: int
    item_id: int
    name: str
    method: str


@dataclass(frozen=True, slots=True)
class Achievement:
    ts: int
    guid: int
    achievement_id: int
    name: str


@dataclass(frozen=True, slots=True)
class BgEnd:
    ts: int
    bg_type: int
    winner: str
    members: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class Zone:
    ts: int
    guid: int
    zone_id: int
    zone_name: str


@dataclass(frozen=True, slots=True)
class Group:
    ts: int
    leader_guid: int
    raid: bool
    members: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class DcStatus:
    ts: int
    tank_guid: int
    enabled: bool
    paused: bool
    payload: str


@dataclass(frozen=True, slots=True)
class Ack:
    ts: int
    id: str
    ok: bool
    error: str


@dataclass(frozen=True, slots=True)
class Pong:
    ts: int
    id: str


@dataclass(frozen=True, slots=True)
class Error:
    ts: int
    message: str
    id: str = ""


Event = (Hello | Cast | Login | Logout | Chat | TextEmote | AfkEvent | Boss | Death | Resurrect
         | LootDrop | LootWon | Achievement | BgEnd | Zone | Group | DcStatus | Ack | Pong | Error)


@dataclass(frozen=True, slots=True)
class Ping:
    id: str


@dataclass(frozen=True, slots=True)
class GetCast:
    id: str


@dataclass(frozen=True, slots=True)
class Say:
    id: str
    guid: int
    channel: str
    text: str
    to_guid: int = 0


@dataclass(frozen=True, slots=True)
class Emote:
    id: str
    guid: int
    emote: str
    target_guid: int = 0


@dataclass(frozen=True, slots=True)
class HumanSay:
    id: str
    channel: str
    text: str
    to_name: str = ""


@dataclass(frozen=True, slots=True)
class SetRank:
    id: str
    guid: int
    rank: int


@dataclass(frozen=True, slots=True)
class SetMotd:
    id: str
    text: str


@dataclass(frozen=True, slots=True)
class AddFriend:
    id: str
    guid: int
    note: str


@dataclass(frozen=True, slots=True)
class Afk:
    id: str
    guid: int
    on: bool
    message: str = ""


Command = Ping | GetCast | Say | Emote | HumanSay | SetRank | SetMotd | AddFriend | Afk

_CORE_EVENTS: dict[str, type] = {
    "hello": Hello, "cast": Cast, "login": Login, "logout": Logout, "chat": Chat,
    "text_emote": TextEmote, "afk": AfkEvent, "boss": Boss, "death": Death,
    "resurrect": Resurrect, "loot_drop": LootDrop, "loot_won": LootWon,
    "achievement": Achievement, "bg_end": BgEnd, "zone": Zone, "group": Group,
    "dc_status": DcStatus, "ack": Ack, "pong": Pong, "error": Error,
}
_CORE_COMMANDS: dict[str, type] = {
    "ping": Ping, "get_cast": GetCast, "say": Say, "emote": Emote, "human_say": HumanSay,
    "set_rank": SetRank, "set_motd": SetMotd, "add_friend": AddFriend, "afk": Afk,
}
_events: dict[str, type] = dict(_CORE_EVENTS)
_commands: dict[str, type] = dict(_CORE_COMMANDS)
CORE_EVENT_TYPES = frozenset(_CORE_EVENTS)


def event_type(ev) -> str:
    """The wire type name of a parsed event: "chat", or the name an extension registered."""
    for name, cls in _events.items():
        if type(ev) is cls:
            return name
    return type(ev).__name__.lower()


def _register(registry: dict[str, type], core: dict[str, type], type_name: str, cls: type) -> None:
    if not isinstance(type_name, str) or not type_name:
        raise ValueError("type name must be a non-empty string")
    if not isinstance(cls, type) or not dataclasses.is_dataclass(cls):
        raise ValueError(f"{cls!r} is not a dataclass")
    if type_name in core:
        raise ValueError(f"{type_name!r} is a core type")
    existing = registry.get(type_name)
    if existing is cls:
        return
    if existing is not None:
        raise ValueError(f"{type_name!r} is already registered to {existing.__name__}")
    registry[type_name] = cls


def register_event(type_name: str, cls: type) -> None:
    _register(_events, _CORE_EVENTS, type_name, cls)


def register_command(type_name: str, cls: type) -> None:
    _register(_commands, _CORE_COMMANDS, type_name, cls)


def _key(field_name: str) -> str:
    return field_name[:-1] if field_name.endswith("_") else field_name


def _convert(value, hint, where: str):
    if hint is bool or hint is int or hint is str:
        if type(value) is not hint:
            raise ProtocolError(f"field {where}: expected {hint.__name__}")
        return value
    if typing.get_origin(hint) is tuple:
        item_hint = typing.get_args(hint)[0]
        if type(value) is not list:
            raise ProtocolError(f"field {where}: expected array")
        return tuple(_convert(v, item_hint, f"{where}[{i}]") for i, v in enumerate(value))
    if isinstance(hint, type) and dataclasses.is_dataclass(hint):
        return _build(hint, value, where)
    raise ProtocolError(f"field {where}: unsupported type {hint}")


def _build(cls: type, obj, where: str = ""):
    if type(obj) is not dict:
        raise ProtocolError(f"field {where}: expected object")
    hints = typing.get_type_hints(cls)
    prefix = f"{where}." if where else ""
    kwargs = {}
    for f in dataclasses.fields(cls):
        key = _key(f.name)
        if key not in obj:
            if f.default is not dataclasses.MISSING or f.default_factory is not dataclasses.MISSING:
                continue
            raise ProtocolError(f"missing field: {prefix}{key}")
        kwargs[f.name] = _convert(obj[key], hints[f.name], f"{prefix}{key}")
    return cls(**kwargs)


def parse_event(line: bytes | str) -> Event:
    """Parse one bridge line. Raises ProtocolError (or a subclass) and nothing else."""
    try:
        raw = line.encode("utf-8", "replace") if isinstance(line, str) else bytes(line)
        if len(raw) > MAX_LINE_BYTES:
            raise ProtocolError(f"line too long: {len(raw)} bytes")
        try:
            obj = json.loads(raw.decode("utf-8", "replace"))
        except (ValueError, RecursionError) as exc:
            raise ProtocolError(f"invalid json: {exc}") from None
        if type(obj) is not dict:
            raise ProtocolError("message is not a json object")
        v = obj.get("v")
        if type(v) is not int or v != PROTOCOL_VERSION:
            raise VersionError(f"unsupported v: {v!r}")
        type_name = obj.get("type")
        if type(type_name) is not str:
            raise ProtocolError("field type: expected str")
        cls = _events.get(type_name)
        if cls is None:
            raise UnknownType(f"unknown type: {type_name}")
        return _build(cls, obj)
    except ProtocolError:
        raise
    except Exception as exc:
        raise ProtocolError(f"unparseable line: {exc.__class__.__name__}") from None


def sanitize_say_text(text: str) -> str:
    out = []
    for c in text:
        if c == "|":
            continue
        if c in "\r\n\t":
            out.append(" ")
        elif unicodedata.category(c) in ("Cc", "Cs"):
            continue
        elif c.isspace():
            out.append(" ")
        else:
            out.append(c)
    collapsed = " ".join("".join(out).split())
    return collapsed.encode("utf-8")[:MAX_TEXT_BYTES].decode("utf-8", "ignore").rstrip()


def _jsonable(value):
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {_key(f.name): _jsonable(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, (tuple, list)):
        return [_jsonable(v) for v in value]
    return value


def encode_command(cmd, ts_ms: int) -> bytes:
    type_name = next((n for n, c in _commands.items() if type(cmd) is c), None)
    if type_name is None:
        raise ValueError(f"not a registered command: {type(cmd).__name__}")
    body = _jsonable(cmd)
    if isinstance(cmd, (Say, HumanSay)) and cmd.channel not in SAY_CHANNELS:
        raise ValueError(f"channel not allowed: {cmd.channel!r}")
    if isinstance(cmd, (Say, HumanSay, SetMotd)):
        body["text"] = sanitize_say_text(cmd.text)
    if isinstance(cmd, HumanSay) and body["text"].startswith((".", "!")):
        raise ValueError("human_say text may not start with '.' or '!'")
    envelope = {"v": PROTOCOL_VERSION, "type": type_name, "ts": ts_ms}
    line = json.dumps({**envelope, **body}, separators=(",", ":")).encode("ascii") + b"\n"
    if len(line) > MAX_LINE_BYTES:
        raise ValueError("command line too long")
    return line
