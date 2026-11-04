"""Bounded loop-owned observations of existing voice work, never playback policy.

Workers pass frozen scalar receipts through the service's existing callbacks. Only the
event loop owns records and emits events. Handoff is observable; sound at the device is not.
"""
from collections.abc import Callable
from dataclasses import dataclass
import hashlib
import math
import secrets
import time

from .logs import log

MAX_ACTIVE = 256
MAX_AGE_S = 120.0
MAX_COUNT = 2**31 - 1
SOURCES = frozenset({"normal", "cpu", "stock"})
OUTCOMES = frozenset({"started", "done", "cut", "dropped", "refused", "incomplete"})
REASONS = frozenset({"none", "offline", "no_voice", "muted", "empty", "duplicate",
                     "invalid_id", "not_his_channel", "raid", "expired", "after_dropped",
                     "cancelled", "moved", "engine_error", "mind_gone", "shutdown",
                     "telemetry_expired"})


def voice_token(speak_id: str) -> str:
    """Shared v1 join: full lowercase SHA-256 of the unchanged ID's UTF-8 bytes."""
    return hashlib.sha256(speak_id.encode("utf-8")).hexdigest()


def read_timer(timer: Callable[[], float]) -> float | None:
    """A broken observational clock must not interrupt synthesis or playback."""
    try:
        value = timer()
        return value if math.isfinite(value) else None
    except Exception:
        return None


def _ms(start: float | None, end: float | None) -> float | None:
    if start is None or end is None:
        return None
    value = (end - start) * 1000.0
    return value if math.isfinite(value) and value >= 0 else None


def _add(total: float | None, value: float | None) -> float | None:
    if value is None:
        return total
    result = (total or 0.0) + value
    return result if math.isfinite(result) else total


@dataclass(frozen=True, slots=True)
class WorkReceipt:
    """Only scalar local clock readings; no job, text, audio, or error payload."""
    source: str
    worker_at: float | None
    synth_end: float | None
    process_at: float | None
    process_end: float | None
    posted_at: float | None


@dataclass(slots=True)
class _Record:
    token: str
    received_at: float
    source: str = "not_observed"
    admitted_at: float | None = None
    enqueued_at: float | None = None
    first_worker_at: float | None = None
    first_ready_at: float | None = None
    first_ready_recorded: bool = False
    handoff_at: float | None = None
    handoff_recorded: bool = False
    first_synth_ms: float | None = None
    first_process_ms: float | None = None
    first_dispatch_ms: float | None = None
    synth_total_ms: float | None = None
    process_total_ms: float | None = None
    dispatch_total_ms: float | None = None
    segment_count: int = 0
    failed_segment_count: int = 0


def _emit(fields: dict) -> None:
    log("turn_timing", **{key: value for key, value in fields.items() if key != "event"})


