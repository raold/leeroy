# Optional typing cadence

`[talk] realism = true` enables bounded cadence for generated typed member turns.
The default is false; a malformed realism value leaves this feature off. Other
keys in the shared talk table do not disable it. Read at mind start and each login;
no migration or native rebuild is needed.
Templates, signed text, raid callouts, urgent turns, kill/wipe reactions, hotmic and external transports
keep their existing timing. This does not change prompts or voice delivery.

The speed prior uses the persona's typing habit, authored age and a stable guid seed.
A measured numeric voice fingerprint can adjust it slightly; a seeded fingerprint
does not count as measured speed. These are authored cadence choices, not measured
keystrokes or claims about real demographic groups. Speed stays within 25–100 WPM.
Length, jitter, long-reply thinking time and small inter-line gaps determine the wait.
Altered-state delay multipliers remain in effect. Waits and correction expiry use
monotonic elapsed time, so a wall-clock adjustment cannot extend them. The existing
typing dots refresh at least every ten seconds throughout the wait.

An optional natural-clause burst has at most three lines and a ninety-second
per-member cooldown. Combat and wipe silence/debrief suppress it. Rare corrections
use six reviewed adjacent-key nonwords, protect character/owner names and skip
links and mentions. Authored and altered-state styler typos remain intact; a line
already changed by styling gets no additional planned correction. A
member earns at most one correction per fifty confirmed normal lines; a two-percent
random chance and the allowance both have to pass. A correction reserves its
allowance before waiting and cannot earn allowance itself. Restart/eviction resets
allowance to zero. No content is stored for this accounting.

The `*word` correction follows the acknowledged typo by 5–10 seconds, through the
ordinary transport and rate checks. If the member becomes AFK, offline, invalid or
in combat, or the turn is cancelled, the correction is abandoned. It is never
queued for later. Every confirmed line advances a per-member sequence across both
media: a waiting follow-up or correction is dropped if a newer line has landed.
Skipped burst lines leave dropped utterance receipts. The original correctly spelled
words remain the repeat/history input, immediately after each normal line. Corrections
remain visible in delivery logs and the Discord feed, but are excluded from own-line
history (including restart) and the next game/Discord prompt. A burst and a correction
together never exceed three lines.

All new waits, including thinking, gaps and a possible correction, share a budget:
at most twelve extra seconds for a whisper/DM, thirty for other typed channels,
relative to the original typing formula for the styled turn. The ordinary first-line
deadline can shorten this further. Existing send-lock contention and bridge ACK
latency are outside this typing budget and are unchanged.

Game-busy evidence uses only native reports: boss participants, current dungeon/
raid map and a zone transition within twenty seconds. There is no new probe and no
claim to observe generic combat, movement or taxi state. Fight evidence is cleared
on session end/start so an offline boss-end event cannot leave stale combat at relog. A busy game whisper gets a
small pause and only its first complete styled line. Discord never inherits game
busy/AFK delays or advances game activity. Its enabled cadence may turn a long
message into separate short posts, still in the same channel/thread or DM pair.

Run `python -m pytest -q tests/test_timing_realism.py` for the fake-clock checks.

# Discord message length

`[talk] discord_words` (an integer 0-200, default 90; owner 2026-11-13, "Long and chatty") is
about the most words a member's message on the guild's Discord runs to: a DM (`dm:<guid>`) or a
channel (`discord:<id>`), whether it answers his post, opens or continues a server conversation,
opens a DM, riffs on General or answers late on her own time. It is read with the rest of
`[talk]`, at mind start and at each login; an invalid value is the default, logged once
(`talk_discord_words_invalid`). No migration or native rebuild. Code: `discord_length.py`.

- The instruction (`prompt.DISCORD_LENGTH`) takes the place of the template's "one short line"
  (or "in a few words", or an explicit whisper's "a line or two"): write it the way you would
  in a Discord DM thread (Discord chat, in a channel), usually a few sentences, a short
  paragraph when there is something to say (at most about that many words), a single quick
  sentence when that is all it needs, one paragraph. It ends the template's own text, before
  its extra (his post, a server seed), so what she answers stays last, with the Lately line and
  her tail after it as before. The system message keeps her game line, so Ollama's cached
  prefix is the same in both media; the instruction says Discord is the exception.
  Banter on Discord drops its own "One short line." for it.
- Room: the request's `num_predict` is `max(80, ceil(words * 1.6) + 24)` (168 at 90), for that
  request only (`preset_for`); `num_ctx` (4096) and the blank-line stop never change, and
  `LIVE_BUDGET` stays 1800 input tokens.
- A reply Ollama cut at num_predict (`done_reason` "length") goes back to its last sentence end
  (".", "!", "?", "…" or an emoji); with none it is kept as it is. Each trim logs
  `discord_line_trimmed` with word counts, never the words.
- After the model: the "long" tell allows 1.25 times the words (its retry asks for "at most about"
  that many), the typing lays the message out as one paragraph of up to that many words (never
  her game line's cap) and at most the store's 2000 characters, and "is typing" lasts at most
  25 seconds, realism's cadence and a correction included. The filters, the guards, memory
  extraction and the console take the longer line as it is.
- 0 is the rollback: one short line on Discord, with the prompts, room, typing and retries as
  before. A game turn never changes, at any value.
