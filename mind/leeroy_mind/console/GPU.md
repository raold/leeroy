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
The pause first asks a running night and photo booth pass to stop, as your login
does, and only then closes the model gate. The night resumes later as an
interrupted run without spending another attempt, and the booth records a
preemption with no failure cooldown. A model step that still meets the closed
gate counts as the same interruption, never as a failure. Photo workers and their
child processes finish cleanup before the unload. Bridge events, Realm
bookkeeping, the console and voice connections continue. Existing voice GPU
ownership remains recorded so an active voice worker cannot accidentally admit
reflection after Resume. Voice GPU use first reported during the pause is not
recorded; the next voice status after Resume records it (voice holds VRAM only in
your WoW session, which a pause excludes).

The control waits up to 60 seconds for that cleanup. When the night is still
stopping then, it answers "GPU use is paused and will finish once the night has
stopped." Model use is already paused at that point; the unload follows in the
background once the night has stopped, and the button offers **Retry Free GPU**
until it has.

An offline raid that falls due during a pause waits: it neither ends the pause
nor fails. The day is not blocked and no attempt is spent; mind.log says
`offline_raid_waiting reason=gpu_paused` once, and after Resume the raid starts
by its usual rules.

The control refuses while you are in WoW or an offline raid or other protected
GPU job holds a lease. Its explanation names the relevant Raid or Training tab.
No training job takes a GPU lease today (lab trials and benchmarks are native
body jobs), so the Training refusal is only a defence. It also refuses while your
WoW presence is unknown: before the first hello after a mind start, while the
bridge is down, or when the session cannot be read ("Can't tell whether you're in
WoW yet; try again in a moment."). With the worldserver down, stop the mind
instead; the console then unloads directly.
An unload failure leaves model use paused and offers **Retry Free GPU**; it does
not claim that the GPU is free. The normal status feed refreshes residency in the
background with a short timeout, so a slow Ollama cannot block the status strip.
The pause itself reads residency with the admin's normal timeout.

With the mind stopped, the button only unloads Ollama directly. It preserves any
stored pause marker and does not create or write `mind.db`. The console reserves
its process-control lock and the mind's OS lock during the unload, so a concurrent
Start cannot race it. The fallback accepts only loopback URLs and refuses HTTP
redirects. `--no-control` prevents both pause and resume.

Ops Switches and Tonight show the pause time. Health reports a note while paused
and, after 24 hours, when the last night finished; a deliberate pause is never a
failure.

## Operator notes

Native `mod-ollama-chat` is outside the mind's pause. A native bot event can still
load a model. When residency is observed while paused, **Retry Free GPU** unloads
it again. Other external GPU applications and the voice service's own models are
also outside this Ollama control. The stored pause ends on Resume, login or an
owner Discord post; starting the mind alone does not clear it.

The operator commands open their own Ollama client and do not read the stored
pause: `realm plan rivals` (beside a running mind) and, with the mind stopped,
`nightly run`, `selfies try`, `personas generate` and the library build each load
a model while `gpu:paused` is set. Run them after Resume, or choose Free GPU again
afterwards.

`gpu` is an existing-key control operation with exactly one argument:
`{"action":"status"}`, `{"action":"pause"}` or `{"action":"resume"}`.
The console exposes GET and POST `/api/gpu` behind its usual key, Host, Origin and
fetch-site gates. Status rides the existing status/SSE loop; no new polling loop
is added. Logs record counts, VRAM MB and the fixed resume reason, never chat text.
