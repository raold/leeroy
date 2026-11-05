"""Bounded, loop-owned observations of existing turn decisions, never policy clocks.

Only scalar handles cross into this collector. A completed handle cannot be admitted
again, and no completed history, text, identity, or wire message is retained.
"""
from contextlib import contextmanager
from dataclasses import dataclass, field
from hashlib import sha256
import math
import secrets
import time
from typing import Callable, Literal

from leeroy_mind.logs import log

MAX_ACTIVE = 256
MAX_AGE_S = 120.0
_COUNT_MAX = 2**31 - 1
Stage = Literal["delay", "hold", "compose", "typing", "send_lock", "bridge_ack"]
_STAGES = frozenset({"delay", "hold", "compose", "typing", "send_lock", "bridge_ack"})
_REASONS = frozenset({
    "none", "cancelled", "error", "expired", "ttl", "shutdown", "session_end",
    "disconnected", "no_ack", "duplicate_id", "not_observed", "ack_timeout",
    "connection_lost", "send_error", "record_error", "refused", "voice_channel",
    "no_channel", "no_persona", "after_dropped", "bad_turn", "llm_error", "silent",
    "tells", "emote_only", "command", "empty", "offline", "afk", "dead",
    "human_offline", "flirt_target", "flirt_stance", "raid", "guards", "chain",
    "elsewhere_changed", "hotmic_changed", "cancel", "stall", "room", "ptt",
    "scope_changed", "transport_refused",
})
_OUTCOMES = frozenset({"completed", "dropped", "cancelled", "error", "incomplete",
                       "started", "done", "cut", "first_ack", "echo_confirmed"})
_DEADLINES = frozenset({"not_observed", "mind_before_generation", "mind_model_queue",
                        "mind_readiness_admission", "mind_after_generation"})


def voice_token(speak_id: str) -> str:
    """Shared v1 join algorithm; this digest is correlation, not anonymization."""
    return sha256(speak_id.encode("utf-8")).hexdigest()


def _ms(start: float | None, end: float | None) -> float | None:
    if start is None or end is None:
        return None
    value = (end - start) * 1000.0
    return value if math.isfinite(value) and value >= 0 else None


def _sum(a: float | None, b: float | None) -> float | None:
    if b is None:
        return a
    value = (a or 0.0) + b
    return value if math.isfinite(value) else None


@dataclass(slots=True)
class _Attempt:
    phase: str = "not_observed"
    at: float | None = None
    queue_ms: float | None = None
    admission_ms: float | None = None
    call_ms: float | None = None
    outcome: str = "not_observed"


@dataclass(slots=True)
class _Trace:
    origin: float
    admitted: float
    origin_scope: str
    planning_ms: float | None
    queue_ms: float | None = None
    queue_outcome: str = "not_observed"
    delay_ms: float | None = None
    hold_ms: float | None = None
    compose_ms: float | None = None
    typing_ms: float | None = None
    send_lock_ms: float | None = None
    bridge_ack_ms: float | None = None
    attempt1: _Attempt = field(default_factory=_Attempt)
    attempt2: _Attempt = field(default_factory=_Attempt)
    attempts: int = 0
    voice_token: str | None = None
    voice_sent: float | None = None
    voice_started: float | None = None
    voice_terminal: float | None = None
    typed_sent: float | None = None
    typed_ack: float | None = None
    typed_echo: float | None = None
    typed_acks: int = 0
    first_ack: bool = False
    echo_pending: bool = False
    pipeline_outcome: str = "not_observed"
    reason: str = "none"
    deadline: str = "not_observed"
    deadline_outcome: str = "not_observed"


def _emit(fields: dict) -> None:
    log("turn_timing", **{k: v for k, v in fields.items() if k != "event"})


