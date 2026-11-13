# Free GPU

The top-bar **Free GPU** button pauses the mind's model work and unloads every
model Ollama reports, including embeddings and models the mind did not load.
It reports the sum of their reported VRAM sizes. This is model residency freed,
not a measurement of all applications' GPU memory.

The pause is stored in `mind.db` as `gpu:paused` and survives a mind restart.
It ends only with **Resume**, your WoW login, or your own Discord post or DM.
Reactions, edits, timers, voice connection changes and the quiet window do not
resume it. A cold Discord answer waits for the live model to load.

While paused, autonomous conversations, DM openers, banter and model requests
stop. Queued extraction waits, including work interrupted during a request.
The night is cancelled through its ordinary resumable cleanup without spending
another attempt. Photo workers and their child processes finish cleanup before
the unload. Bridge events, Realm bookkeeping, the console and voice connections
continue. Existing voice GPU ownership remains recorded so an active voice
worker cannot accidentally admit reflection after Resume.

The control refuses while you are in WoW or an offline raid or other protected
GPU job holds a lease. Its explanation names the relevant Raid or Training tab.
An unload failure leaves model use paused and offers **Retry Free GPU**; it does
not claim that the GPU is free. The normal status feed refreshes residency in the
background with a short timeout, so a slow Ollama cannot block the status strip.

With the mind stopped, the button only unloads Ollama directly. It preserves any
stored pause marker and does not create or write `mind.db`. The console reserves
its process-control lock and the mind's OS lock during the unload, so a concurrent
Start cannot race it. The fallback accepts only loopback URLs and refuses HTTP
redirects. `--no-control` prevents both pause and resume.

Ops Switches and Tonight show the pause time. Health reports a note while paused
and another explanation after 24 hours; a deliberate pause is never a failure.

## Operator notes

Native `mod-ollama-chat` is outside the mind's pause. A native bot event can still
load a model. When residency is observed while paused, **Retry Free GPU** unloads
it again. Other external GPU applications and the voice service's own models are
also outside this Ollama control. The stored pause ends on Resume, login or an
owner Discord post; starting the mind alone does not clear it.

`gpu` is an existing-key control operation with exactly one argument:
`{"action":"status"}`, `{"action":"pause"}` or `{"action":"resume"}`.
The console exposes GET and POST `/api/gpu` behind its usual key, Host, Origin and
fetch-site gates. Status rides the existing status/SSE loop; no new polling loop
is added. Logs record counts, VRAM MB and the fixed resume reason, never chat text.
