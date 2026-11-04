# Turn timing observations

`turn_timing` logs use schema 1, service `mind`, an opaque local `trace_id`, and
`voice_token` when the existing voice writer accepted a Speak. The token is the
lowercase SHA-256 hexadecimal digest of the UTF-8 Speak ID, identical in voice.
It is a correlation handle, not an anonymity guarantee. The wire protocol is unchanged.

All durations end in `_ms` and use an independently injected process-local monotonic
timer. Existing wall-clock deadlines, `expires_ms`, store updates, and `llm_s` retain
their meaning. Never subtract mind and voice timestamps or infer transport latency
from a residual. `origin_scope` is `mind_human_chat_entry` for human reply planning
(after storage/world dispatch), otherwise `turn_admission`. Neither includes STT.

The terminal record includes planning, sequential turn queue, deliberate delay,
voice hold, compose/retrieval, typing, send-lock wait, and bridge ACK wait. Model
queue time includes scheduler dispatch until closure entry. Readiness admission
is separate from the LLM client call, which includes client/HTTP work and is not
GPU-only inference. Two attempt slots supply aggregate timings and fixed outcomes.
Cancelled/failed phases retain observed partial waits; absent phases remain null.
Templates have no model timings. `deadline_decision` names the last observed policy
check, preserving the first expiry decision; `deadline_outcome` distinguishes
`within_deadline`, `expired`, and `not_observed`. No new deadline is enforced.

There is at most one `phase=first_ack` milestone and one `phase=terminal` per
admitted trace. `first_ack_scope` distinguishes bridge ACK, echo observation, and
voice started-ACK receipt. A typed ACK timeout remains pending until the existing
echo settlement, telemetry TTL, or session end; it is never reported as certainly
unsent. Successful writer acceptance is not an ACK. Voice started and terminal
ACKs retain independent timing even though the existing unanswered/store state
stops waiting on started. Disconnect releases timing without changing that store.
Concurrent duplicate wire IDs end timing as ambiguous (`duplicate_id`); late ACKs
never allocate records. Existing IDs are not globally unique, so offline joins
must also reject ambiguous reuse across runs.

`send_to_started_ack_ms` and `origin_to_started_ack_ms` end at local receipt of
Played.started. This is a scheduler-start acknowledgement, not first sound.
`first_nonzero_output_ms` and `physical_audible_ms` stay null with explicit
`not_observed` scopes. No callback instrumentation or PCM inspection is added.

The loop collector retains at most 256 active records for 120 monotonic seconds
from admission, with expiry on existing tick/event paths and no completed cache.
Each record holds scalar fields and two fixed attempt slots; it retains no Turn,
text, prompt, persona, identity, path, channel, connection, or message. Unknown
reasons become `other`. Capacity skips telemetry, and timer/sink failure cannot
change real admission, sends, cancellation, ACKs, or shutdown. Unstarted queued
turns are released by the existing task completion path without adding gameplay
rows. Session end flushes incomplete traces and permits the next session; explicit
collector close also prevents future admission.
