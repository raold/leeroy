# Voice turn timing

The voice service emits content-free `turn_timing` events with `schema: 1` and
`service: "voice"`. Each received Speak has an opaque local `trace_id` and a
`voice_token`: the full lowercase SHA-256 hex digest of the unchanged Speak ID's
UTF-8 bytes. The mind uses the same digest. It correlates observations; it is not
an anonymity guarantee. Protocol messages and IDs are unchanged.

An observed line emits at most one `first_handoff` milestone and one `terminal`
summary. Duplicate Speak receipts have separate local traces, with reason
`duplicate`; they do not replace the existing line's trace. A repeated token is
therefore not necessarily an unambiguous distributed join.

All durations are finite milliseconds from one injected monotonic timer. The
service's existing wall clock still decides expiry and scheduling. No remote or
wall-clock timestamp is subtracted from a timing observation.

| Fields | Observation |
| --- | --- |
| `receive_to_admission_ms`, `receive_to_enqueue_ms` | Receive through successful scheduler submission and the boundary before enqueueing the line's existing batch of worker tasks. These share one observation. |
| `enqueue_to_first_worker_ms` | Batch enqueue to first worker entry. Later segment waits include time behind earlier synthesis, so they are not summed as an independent queue duration. |
| `first_wrapper_synthesis_ms`, `wrapper_synthesis_total_ms` | First segment and accumulated engine-wrapper calls, including existing expressive fallback. This is not pure model inference. Stock clips have null synthesis times. |
| `first_native_processing_ms`, `native_processing_total_ms` | First segment and accumulated existing native audio processing, including validation. |
| `first_worker_post_to_loop_ms`, `worker_post_to_loop_total_ms` | Worker observation immediately before the existing callback post through loop callback dispatch. |
| `receive_to_first_ready_ms` | Receive through the first successful segment callback. |
| `first_ready_to_handoff_ms`, `receive_to_handoff_ms` | Wait for the first mixer handoff after `mixer.apply` succeeds, identified by the existing `Played.started` event. A stalled line's resumption does not repeat it. |
| `receive_to_terminal_ms` | Receive through the existing terminal Played outcome, or telemetry expiry/shutdown. |

`source` is `normal`, `cpu`, or `stock` after enqueue; normal includes the current
normal-thread engine and its existing fallback. Segment counts and totals cover
receipts consumed while the trace is active. A terminal summary does not wait for
cancelled workers; late completions are ignored. Totals can overlap playback and
other stages and must not be added into a disjoint wall-time decomposition.

`deadline_scope` reports an observed existing decision. `voice_before_start`
means the scheduler returned `expired`. All other cases are
`no_decision_observed`. In particular, the existing `engine_error` reason does
not distinguish the scheduler's segment timeout from an engine failure, so it
does not establish a deadline decision. Telemetry does not impose deadlines or
compute lateness.

`first_nonzero_output_ms` and `physical_audible_ms` are always null with scope
`not_observed`. Handoff can precede even the first device callback. No mixer,
callback, device, leading-silence scan, or blackbox instrumentation is added.

The loop owns at most 256 fixed-size records, with 120-second age expiry on
existing event/tick paths. It retains only safe scalar fields and hashed IDs,
never speech, identities, paths, jobs, references, or audio. Workers construct
immutable scalar receipts and use the existing `_post` callbacks; they do not
mutate or log the collector. Counts saturate at 2^31-1. Capacity skips observation
and increments `collector_suppressed`; no real work is refused. Expiry and
shutdown emit incomplete summaries and release state. Finished handles never
reopen, and there is no completed-history cache. Failed emission is counted and
cannot change playback, messages, fallback, cancellation, or cleanup.

The focused tests in `tests/test_turn_timing.py` use fake independent clocks,
gated workers, native arrays and fake output pumps. Existing service, scheduler,
native-audio, CPU fallback, expressive, wire, and logging suites remain the
compatibility checks. No live device, model, or physical audibility claim is
established by these tests.