class TurnTiming:
    def __init__(self, *, timer: Callable[[], float] = time.monotonic,
                 emit: Callable[[dict], None] = _emit):
        self._timer, self._emit = timer, emit
        self._nonce = secrets.token_hex(8)
        self._seq = 0
        self._active: dict[str, _Trace] = {}
        self._closed = False
        self.suppressed = 0

    def now(self) -> float | None:
        try:
            value = float(self._timer())
            return value if math.isfinite(value) else None
        except Exception:
            return None

    def begin(self, *, origin: float | None = None) -> str:
        self.expire()
        now = self.now()
        if self._closed or now is None or len(self._active) >= MAX_ACTIVE or self._seq >= 2**64 - 1:
            self.suppressed = min(_COUNT_MAX, self.suppressed + 1)
            return ""  # admission was attempted; the caller must not retry this turn
        self._seq += 1
        token = f"{self._nonce}-{self._seq:016x}"
        observed = origin is not None and math.isfinite(origin) and origin <= now
        self._active[token] = _Trace(
            origin if observed else now, now,
            "mind_human_chat_entry" if observed else "turn_admission",
            _ms(origin, now) if observed else None)
        return token

    def _get(self, token: str | None) -> _Trace | None:
        self.expire()
        return self._active.get(token)

    def dequeued(self, token: str | None) -> None:
        if (r := self._get(token)) is not None and r.queue_outcome == "not_observed":
            r.queue_ms = _ms(r.admitted, self.now())
            r.queue_outcome = "completed"

    def queued_done(self, tokens: tuple[str, ...], *, cancelled: bool) -> None:
        for token in tokens:
            if (r := self._get(token)) is not None and r.queue_outcome == "not_observed":
                r.queue_ms = _ms(r.admitted, self.now())
                r.queue_outcome = "cancelled" if cancelled else "error"
                self.finish(token, "cancelled" if cancelled else "error",
                            "cancelled" if cancelled else "error")

    @contextmanager
    def span(self, token: str | None, stage: Stage):
        start = self.now()
        try:
            yield
        finally:
            if stage in _STAGES and (r := self._get(token)) is not None:
                name = stage + "_ms"
                setattr(r, name, _sum(getattr(r, name), _ms(start, self.now())))

    def model(self, token: str | None, attempt: int, phase: str) -> None:
        """One active phase per attempt, including partial waits on failed/cancelled exits."""
        if attempt not in (0, 1) or (r := self._get(token)) is None:
            return
        a = r.attempt1 if attempt == 0 else r.attempt2
        if phase == "queue" and a.phase == "not_observed":
            r.attempts += 1
        elif a.phase not in {"queue", "admission", "call", "started"}:
            return  # completed attempt or a late scheduler closure
        if a.phase in {"queue", "admission", "call"}:
            setattr(a, a.phase + "_ms", _ms(a.at, self.now()))
        a.at = self.now()
        if phase in {"queue", "admission", "call", "started"}:
            a.phase = phase
        else:
            a.phase = "finished"
            a.outcome = phase if phase in {"completed", "cancelled", "error", "expired",
                                           "scope_changed"} else "other"

    def decision(self, token: str | None, reason: str, *, deadline: str = "not_observed") -> None:
        if (r := self._get(token)) is not None:
            r.reason = reason if reason in _REASONS else "other"
        if deadline != "not_observed":
            self.deadline_check(token, deadline, expired=True)

    def deadline_check(self, token: str | None, scope: str, *, expired: bool) -> None:
        if scope in _DEADLINES and (r := self._get(token)) is not None:
            if r.deadline_outcome != "expired":
                r.deadline = scope
                r.deadline_outcome = "expired" if expired else "within_deadline"

    def typed_send(self, token: str | None) -> None:
        if (r := self._get(token)) is not None and r.typed_sent is None:
            r.typed_sent = self.now()

    def typed_ack(self, token: str | None) -> None:
        if (r := self._get(token)) is not None:
            r.typed_acks = min(_COUNT_MAX, r.typed_acks + 1)
            if r.typed_ack is None:
                r.typed_ack = self.now()
            self._first(token, r)

    def uncertain(self, token: str | None, reason: str) -> None:
        self.decision(token, reason)
        if (r := self._get(token)) is not None:
            r.echo_pending = True

    def echo_settled(self, token: str | None, *, confirmed: bool) -> None:
        if (r := self._get(token)) is None:
            return
        r.echo_pending = False
        if confirmed:
            r.typed_echo = self.now()
            self._first(token, r)
        self.finish(token, "echo_confirmed" if confirmed else "incomplete", r.reason)

    def voice_sent(self, token: str | None, speak_id: str) -> None:
        if (r := self._get(token)) is None:
            return
        try:
            digest = voice_token(speak_id)
        except UnicodeEncodeError:
            return  # wire accepts surrogate IDs; correlation must not affect delivery
        duplicates = [t for t, old in self._active.items() if old.voice_token == digest]
        r.voice_token, r.voice_sent = digest, self.now()
        if duplicates:  # ambiguous existing wire ID: do not guess which line an ACK belongs to
            for other in duplicates:
                self.finish(other, "incomplete", "duplicate_id")
            self.finish(token, "incomplete", "duplicate_id")

    def played(self, speak_id: str, state: str, reason: str = "") -> None:
        self.expire()
        try:
            digest = voice_token(speak_id)
        except UnicodeEncodeError:
            return
        for token, r in list(self._active.items()):
            if r.voice_token != digest:
                continue
            if state == "started":
                if r.voice_started is None:
                    r.voice_started = self.now()
                    self._first(token, r)
            elif state in {"done", "cut", "dropped"}:
                r.voice_terminal = self.now()
                self.finish(token, state, reason or "none")
            return

    def voice_missing(self, speak_id: str | None = None, *, reason: str) -> None:
        self.expire()
        try:
            digest = voice_token(speak_id) if speak_id is not None else None
        except UnicodeEncodeError:
            return  # invalid correlation is not the None sentinel for all voice traces
        for token, r in list(self._active.items()):
            if r.voice_token is not None and (digest is None or r.voice_token == digest):
                self.finish(token, "incomplete", reason)

    def pipeline_done(self, token: str | None, outcome: str = "completed") -> None:
        if (r := self._get(token)) is None:
            return
        r.pipeline_outcome = outcome if outcome in _OUTCOMES else "other"
        if outcome in {"cancelled", "error"} and r.reason == "none":
            r.reason = outcome
        if r.echo_pending or r.voice_sent is not None:
            return
        if outcome == "completed" and r.reason != "none":
            outcome = "dropped"
        self.finish(token, outcome, r.reason)

    def _first(self, token: str, r: _Trace) -> None:
        if not r.first_ack:
            r.first_ack = True  # set before calling the sink, including a failing sink
            self._publish(token, r, "first_ack", "none", "first_ack")

    def finish(self, token: str | None, outcome: str, reason: str = "none") -> None:
        r = self._active.pop(token, None)
        if r is not None:
            self._publish(token, r, outcome, reason, "terminal")

    def expire(self) -> None:
        now = self.now()
        if now is not None:
            for token, r in list(self._active.items()):
                if now - r.admitted >= MAX_AGE_S:
                    self.finish(token, "incomplete", "ttl")

    def flush(self, reason: str = "session_end") -> None:
        for token in list(self._active):
            self.finish(token, "incomplete", reason)

    def close(self) -> None:
        self._closed = True
        self.flush("shutdown")

    def _publish(self, token: str, r: _Trace, outcome: str, reason: str, phase: str) -> None:
        # Explicit schema: never serialize a Turn, message, exception, or dataclass wholesale.
        now = self.now()
        fields = {
            "schema": 1, "service": "mind", "event": "turn_timing",
            "trace_id": token, "voice_token": r.voice_token, "phase": phase,
            "first_ack_scope": ("typed_bridge_ack" if r.typed_ack is not None else
                                "typed_echo" if r.typed_echo is not None else
                                "voice_started_ack" if r.voice_started is not None else
                                "not_observed"),
            "outcome": outcome if outcome in _OUTCOMES else "other",
            "reason": reason if reason in _REASONS else "other",
            "origin_scope": r.origin_scope, "observation_scope": "mind_local_monotonic",
            "pipeline_outcome": r.pipeline_outcome, "deadline_decision": r.deadline,
            "deadline_outcome": r.deadline_outcome,
            "planning_ms": r.planning_ms, "turn_queue_ms": r.queue_ms,
            "turn_queue_outcome": r.queue_outcome,
            "delay_ms": r.delay_ms, "hold_ms": r.hold_ms, "compose_ms": r.compose_ms,
            "typing_ms": r.typing_ms, "send_lock_ms": r.send_lock_ms,
            "bridge_ack_ms": r.bridge_ack_ms, "model_attempts": r.attempts,
            "model_queue_ms": _sum(r.attempt1.queue_ms, r.attempt2.queue_ms),
            "readiness_admission_ms": _sum(r.attempt1.admission_ms, r.attempt2.admission_ms),
            "llm_client_call_ms": _sum(r.attempt1.call_ms, r.attempt2.call_ms),
            "model_attempt1_outcome": r.attempt1.outcome,
            "model_attempt2_outcome": r.attempt2.outcome, "typed_acks": r.typed_acks,
            "origin_to_typed_ack_ms": _ms(r.origin, r.typed_ack),
            "send_to_typed_ack_ms": _ms(r.typed_sent, r.typed_ack),
            "origin_to_echo_ms": _ms(r.origin, r.typed_echo),
            "origin_to_voice_send_ms": _ms(r.origin, r.voice_sent),
            "send_to_started_ack_ms": _ms(r.voice_sent, r.voice_started),
            "origin_to_started_ack_ms": _ms(r.origin, r.voice_started),
            "send_to_terminal_ack_ms": _ms(r.voice_sent, r.voice_terminal),
            "origin_to_terminal_ack_ms": _ms(r.origin, r.voice_terminal),
            "elapsed_ms": _ms(r.origin, now),
            "first_nonzero_output_ms": None, "first_nonzero_output_scope": "not_observed",
            "physical_audible_ms": None, "physical_audible_scope": "not_observed",
            "suppressed_count": self.suppressed,
        }
        try:
            self._emit(fields)
        except Exception:
            pass  # telemetry failure cannot change work, ACKs, or shutdown
