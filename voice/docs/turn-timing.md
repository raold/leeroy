# Voice turn timing

The voice service times each line with the events it already logs, not with a
collector of its own. They are content-free: no text, reference path or audio.

| Event | Fields | Observation |
| --- | --- | --- |
| `segment_ready` | `id`, `guid`, `segment`, `engine`, `synth_ms`, `audio_ms` | One segment is ready for the mixer. `synth_ms` is the engine call, including a fallback to the member's Kokoro voice, plus native processing, on the worker's monotonic clock. `audio_ms` is the segment's length. `engine` is `stock`, `kokoro` or `<engine>/cpu` or `/cuda`. |
| `line_started` | `id`, `guid`, `tier`, `engine`, `wait_ms` | The scheduler started the line. `wait_ms` runs from the Speak's arrival to that start: queueing, the first segment's synthesis and processing, and the wait for the mixer. It is on the service clock, the wall clock that `expires_ms` and scheduling use, so a clock step during the wait shows in it. `engine` is what made the first segment. |
| `line_dropped`, `synthesis_failed`, `engine_fallback` | `id`, `reason`, `error` or `why` | A line that never started, or a segment that lost its engine. |

The mind's `turn_timing` records (see `mind/docs/turn-timing.md`) carry
`voice_token`: the lowercase SHA-256 hex digest of the Speak ID's UTF-8 bytes.
These events log that ID as `id`, so hashing it joins the two logs. Speak IDs
are not unique across runs, so a join must reject ambiguous reuse. Durations
from the two processes are never subtracted from each other.

A started line is a scheduler start, not first sound: neither the first
nonzero output at the device nor physical audibility is observed.
