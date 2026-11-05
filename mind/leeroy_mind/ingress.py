"""Immutable native-source snapshots and their canonical JSON bindings.

This leaf has no protocol, storage, or Realm imports. A snapshot alone grants no
delivery or business authority; its owner must also check the live delivery.
"""

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

_ENVELOPE_PREFIX = b"realm-envelope-v1\0"
_ROW_PREFIX = b"realm-row-binding-v1\0"
_MAX_CANONICAL_BYTES = 65536


def _digest(value: str) -> bool:
    return (type(value) is str and len(value) == 64
            and all(c in "0123456789abcdef" for c in value))


def _text(value) -> str:
    if type(value) is not str:
        raise ValueError("expected string")
    value.encode("utf-8", "strict")
    return value


@dataclass(frozen=True, slots=True)
class WireOrigin:
    envelope_sha256: str
    protocol_version: int
    event_type: str
    server_ms: int
    received_at: float
    transport_epoch: int
    top_keys: tuple[str, ...]
    origin: Literal["bridge"]

    def __post_init__(self):
        if not _digest(self.envelope_sha256):
            raise ValueError("invalid envelope digest")
        if type(self.protocol_version) is not int or self.protocol_version != 1:
            raise ValueError("invalid protocol version")
        if (not _text(self.event_type) or type(self.server_ms) is not int
                or not 1 <= self.server_ms <= 253402300799999):
            raise ValueError("invalid event identity")
        if (type(self.received_at) not in (int, float) or not math.isfinite(self.received_at)
                or not 0 <= self.received_at <= 253402300799.999):
            raise ValueError("invalid receive time")
        object.__setattr__(self, "received_at", float(self.received_at))
        if type(self.transport_epoch) is not int or not 0 < self.transport_epoch <= 0xFFFFFFFFFFFFFFFF:
            raise ValueError("invalid transport epoch")
        keys = tuple(_text(key) for key in self.top_keys)
        if len(keys) > 64 or keys != tuple(sorted(set(keys))):
            raise ValueError("invalid envelope keys")
        object.__setattr__(self, "top_keys", keys)
        if type(self.origin) is not str or self.origin != "bridge":
            raise ValueError("invalid wire origin")


@dataclass(frozen=True, slots=True)
class RowReceipt:
    row_kind: Literal["game_event", "chat_line"]
    row_id: int
    row_sha256: str
    wire: WireOrigin
    backlog: bool

    def __post_init__(self):
        if type(self.row_kind) is not str or self.row_kind not in ("game_event", "chat_line"):
            raise ValueError("invalid row kind")
        _row_id(self.row_id)
        if not _digest(self.row_sha256) or type(self.wire) is not WireOrigin:
            raise ValueError("invalid row binding")
        if type(self.backlog) is not bool:
            raise ValueError("invalid backlog flag")


def _json_value(value, depth: int = 0, *, max_depth: int | None = None):
    """Copy only exact JSON values, rejecting surrogates and nonfinite numbers."""
    if isinstance(value, Mapping):
        if max_depth is not None and depth >= max_depth:
            raise ValueError("JSON nesting exceeds 32")
        return {_text(key): _json_value(child, depth + 1, max_depth=max_depth)
                for key, child in value.items()}
    if type(value) in (list, tuple):
        if max_depth is not None and depth >= max_depth:
            raise ValueError("JSON nesting exceeds 32")
        return [_json_value(child, depth + 1, max_depth=max_depth) for child in value]
    if type(value) is str:
        return _text(value)
    if value is None or type(value) in (bool, int):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    raise ValueError("expected finite JSON value")


def _canonical(value, *, max_depth: int | None = None) -> bytes:
    return json.dumps(_json_value(value, max_depth=max_depth), sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def _unique_object(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError("duplicate JSON key")
        obj[key] = value
    return obj


def _nonfinite(value):
    raise ValueError("nonfinite JSON number")


def _wire_origin(line: bytes | str, *, received_at: float,
                 transport_epoch: int) -> WireOrigin | None:
    """Strict full-envelope metadata; ordinary protocol parsing is separate."""
    try:
        raw = line.encode("utf-8", "strict") if isinstance(line, str) else bytes(line)
        obj = json.loads(raw.decode("utf-8", "strict"), object_pairs_hook=_unique_object,
                         parse_constant=_nonfinite)
        if type(obj) is not dict or len(obj) > 64:
            return None
        canonical = _canonical(obj, max_depth=32)
        if len(canonical) > _MAX_CANONICAL_BYTES:
            return None
        return WireOrigin(hashlib.sha256(_ENVELOPE_PREFIX + canonical).hexdigest(),
                          obj["v"], obj["type"], obj["ts"], received_at,
                          transport_epoch, tuple(sorted(obj)), "bridge")
    except (KeyError, TypeError, ValueError, OverflowError, RecursionError):
        return None


def _row_id(value):
    if type(value) is not int or not 0 < value <= 0x7FFFFFFFFFFFFFFF:
        raise ValueError("invalid row id")


def game_row_binding(row_id: int, event_type: str, payload: Mapping) -> str:
    """Bind the exact stored event type and complete parsed payload to its id."""
    _row_id(row_id)
    _text(event_type)
    if not isinstance(payload, Mapping):
        raise ValueError("expected payload object")
    body = {"row_kind": "game_event", "row_id": row_id, "type": event_type, "payload": payload}
    return hashlib.sha256(_ROW_PREFIX + _canonical(body)).hexdigest()


def chat_row_binding(row_id: int, *, channel: str, from_guid: int, from_name: str,
                     to_guid: int, text: str, map: int, zone: int, origin: str) -> str:
    """Bind immutable chat fields, excluding later thread/audience bookkeeping."""
    _row_id(row_id)
    for value in (channel, from_name, text, origin):
        _text(value)
    if any(type(value) is not int for value in (from_guid, to_guid, map, zone)):
        raise ValueError("expected integer chat identity")
    body = {"row_kind": "chat_line", "row_id": row_id, "channel": channel,
            "from_guid": from_guid, "from_name": from_name, "to_guid": to_guid,
            "text": text, "map": map, "zone": zone, "origin": origin}
    return hashlib.sha256(_ROW_PREFIX + _canonical(body)).hexdigest()
