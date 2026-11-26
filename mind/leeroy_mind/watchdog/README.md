# Event-loop stall recorder

`[watchdog] enabled` is on by default (`false` in `mind.toml` and a mind restart turn it off).
Ops lists **Capture event-loop stalls**. Malformed or unknown settings leave it off. No migration or native rebuild is needed. This does not
restart services, interrupt a call, use a model, or turn other features on.

The `run` service arms faulthandler's native one-shot timer for two seconds and
resets it every 250 ms from the event loop. A blocked loop therefore leaves a
trace in `logs/mind-loop-stalls.log`, even when the blocking call holds the GIL.
It includes the main thread and other threads; the opening record names the
main thread ID so it can be matched to the trace. Frames include file, function
and line number, without source text, locals, prompts or chat. A recovered loop
adds a UTC timestamp and delay to that file and logs `loop_watchdog_delayed`
with `delay_ms` in `mind.log`.

There is one dump per continuous stall. After recovery, recording is suppressed
for 60 seconds, so repeated stalls cannot flood disk. During that cooldown a
second stall can go unrecorded. A healthy event loop waiting asynchronously for
network/model work continues its heartbeat and produces no dump. These records
identify where Python was blocked; they do not prove a remote service failed.

The stack log rotates after it reaches 2 MiB, at startup or after a stall, keeping
three backups. A single dump can exceed the limit. Rotation and shutdown cancel
and join the native timer before closing its descriptor. File/timer failures
disable this diagnostic and emit a reason code rather than stopping the mind.
The process-global delayed traceback timer has one owner; do not install another
`dump_traceback_later` user in this service. Existing fatal fault handlers remain
unchanged. See [Python's faulthandler documentation](https://docs.python.org/3.14/library/faulthandler.html#dumping-the-tracebacks-after-a-timeout).

Tests use a fake timer for heartbeat/cooldown/lifecycle and isolated subprocesses
for real blocked-loop/GIL-held dumps. Run `python -m pytest -q tests/test_loop_watchdog.py`.
For rollback disable the switch, and remove the `[watchdog]` table before using
older code whose main config loader does not recognize it.