class TurnTiming:
    """At most 256 active fixed-size records, expired on existing loop paths.

    Handles belong to one receipt of Speak, not its reusable wire ID. Finishing removes
    the handle; late callbacks can never allocate records or produce another terminal.
    Counter snapshots saturate. There is no completed-history cache.
    """

    def __init__(self, *, timer: Callable[[], float] = time.monotonic,
                 emit: Callable[[dict], None] = _emit):
        self._timer = timer
        self._emit = emit
        self._active: dict[str, _Record] = {}
        self._closed = False
        self.suppressed = 0
        self.late_updates = 0
        self.emit_failures = 0

    @property
    def active_count(self) -> int:
        return len(self._active)

    def begin(self, speak_id: str) -> str | None:
        now = read_timer(self._timer)
        self._expire(now)
        if self._closed or now is None or len(self._active) >= MAX_ACTIVE:
            self.suppressed = min(MAX_COUNT, self.suppressed + 1)
            return None
        try:
            handle = secrets.token_hex(16)
            token = voice_token(speak_id)
        except Exception:  # unavailable entropy or an ID not encodable as UTF-8
            self.suppressed = min(MAX_COUNT, self.suppressed + 1)
            return None
        self._active[handle] = _Record(token, now)
        return handle

    def _get(self, handle: str | None, now: float | None) -> _Record | None:
        self._expire(now)
        record = self._active.get(handle) if handle is not None else None
        if handle is not None and record is None:
            self.late_updates = min(MAX_COUNT, self.late_updates + 1)
        return record

    def enqueued(self, handle: str | None, source: str) -> None:
        now = read_timer(self._timer)
        if (record := self._get(handle, now)) is not None:
            record.admitted_at = record.enqueued_at = now
            record.source = source if source in SOURCES else "other"

    def worked(self, handle: str | None, receipt: WorkReceipt, *, ready: bool) -> None:
        now = read_timer(self._timer)
        if (record := self._get(handle, now)) is None:
            return
        synth = (_ms(receipt.worker_at, receipt.synth_end)
                 if receipt.source != "stock" else None)
        process = _ms(receipt.process_at, receipt.process_end)
        dispatch = _ms(receipt.posted_at, now)
        if record.segment_count == 0:
            record.first_worker_at = receipt.worker_at
            record.first_synth_ms = synth
            record.first_process_ms = process
            record.first_dispatch_ms = dispatch
        record.segment_count = min(MAX_COUNT, record.segment_count + 1)
        if not ready:
            record.failed_segment_count = min(MAX_COUNT, record.failed_segment_count + 1)
        record.synth_total_ms = _add(record.synth_total_ms, synth)
        record.process_total_ms = _add(record.process_total_ms, process)
        record.dispatch_total_ms = _add(record.dispatch_total_ms, dispatch)
        if ready and not record.first_ready_recorded:
            record.first_ready_recorded = True
            record.first_ready_at = now

    def handoff(self, handle: str | None) -> None:
        now = read_timer(self._timer)
        if (record := self._get(handle, now)) is not None and not record.handoff_recorded:
            record.handoff_recorded = True
            record.handoff_at = now
            self._write(handle, record, now, "started", "none", "first_handoff")

    def finish(self, handle: str | None, outcome: str, reason: str = "") -> None:
        now = read_timer(self._timer)
        if (record := self._get(handle, now)) is not None:
            del self._active[handle]
            self._write(handle, record, now, outcome, reason or "none", "terminal")

    def expire(self) -> None:
        self._expire(read_timer(self._timer))

    def _expire(self, now: float | None) -> None:
        if now is None:
            return
        for handle, record in tuple(self._active.items()):
            if now - record.received_at >= MAX_AGE_S:
                del self._active[handle]
                self._write(handle, record, now, "incomplete", "telemetry_expired", "terminal")

    def close(self) -> None:
        self._closed = True
        now = read_timer(self._timer)
        records, self._active = self._active, {}
        for handle, record in records.items():
            self._write(handle, record, now, "incomplete", "shutdown", "terminal")

    def _write(self, handle: str, record: _Record, now: float | None,
               outcome: str, reason: str, phase: str) -> None:
        outcome = outcome if outcome in OUTCOMES else "other"
        reason = reason if reason in REASONS else "other"
        deadline = "voice_before_start" if reason == "expired" else "no_decision_observed"
        # Explicit allowlist: never serialize a job, receipt, or dataclass generically.
        fields = {
            "schema": 1, "service": "voice", "event": "turn_timing", "trace_id": handle,
            "voice_token": record.token, "phase": phase, "outcome": outcome,
            "reason": reason, "origin_scope": "voice_speak_receive",
            "source": record.source, "deadline_scope": deadline,
            "receive_to_admission_ms": _ms(record.received_at, record.admitted_at),
            "receive_to_enqueue_ms": _ms(record.received_at, record.enqueued_at),
            "enqueue_to_first_worker_ms": _ms(record.enqueued_at, record.first_worker_at),
            "first_wrapper_synthesis_ms": record.first_synth_ms,
            "wrapper_synthesis_total_ms": record.synth_total_ms,
            "first_native_processing_ms": record.first_process_ms,
            "native_processing_total_ms": record.process_total_ms,
            "first_worker_post_to_loop_ms": record.first_dispatch_ms,
            "worker_post_to_loop_total_ms": record.dispatch_total_ms,
            "receive_to_first_ready_ms": _ms(record.received_at, record.first_ready_at),
            "first_ready_to_handoff_ms": _ms(record.first_ready_at, record.handoff_at),
            "receive_to_handoff_ms": _ms(record.received_at, record.handoff_at),
            "receive_to_terminal_ms": (_ms(record.received_at, now)
                                        if phase == "terminal" else None),
            "segment_count": record.segment_count,
            "failed_segment_count": record.failed_segment_count,
            "collector_suppressed": self.suppressed,
            "collector_late_updates": self.late_updates,
            "collector_emit_failures": self.emit_failures,
            "first_nonzero_output_ms": None, "first_nonzero_output_scope": "not_observed",
            "physical_audible_ms": None, "physical_audible_scope": "not_observed",
        }
        try:
            self._emit(fields)
        except Exception:
            self.emit_failures = min(MAX_COUNT, self.emit_failures + 1)
