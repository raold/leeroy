"""The dojo wire contract, version 1: loopback NDJSON between one controller and one training worker.

Requests are hello, reset, step, status and close. Every line is one JSON object of at most MAX_LINE_BYTES bytes
(UTF-8, without the newline) carrying ``"v": 1``, a ``"type"`` and a request ``"id"``. Unknown fields are refused, so a
typo cannot silently change meaning. The worker answers every request line with exactly one line: a ``<type>_result``
with ``"ok": true`` or an ``"error"`` with a code from ErrorCode.

modules/mod-dojo/src/Lib/DojoProtocol.* implements the same rules; tests/fixtures/dojo_protocol.jsonl in that module
holds the cases both must agree on.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import Any

PROTOCOL_VERSION = 1
MAX_LINE_BYTES = 256 * 1024
MAX_ACTORS = 2
MAX_EVENTS = 64
RESULT_CACHE = 8
RECENT_EPISODES = 64
MAX_DECISION = 1_000_000
MAX_SEED = 2**53 - 1
MAX_INDEX = 2**31 - 1
MAX_TEXT_BYTES = 256
MAX_PROTOCOL = 1000
MAX_OBSERVATION_VALUES = 4096
MAX_DEPTH = 64

REQUEST_TYPES = ("hello", "reset", "step", "status", "close")

_ID = re.compile(r"[A-Za-z0-9._:-]{1,64}")

# Allowed fields per request type: required first, then optional.
_FIELDS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "hello": (("protocol",), ("client", "expect")),
    "reset": (("session", "episode", "decision", "scenario_seed"), ("scenario",)),
    "step": (("session", "episode", "decision", "actions"), ()),
    "status": ((), ("session",)),
    "close": (("session",), ("reason",)),
}
_ENVELOPE = ("v", "type", "id")
_EXPECT_KEYS = ("observation_schema", "action_schema", "scenario")


class ErrorCode:
    MALFORMED = "malformed"                          # not JSON, not an object, a missing, unknown or ill-typed field
    LINE_TOO_LONG = "line_too_long"                  # over max_line_bytes; the connection is closed after it
    UNSUPPORTED_VERSION = "unsupported_version"      # protocol, schema or scenario the worker does not speak
    NOT_READY = "not_ready"                          # not allowed in the current lifecycle state
    BAD_SESSION = "bad_session"                      # session is not this connection's
    BUSY = "busy"                                    # another mutating request is in flight
    STALE_EPISODE = "stale_episode"                  # an episode that is not the current one, or a reused id
    WRONG_STEP = "wrong_step"                        # decision index is not the expected one
    CONFLICTING_DUPLICATE = "conflicting_duplicate"  # a known request id with a different payload
    INVALID_ACTION = "invalid_action"                # missing or repeated actor, unknown or masked action
    ABORTED = "aborted"                              # the episode was aborted; "reason" says why
    RESULT_OVERFLOW = "result_overflow"              # the result would exceed a bound; the episode is aborted

    ALL = frozenset({MALFORMED, LINE_TOO_LONG, UNSUPPORTED_VERSION, NOT_READY, BAD_SESSION, BUSY, STALE_EPISODE,
                     WRONG_STEP, CONFLICTING_DUPLICATE, INVALID_ACTION, ABORTED, RESULT_OVERFLOW})


class AbortReason:
    DISCONNECT = "disconnect"
    CLOSED = "closed"
    FAULT = "fault"
    RESET_FAILED = "reset_failed"
    INVALID_RESULT = "invalid_result"
    RESULT_OVERFLOW = "result_overflow"


@dataclass(frozen=True)
class Bounds:
    max_line_bytes: int = MAX_LINE_BYTES
    max_actors: int = MAX_ACTORS
    max_events: int = MAX_EVENTS
    result_cache: int = RESULT_CACHE

    def to_wire(self) -> dict[str, int]:
        return {"max_line_bytes": self.max_line_bytes, "max_actors": self.max_actors,
                "max_events": self.max_events, "result_cache": self.result_cache,
                "recent_episodes": RECENT_EPISODES, "in_flight": 1}

    @classmethod
    def from_wire(cls, data: dict[str, Any] | None) -> Bounds:
        data = data or {}
        return cls(**{k: int(data[k]) for k in ("max_line_bytes", "max_actors", "max_events", "result_cache")
                      if k in data})


@dataclass(frozen=True)
class Request:
    type: str
    id: str
    body: dict[str, Any]
    fingerprint: str


@dataclass(frozen=True)
class Decoded:
    request: Request | None
    code: str | None = None
    id: str | None = None
    message: str = ""


def encode(obj: Any) -> str:
    """One compact line, no newline. Refuses NaN and infinities, which are not JSON."""
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def canonical(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False, allow_nan=False, sort_keys=True)


def line_bytes(obj: Any) -> int:
    return len(encode(obj).encode("utf-8"))


def error(code: str, request_id: str | None, message: str, reason: str | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"v": PROTOCOL_VERSION, "type": "error", "id": request_id, "ok": False, "code": code,
                           "message": message}
    if reason is not None:
        out["reason"] = reason
    return out


def is_id(value: Any) -> bool:
    return isinstance(value, str) and _ID.fullmatch(value) is not None


def is_int(value: Any, low: int, high: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and low <= value <= high


def is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def is_text(value: Any) -> bool:
    return isinstance(value, str) and len(value.encode("utf-8")) <= MAX_TEXT_BYTES


def _refuse_constant(name: str) -> Any:
    raise ValueError(f"{name} is not JSON")


class _Malformed(Exception):
    pass


def _too_deep(text: str) -> bool:
    """More than MAX_DEPTH nested arrays or objects, counted outside strings."""
    if text.count("[") + text.count("{") <= MAX_DEPTH:
        return False
    depth, in_string, escaped = 0, False, False
    for ch in text:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch in "[{":
            depth += 1
            if depth > MAX_DEPTH:
                return True
        elif ch in "]}":
            depth -= 1
    return False


def decode_request(line: bytes | str, bounds: Bounds | None = None) -> Decoded:
    """Validates one request line. Never raises; a refusal carries the line's id when it had a valid one."""
    bounds = bounds or Bounds()
    raw = line.encode("utf-8") if isinstance(line, str) else bytes(line)
    if len(raw) > bounds.max_line_bytes:
        return Decoded(None, ErrorCode.LINE_TOO_LONG, None, f"line over {bounds.max_line_bytes} bytes")
    try:
        text = raw.decode("utf-8")
        if _too_deep(text):
            return Decoded(None, ErrorCode.MALFORMED, None, f"nested deeper than {MAX_DEPTH}")
        obj = json.loads(text, parse_constant=_refuse_constant)
        canonical(obj).encode("utf-8")   # a lone surrogate escape parses here but is not valid Unicode
    except (UnicodeError, ValueError, RecursionError):
        return Decoded(None, ErrorCode.MALFORMED, None, "not JSON")
    if not isinstance(obj, dict):
        return Decoded(None, ErrorCode.MALFORMED, None, "not a JSON object")

    request_id = obj.get("id") if is_id(obj.get("id")) else None
    if "v" not in obj:
        return Decoded(None, ErrorCode.MALFORMED, request_id, "missing v")
    if not is_int(obj["v"], -2**63, 2**64 - 1):
        return Decoded(None, ErrorCode.MALFORMED, request_id, "v is not an integer")
    if obj["v"] != PROTOCOL_VERSION:
        return Decoded(None, ErrorCode.UNSUPPORTED_VERSION, request_id, f"protocol v{obj['v']} is not spoken here")
    if request_id is None:
        return Decoded(None, ErrorCode.MALFORMED, None, "missing or invalid id")
    kind = obj.get("type")
    if not isinstance(kind, str) or kind not in _FIELDS:
        return Decoded(None, ErrorCode.MALFORMED, request_id, "unknown type")

    required, optional = _FIELDS[kind]
    allowed = set(_ENVELOPE) | set(required) | set(optional)
    unknown = sorted(k for k in obj if k not in allowed)
    if unknown:
        return Decoded(None, ErrorCode.MALFORMED, request_id, f"unknown field: {unknown[0]}")
    for name in required:
        if name not in obj:
            return Decoded(None, ErrorCode.MALFORMED, request_id, f"missing {name}")
    try:
        _check_fields(kind, obj, bounds)
    except _Malformed as e:
        return Decoded(None, ErrorCode.MALFORMED, request_id, str(e))
    return Decoded(Request(kind, request_id, obj, canonical(obj)), None, request_id, "")


