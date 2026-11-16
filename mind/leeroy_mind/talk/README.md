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

With `[talk] texture` on, a slip the styler made in a word that matters (below) is corrected at
the member's own share instead: about 12% of such slips for a lower-case typer with no end
punctuation, up to 32% for a proper-case typer with full stops. That correction comes 2-6
seconds after the line, spends none of the allowance above, and needs the same safe, unhurried,
multi-line-capable turn as any correction. The six-word list and its allowance stay for lines
with no slip of their own.

# Typing texture

`[talk] texture = true` (default false; anything but a boolean is off; read at mind start and
each login; no migration or native rebuild) types each member's lines the way people type in
chat, around her declared typing (`persona_core.json` `typing`), with nothing new in the
persona. `talk/style.py` `texture_of` fixes her habits from her character name and her declared
case and end punctuation (a lower-case, no-full-stop typer is the most casual, a proper-case,
full-stop typer the most careful):

- **Apostrophes.** Contractions lose theirs ("dont", "im", "thats") at her rate, about 90% of uses
  for the most casual and 3% for the most careful; one member in ten keeps most of hers whatever
  her case. "we're", "we'll", "he'll", "she'll", "we'd" and "she'd" always keep theirs: without
  it each is another word.
- **Run-ons.** Her full stops between fragments run on at her rate ("x, y", "x y", now and then
  "x and y" between two clauses), short fragments most readily; "?", "!" and "..." stay, and one
  member in eight keeps her full stops. A fragment of one or two words always takes a comma
  ("stop, pull the boss", never "stop pull the boss"). A run-on is punctuation, so the line's
  stored words show it too; an ordinary word after it loses its capital, "I", names and acronyms
  keep theirs.
- **Shorthand.** Her own list applies use by use, not every time: a real entry ("u", "brb",
  "dps") at 55-95%, an entry no one types ("tht", "wt", "nt", "2") at about a third of that.
  A casual typer (at least mixed case with some end punctuation) also has two to four real
  shorthands of her own from u, ur, rn, idk, tbh, ngl, imo, ofc, prob, def, w/e, bc, tho,
  gonna (only before a verb) and wanna, used some of the time. "you're" never becomes "u're"
  (with the switch off too).
- **Spelling habits.** Two or three per member ("definately", "alot", "wierd"; casual typers can
  also have "your" for "you're", "then" for "than", or a finger habit such as "teh" or
  "jsut"), each used some of the time. None for a persona with `typo_rate` 0.
- **Slips.** At most one per line, in some lines: about 5 x `typo_rate` of the lines of six words
  or more (x the drink multiplier, at most 60%), fewer in shorter ones, 0.8 x that on Discord.
  At a keyboard: a neighbouring key, a letter dropped (often the last), doubled or swapped
  ("teh", "ponit"), a space left out ("inthe") or hit early ("abou tthe"); short words and the
  first letter now and then; never another common word. On Discord (a phone) a third are
  autocorrect's wrong real words ("form", "wife"). Names, item links, marks and shorthand never
  slip, and no slip ever types a swear, sexual word or slur she did not write (`NEVER_TYPED`:
  "sec" never becomes "sex", nor "count" a worse word; the switch-off typo path keeps this rule
  too). A slip in a word of four letters or more that is not a little word, or an autocorrect
  one, may be followed by "*word" (with `[talk] realism` on, above).
- **Kept as written.** A quoted phrase (straight or curly quotes) and a web address get no
  shorthand, spelling habit, apostrophe drop, run-on, slip or autocorrect, just like item links.

Measured on the live corpus (2026-11-15, read-only; members' stored lines rebuilt as drafts and
typed again), against his own lines:

| | him, game | members before | members after | him, Discord | members before | members after |
|---|---:|---:|---:|---:|---:|---:|
| lines with a contraction's apostrophe | 4.8% | 36.5% | 14.6% | 8.9% | 67.1% | 37.5% |
| lines with one dropped | 22.5% | 2.7% | 24.1% | 43.9% | 4.4% | 44.7% |
| lines with an inner ". " | 20.9% | 40.7% | 15.7% | 50.4% | 79.6% | 53.5% |
| lines with a comma | 25.9% | 9.0% | 28.8% | 43.1% | 20.4% | 47.9% |
| "u're" per 100 lines | 0 | 3.2 | 0 | 0 | 6.5 | 0 |
| lines with a typo | 18.0% | 15.3% | 18.4% | 18.7% | 33.8% | 18.8% |
| lines with two or more | 3.3% | 1.2% | 0.8% | 8.9% | 10.7% | 1.9% |

About 9.5% of game lines then carry a slip worth correcting, and about 1.4% of lines would be
followed by a "*word". The persona-declared "u" stays a persona choice (about 30% of "you"s).

Run `python -m pytest -q tests/test_typing_texture.py tests/test_timing_realism.py`.

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
