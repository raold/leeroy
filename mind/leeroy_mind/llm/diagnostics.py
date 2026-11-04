"""Content-free observations of a parsed chat reply, before text cleanup."""
from dataclasses import dataclass

_MISSING = object()
_KINDS = frozenset({"missing", "null", "invalid", "empty", "whitespace", "text"})
_REASONS = frozenset({"stop", "length", "load", "unload", "other", "invalid"})
_MAX_COUNTER = (1 << 63) - 1


def _counter(value):
    return value if type(value) is int and 0 <= value <= _MAX_COUNTER else None


def _text_shape(value):
    if value is _MISSING:
        return "missing", None
    if value is None:
        return "null", None
    if not isinstance(value, str):
        return "invalid", None
    return ("empty" if not value else "whitespace" if not value.strip() else "text"), len(value)


@dataclass(frozen=True)
class ResponseDiagnostics:
    message_kind: str
    content_kind: str
    content_chars: int | None
    thinking_kind: str
    thinking_chars: int | None
    done: bool | None
    done_reason: str | None
    eval_count: int | None
    prompt_eval_count: int | None
    total_duration_ns: int | None
    load_duration_ns: int | None
    prompt_eval_duration_ns: int | None
    eval_duration_ns: int | None

    def __post_init__(self):
        if (type(self.message_kind) is not str or self.message_kind not in {"missing", "null", "object", "invalid"}
                or type(self.content_kind) is not str or self.content_kind not in _KINDS
                or type(self.thinking_kind) is not str or self.thinking_kind not in _KINDS
                or (self.done is not None and type(self.done) is not bool)
                or (self.done_reason is not None and (type(self.done_reason) is not str
                                                      or self.done_reason not in _REASONS))):
            raise ValueError("invalid diagnostic label")
        for name in ("content_chars", "thinking_chars", "eval_count", "prompt_eval_count",
                     "total_duration_ns", "load_duration_ns", "prompt_eval_duration_ns", "eval_duration_ns"):
            value = getattr(self, name)
            if value is not None and _counter(value) is None:
                raise ValueError("invalid diagnostic counter")


def capture_response(out: dict) -> ResponseDiagnostics:
    message = out.get("message", _MISSING)
    message_kind = ("missing" if message is _MISSING else "null" if message is None
                    else "object" if isinstance(message, dict) else "invalid")
    fields = message if isinstance(message, dict) else {}
    content_kind, content_chars = _text_shape(fields.get("content", _MISSING))
    thinking_kind, thinking_chars = _text_shape(fields.get("thinking", _MISSING))
    reason = out.get("done_reason")
    reason = (None if reason is None else "invalid" if not isinstance(reason, str)
              else reason if reason in {"stop", "length", "load", "unload"} else "other")
    return ResponseDiagnostics(message_kind, content_kind, content_chars, thinking_kind, thinking_chars,
        out.get("done") if type(out.get("done")) is bool else None, reason,
        *(_counter(out.get(name)) for name in ("eval_count", "prompt_eval_count", "total_duration",
                                             "load_duration", "prompt_eval_duration", "eval_duration")))