def _check_fields(kind: str, obj: dict[str, Any], bounds: Bounds) -> None:
    def need(ok: bool, name: str) -> None:
        if not ok:
            raise _Malformed(f"bad field: {name}")

    if kind == "hello":
        p = obj["protocol"]
        need(isinstance(p, dict) and set(p) == {"min", "max"} and is_int(p.get("min"), 1, MAX_PROTOCOL)
             and is_int(p.get("max"), 1, MAX_PROTOCOL) and p["min"] <= p["max"], "protocol")
        if "client" in obj:
            need(is_text(obj["client"]), "client")
        if "expect" in obj:
            e = obj["expect"]
            need(isinstance(e, dict) and all(k in _EXPECT_KEYS and is_text(v) for k, v in e.items()), "expect")
        return
    if "session" in obj:
        need(is_id(obj["session"]), "session")
    if "episode" in obj:
        need(is_id(obj["episode"]), "episode")
    if "decision" in obj:
        need(is_int(obj["decision"], 0, MAX_DECISION), "decision")
    if kind == "reset":
        need(is_int(obj["scenario_seed"], 0, MAX_SEED), "scenario_seed")
        if "scenario" in obj:
            need(is_id(obj["scenario"]), "scenario")
    elif kind == "step":
        actions = obj["actions"]
        need(isinstance(actions, list) and len(actions) <= bounds.max_actors, "actions")
        for a in actions:
            need(isinstance(a, dict) and set(a) == {"actor", "action"} and is_int(a.get("actor"), 0, MAX_INDEX)
                 and is_int(a.get("action"), 0, MAX_INDEX), "actions")
    elif kind == "close":
        if "reason" in obj:
            need(is_text(obj["reason"]), "reason")
