# Voice notes in Discord DMs

Members can deliver a short DM as audio in their own voice. Its message and chat line keep
the exact words, so recall, guards, reward evidence and the one-mind pair timeline read the
same private conversation. The owner can record and preview a note before sending it back.
Channel and group posts do not accept voice notes.

In Comms, open a member's DM and choose **Record voice**. Stop to preview it, then send or
discard it; recording stops automatically at 60 seconds. Escape, leaving the conversation,
or hiding the page cancels an active recording and releases the microphone. A failed upload
keeps the preview for retry. Recording permission requires a secure browser context;
loopback localhost is supported. Voice previews stay in memory, never browser storage.

Received notes have play/pause, seeking and a transcript toggle. A member's transcript starts
collapsed; his starts expanded. Ordinary feed updates preserve playback and keyboard focus.
Leaving the conversation stops playback. Hide photos does not hide voice notes.

## Operator notes

Add to `mind.toml`, then restart the mind and console:

```toml
[voice_notes]
enabled = true
share = 0.15
per_member_day = 3
guild_day = 12
max_words = 40
```

The feature defaults off. An invalid table also means off. Allowed ranges are share 0–0.5,
member cap 0–10, guild cap 0–50 and word limit 5–60. The caps use the local calendar day,
share the existing `server:day:<date>` record, and fail closed if its counters are malformed.
The attachment and its counters commit with the message, including concurrent sends.

For this release, restart **voice, mind and console together**: both wire copies add
`render`, `rendered`, `transcribe` and `transcript` (20 message types, 11 to voice and 9 to
mind). `Status.render` independently reports offline rendering readiness; `Status.ok`
continues to describe room audio. New readers accept an old status without `render`, which
defaults false. An old voice service cannot handle the new requests, so audio needs the new
service even though an unsuccessful render still falls back to typed text.

Migration **0040_discord_voice.sql** adds the six-column `discord_voice` attachment table.
No worldserver rebuild is needed. This feature never enables itself or changes live config.

**Audio is retained indefinitely.** Nothing deletes rendered notes, failed-render temporary
files, uploaded recordings, unposted staged files, or files for deleted messages. Uploads
remain under `discord/incoming-voice/`; adopted and rendered notes live under
`discord/voice/` in the mind data directory. Adoption copies the bytes and leaves a claim
marker so the token cannot be posted twice. The disk therefore continues to grow.

## Delivery and transcription

A member must be online in Discord, have a voice spec, have an already held live lease and
a usable connected voice service, and have room under both daily caps. Eligible lines have
3–40 words by default, excluding emoji-only content and item-link placeholders. A stable
SHA-256 draw from member and turn IDs scales the share by her talkative dial; zero never
chooses audio. Presence, lease and caps are checked again after rendering.

Rendering waits behind queued live room synthesis and never plays, ducks, enters the mixer,
adds microphone or room effects, or takes a GPU lease. An already admitted CUDA expressive
engine can render on that same synthesis queue; otherwise the member's native Kokoro blend
is used. CPU expressive is reserved for its separately admitted room worker, so notes use
Kokoro rather than compete with a room job on that worker. This is a deliberate admission
restriction. OGG Vorbis is selected once at service startup, or 16-bit WAV if Vorbis is
unavailable; completed files are renamed atomically. The mind waits at most 20 seconds,
then sends the original text. Comms shows “recording a voice message” during the wait.

His upload must be mono, 16 kHz, 16-bit PCM WAV, 0.2–60 seconds and at most 2 MiB. The console
checks key, Host, Origin and same-origin fetch headers, validates the whole RIFF structure
before writing, and allows four uploads a minute and 100 a local day. Retained staged files
rebuild the daily quota after a console restart. Playback uses the same link/junction and
file-identity checks as photos, with private 60-second caching; deleted posts return 404.

The post appears immediately as “(voice message)”. A separate 20-second STT request uses
the existing recognizer and hotwords without opening the microphone or changing push-to-talk
state. A successful transcript replaces both message and chat text. The ordinary “don't
tell” screen applies, including secret private-pair memories. Failed transcription leaves
the bubble playable and tells the answering member only that she cannot play it right now.

Only the completed success or failure is handed to `DiscordTalk.on_his_post`, exactly once
in the running mind. Its ordinary own-time rules determine when she answers; no pending
placeholder creates a deferred-answer receipt. Durable pending/ready receipts resume an
interrupted transcription or handoff after restart. An interrupted transcription waits for
the voice service to connect (it loads its models after a restart) and is sent then; only
after five minutes without a connection does it settle as a failed transcription. Deleted
messages are not handed over, and a completed handoff has no recovery receipt left. Recovery
processes one receipt at a time; switching the feature off leaves the retained receipts
untouched.

Logs contain IDs, sizes, durations and reason codes, never transcript text or names.
