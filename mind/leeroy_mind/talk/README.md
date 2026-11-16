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

# DM prompts, owed answers and game names (2026-11-15)

All 18 last-resort trims logged since 11-14 were answers to his Discord DMs: her own 200-word
paragraphs filled "Your last lines", the DM's history was packed into a 1,100-character
instruction at 200 characters a message, and the trim emptied her memories first.

- **The DM's history is the Chat section.** `discord.talk.DiscordTalk.dm_chat` gives the answer
  turn (`TurnMeta.discord_chat`) the DM's last 12 messages, oldest first: his up to 600
  characters, hers as their first 30 words (`prompt.replay`), each with its photo note and age.
  Her own long lines are replayed the same way in "Your last lines" and in the elsewhere block;
  the repeat check still reads them whole. The DM's instruction keeps only where she is and his
  message. The copy check does not read the DM's history (as before: it had none).
- **A DM has its own budget**, `prompt.DM_BUDGET` = 2600 estimated tokens: num_ctx 4096 less a
  full `dm_words` reply (344) leaves room even if the real count runs 25% over the estimate.
  Every other turn keeps `LIVE_BUDGET` (1800). Measured on the 18 logged DM trims (a scratch
  copy, the logged section sizes): the same turns now come to about 1,600-2,200 with five
  memories and twelve history lines, so none reaches its last resort.
- **The trim keeps evidence over her own words.** The awareness card is first fitted without
  its claims part ("Your claims", kept whole); the last resort then empties her own last lines
  first, lets the claims go, keeps two memories (`PROTECTED_MEMORIES`) past the relationship
  cards, and only drops them and the rest of the card at the very end. `PromptParts.untrimmed`
  holds whole sections the trim never touches (for the persona's own "About you" block),
  rendered first in the user message.
- **An answer he is owed in private never vanishes for its tells** (his whisper or his DM:
  `Pipeline._owed_to_him`; in a channel the old rule stands). A draft whose tells are all soft and none of
  `DROP_ON_RETRY` is kept; when the retry is worse, it is sent (`say_owed_kept`). Otherwise one
  more attempt asks for a sentence or two (`OWED_HINT`), and if that fails too she sends a
  holding line (`HOLDING_LINES`, never one she just said; `say_owed_holding`). An owed Discord
  answer over its length is cut back to a sentence end (`discord_length.fit_words`) instead of
  being asked again. A second silence still stands; other turns keep the old rule.
  Seen live 11-15: her first draft to "leave him for me" was asked again for a repeated move and
  its length, the retry asked him over, and both were dropped.
- **Game names** (`talk/gamenames.py`, the soft tell "game name", dropped on retry): a
  battleground put in or named after a library zone that has none ("a battleground in Tanaris"),
  or a word in a boss context ("parse X", "wipe on X", "get X down") that the library's text
  never uses (`Library.known_words`, its full-text vocabulary) and that is no everyday word,
  shorthand or member. Without a library nothing is flagged. Over the live corpus it flags the
  three invented bosses (9 lines) and the 3 misplaced battlegrounds, and only typos the typing
  adds after the check otherwise. "Tonight so far" names the battleground she played
  (`wow.bg_played`) and lists her evening in order ("you were in Tanaris; then you played a game
  of Warsong Gulch").
- **His addons' whispers** ("<DBM> ... has wiped on ...", "<Deadly Boss Mods>", "<BigWigs>",
  any capitalised `<Tag>`, "[AutoReply]") are not his words: never stored, never answered, no
  handler sees them (`pastes.is_addon_whisper`, `app._on_chat`; log
  `his_addon_whisper_skipped`). "<3", a typed "<sigh>" and his pasted "<Flask>" checks are his.
