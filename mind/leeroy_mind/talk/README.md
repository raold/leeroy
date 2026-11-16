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

Since the second round (owner, 2026-11-13: channels a mix, "DMs max expressive"):

- **His DMs** take `[talk] dm_words` (0-200, default **200**; invalid is the default, logged once,
  `talk_dm_words_invalid`) and their own wording (`prompt.DISCORD_DM`): write as fully as you
  feel, say everything you want to say in your own voice, a whole paragraph when there is
  something to say. A config without `dm_words` falls back to `discord_words`.
- **Channels and group DMs** (`gdm:<id>`, a chat, not a pair) draw a length band once per turn
  (`Turn.discord_draw`, from the pipeline's own `_length_rng`, so compose never draws from
  `self.rng`): 55% a quick message of a few words or one sentence (at most 20 words,
  `prompt.DISCORD_QUICK`), 30% a couple of sentences (45, `DISCORD_FEW`), 15% the paragraph of
  `discord_words` (`DISCORD_LENGTH`), never over the setting (`discord_length.band`). The band
  sets the wording, the room, the typing, the "long" tell and the trim alike, and a silent answer
  is asked again for "a sentence", "a couple of sentences" or "a few sentences" by its band.
  Measured on the live install the first afternoon: channel messages ran a median 40 words
  (9% at 15 or fewer), against 9 before #271.

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

# Every message of his is answered

Owner, 2026-11-15: "make sure they actually reply something. doesn't have to be a profound reply
but something, and needs to add to the conversation." Code: `owed.py` (its docstring is the
full rule); no switch, no migration, no native rebuild.

- **Planning.** A remark of his gets one or two answers, never none (`select.REPLY_COUNT_REMARK`);
  every whisper is answered by its member (`WHISPER_REPLY_P` 1.0); when everyone who could answer
  spoke in the last minute (weight 0), one of them answers anyway; his hello in the login quiet
  keeps one answer; a Discord channel post with nobody online or idle draws from the rest of its
  audience (`discord_draw_widened`).
- **The owed answer** (`TurnMeta.owed_answer`: the first answer to each line or post, a whisper
  answer, an answer to his DM) passes the guild's pace, keeps a reviewed draft instead of being
  dropped, and has a 90 s (game) or 300 s (Discord) deadline. If its member cannot speak, the next
  answer to that line in the job takes the debt.
- **In the turn.** A silence, two drafts that kept a tell, a failed or late model request: one
  more plain ask (`OWED_HINT`, with the summons warning where her arc keeps it in the game); only
  fatal tells, an unbacked shared-history claim, a flirt at a member or such a summons block that
  draft (a repeated move or length no longer do: a long draft is cut at a sentence). Without a usable draft the answer is `fallback_line`, a short reaction to his
  message that asks something back. Logs `owed_rescue_line reason= how=model|fallback`.
- **The watchdog** (ticked by the app): a message still unanswered 45 s (whisper, voice), 60 s
  (game channels) or 150 s (Discord) after it, with nothing in flight, gets a rescue turn (at most
  two, 90 s apart): `owed_rescue reason=not_planned|dropped:<reason>|no_answer`. Messages older than
  15 min (game) or 30 min (Discord) are let go (`owed_given_up reason=too_old`).
- **Held, then answered just after** (`owed_held reason=`): his raid's boss fight and wipe silence
  (`raid`; `dead` for a /say nobody alive can answer yet), a Free GPU pause (`gpu_paused`), the
  whispered member AFK (`afk`), him offline for a game line (`human_offline`), a DM she answers on
  her own time (`own_time`), a group DM's pacing (`group_pacing`), a selfie request still being
  made (`photo_request`), his voice note still being transcribed (`voice_note`).
- **Never owed:** his bot commands and raid orders, emotes, addon pastes (one answer per paste
  burst already) and DBM/BigWigs/AFK auto-whispers; a line nobody with a persona can hear (an
  empty voice room, a whisper to someone offline or outside the cast: `owed_given_up
  reason=nobody_present|target_offline`); game lines from before a mind restart.
- **Health:** `owner-replies` counts his messages of the last 24 h with no member answer within
  5 minutes, by medium (a note from one, a FAIL from 10); counts only, never text.
