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
per-member cooldown. Combat and wipe silence/debrief suppress it. A server
conversation's reply can be drawn as a burst (`TurnMeta.burst`): it splits at any
clause break once it has four words, without the line-length rule or a second draw.
On Discord only a turn's first message is a formal reply; its follow-ups and a
correction are plain messages. Rare corrections
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

# A guild that talks among itself

Owner's "pass the Turing test" push (2026-11-15): 82% of the members' typed game lines were aimed
at him, every login drew 2-5 greeters, 23 of 147 greetings opened "there he is", and a quarter of
the openers to him asked how his day was or what he was up to.

- **Openers.** The openers' clock (`budget.BurstBudget.to_him`) runs at `[talk] openers_per_hour`,
  now 2 by default (was 6), still scaled by chattiness and the talkative dials. Each due opener goes
  to him only `[talk] openers_to_him` of the time (a number 0-1, default 0.35; 1 is the old
  behaviour, 0 none); the rest start a conversation among the members in guild chat about the
  guild's own business (`engine._open_to_guild`, `guild_topics.py`): a kind of subject first
  (raid, the member's own game, loot, a life beat, the calendar, a rival guild), then a topic, from
  the seeds the Discord server reads (`server.seeds.public_seeds`, never an officers' one, never
  his loot) and the member's wants and pastimes. A life beat is raised only by its member, a loot
  or birthday topic never by the member it is about, a topic once a night; others answer it as a
  social burst, which holds the social bursts' own clock while it runs and rests it a gap after
  (`BurstBudget.begin`), and it gives way to a social burst already running. With every candidate
  just having spoken nobody opens. Utterance `why` is `burst:guild:<subject>`. An opener to him
  never asks how his day went or what he is up to (`engine.TO_HIM_TOPICS`).
- **Greetings** (`greetings.py`): nobody greets about 30% of his logins, else one or two; the login
  quiet starts either way (`greet_skipped: reason=nobody_noticed`). A later greeter who is a man or
  a woman with no romance toward him types a plain hello half the time (`greet_plain:<how>`). A
  stock greeting ("there he is", "welcome back"...) said in any greeting within 3 days, this
  login's included, and the opening three words of an earlier login's greeting within a day,
  rest: each greeter's instruction names them as it is composed (an instruction hook, so a later
  greeter knows what an earlier one just said), and a line that still says one is asked again
  once with a hint naming exactly that phrase, then dropped (`phrase: resting`). Simulated with a
  fake model that ignores the list (40 logins): 0.93 greeters a login, 14 with none, 1 dropped.
- **The flirt rule** (`prompt.FLIRT_RULE`) reaches a woman only on a turn that is his (addressed to
  him, a whisper or DM to him, a FLIRT), and a man's "the women only have eyes for him" only on a
  turn that is his or where he is in the last three lines; every other turn reads
  `FLIRT_RULE_AWAY`, which still keeps flirting off every guildmate (and `filters.member_flirt`
  still screens every line). The golden prompts are unchanged: every prompt of the tuning scenario
  is a turn to him, and the scenario keeps its recorded greeter draw.
- No switch (ruling on the #319 review): the dials are `[talk] openers_per_hour` and
  `openers_to_him`. Rollback values: `openers_per_hour = 6`, `openers_to_him = 1`. The greeter draw
  is not a mind.toml key (`reactions.greeter_draw`, which the tuning scenario pins to 2-3); it, the
  topics and the flirt-rule scope roll back only with the code from before.

# DM prompts, owed answers and game names (2026-11-15)

All 18 last-resort trims logged since 11-14 were answers to his Discord DMs: her own 200-word
paragraphs filled "Your last lines", the DM's history was packed into a 1,100-character
instruction at 200 characters a message, and the trim emptied her memories first.

- **The DM's history is the Chat section.** `discord.talk.DiscordTalk.dm_chat` gives the answer
  turn (`TurnMeta.discord_chat`) the DM's last 12 messages, oldest first: his up to 600
  characters, hers as their first 30 words (`prompt.replay`), each with its photo note and age.
  Her own long lines are replayed the same way in "Your last lines" and in the elsewhere block;
  the repeat check still reads them whole. The DM's instruction keeps only where she is and his
  message. The copy check reads his earlier messages there (not hers: hers are the repeat
  check's), so she never sends one of his messages back as her own.
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
- **An answer he is owed never ends in nothing** (owner, 2026-11-15: every message of his gets
  a reply that adds something). Owed is `LiveReview.owed`, the one definition: his whisper, his
  DM, a post naming her, his pushback, a first answer to his question, an emote at her. A draft
  whose tells are all soft and none of `DROP_ON_RETRY` is kept, and sent when the retry is worse
  or silent (`say_owed_kept`). A first silence is asked again. When nothing usable is left,
  `Pipeline.owed_fallback` makes one more short generation on the same prompt, told only
  "Answer what he just said in one or two short sentences, in your own voice"
  (`OWED_FALLBACK_HINT`), reviewed only for silence and the hard screens (`OWED_HARD_TELLS`: AI
  or prompt talk, a flirt at a member, his words said back, asking him over), and sends it
  (`say_owed_fallback`); if even that fails it is dropped (`say_owed_fallback_refused`). An owed
  Discord answer over its length is cut back to a sentence end (`discord_length.fit_words`)
  instead of being asked again. Turns he is not owed keep the old rule. Seen live 11-15: her
  first draft to "leave him for me" was asked again for a repeated move and its length, the
  retry asked him over, and both were dropped.
- **Game names** (`talk/gamenames.py`, the soft tell "game name", dropped on retry): a
  battleground put in or named after a library zone that has none ("a battleground in Tanaris"),
  or a word in a boss context ("parse X", "wipe on X", "get X down") that the library's text
  never uses (`Library.known_words`, its full-text vocabulary) and that is no everyday word
  (or everyday ending: "homework"), boss nickname (`SHORTHAND`: festy, rotty, valy, maexx,
  levi...), known boss or place with its punctuation taken out ("lanathel") or member. A
  battleground "near" a place, in a capital city or in a zone with a battleground's entrance
  (`QUEUE_PLACES`), or named by its own longer name ("dalaran sewers"), is fine. Without a
  library nothing is flagged. Over the live corpus it flags the
  three invented bosses (9 lines) and the 3 misplaced battlegrounds, and only typos the typing
  adds after the check otherwise. "Tonight so far" names the battleground she played
  (`wow.bg_played`) and lists her evening in order ("you were in Tanaris; then you played a game
  of Warsong Gulch").
- **His addons' whispers** (a known addon's own tag, `pastes.ADDON_TAGS`: "<DBM> ... has wiped
  on ...", "<Deadly Boss Mods>", "<BigWigs>", "<RCLootCouncil>", "<Details!>", "[AutoReply]",
  "Auto-reply:") are not his words: never stored, never answered, no handler sees them
  (`pastes.is_addon_whisper`, `app._on_chat`; log `his_addon_whisper_skipped`). "<3", a tag he
  types himself ("<AFK> back in 5", "<Sigh> fine") and his pasted "<Flask>" checks are his.

# Every message of his is answered

Owner, 2026-11-15: "make sure they actually reply something. doesn't have to be a profound reply
but something, and needs to add to the conversation." Code: `owed.py` (its docstring is the
full rule); no switch, no migration, no native rebuild.

- **Planning.** A remark of his gets one or two answers, never none (`select.REPLY_COUNT_REMARK`);
  every whisper is answered by its member (`WHISPER_REPLY_P` 1.0); when everyone who could answer
  spoke in the last minute (weight 0), one of them answers anyway; his hello in the login quiet
  keeps one answer; a Discord channel post with nobody online or idle draws from the rest of its
  audience (`discord_draw_widened`).
- **The owed answer** (`TurnMeta.owed_answer`: the first answer to each line or post) passes
  the guild's pace, keeps a reviewed draft instead of being dropped, and has a 90 s (game) or
  300 s (Discord) deadline. If its member cannot speak, the next answer to that line in the job
  takes the debt. What is owed has one definition, the guards' `LiveReview.owed`: that first
  answer, an answer to his whisper or DM, a member he named, his pushback's target, the first
  answer to his question, her line after his emote.
- **In the turn.** `Pipeline.owed_fallback` is the one fallback (#325, above), extended here:
  its hard screens (`OWED_HARD_TELLS`) also refuse other people's lines, a list, an *action* and a
  copied sample line, and a refused or silent fallback is asked once more with what was wrong in
  words (`OWED_RETRY_HINT`, review-325-r2 P3-C). A turn whose model request failed or ran late
  (`llm_error`, `expired`) sends its late draft when it passes the hard screens
  (`owed_late_draft_sent`), else asks `owed_fallback` under the owed deadline
  (`owed.rescue_text`, log `owed_rescue_line`). An owed turn that still ends with nothing is
  the watchdog's. **There is no canned line** (review-327 P2-1): the only words ever sent are a
  draft of the member's that passed the hard screens.
- **The watchdog** (ticked by the app): a message still unanswered 45 s (whisper, voice), 60 s
  (game channels) or 150 s (Discord) after it, with nothing in flight, gets a rescue turn
  (`owed_rescue reason=not_planned|dropped:<reason>|no_answer`): a real generation (its member
  weighed with recent speech ignored, so the dials still apply; `no_speaker` holds when nobody
  present speaks at all). A rescue that finds no clean draft holds the message
  (`owed_held reason=no_clean_draft`) and tries again on a backoff (90 s, doubling to 15 min),
  and at once when the live model is back; a rescue the model itself failed (`llm_error`,
  `expired`) takes no backoff, and every backoff resets on the next line the guild sends. A
  held message is never given up for its age; what ends one: `nobody_present`,
  `target_offline` (she logged out), his goodbye after he logged out (`goodbye`), a line in a
  game channel once he logs out (`logout`: his chat frame is fresh at the next login; a whisper
  is kept and answered then, its age in the prompt), and a Discord channel post 6 h on
  (`too_old`; DMs and group DMs are kept whatever their age). A DM to a member her arc has
  away holds as `away`.
- **Held, then answered just after** (`owed_held reason=`): his raid's boss fight and wipe silence
  (`raid`, Discord answers too; `dead` for a /say nobody alive can answer yet), a Free GPU pause
  (`gpu_paused`), the live model away (`model_away`: the app refuses every line then), the
  whispered member AFK (`afk`), him offline for a game line (`human_offline`), a DM she answers on
  her own time (`own_time`), a group DM's pacing (`group_pacing`), a selfie request still being
  made (`photo_request`), his voice note still being transcribed (`voice_note`).
- **Never owed:** his bot commands and raid orders, emotes, addon pastes (one answer per paste
  burst already) and DBM/BigWigs/AFK auto-whispers; a line nobody with a persona can hear (an
  empty voice room, a whisper to someone offline or outside the cast: `owed_given_up
  reason=nobody_present|target_offline`); game lines from before a mind restart.
- **His talk first** (review-327 P2-2). A whisper or opener that starts a conversation with him
  (`TurnMeta.initiates`) waits once, up to 2 min, while answers to him are on their way, and is
  dropped `his_talk` only when he spoke after it was planned (`TurnMeta.planned_at`) and an
  answer to him is still in flight. After-kill and loot whispers (`KEEP_WHISPERS`) are never
  dropped for this.
- **Several answers to him at once** (owner, 2026-11-16: "allow longer periods of gpu usage if
  chatting with multiple bots at the same time"). An owed answer's deadline is its base (90 s
  game, 300 s Discord) times the answers to him in flight (`owed.answers_in_flight`: the turn
  each job is on and those queued behind it, `REPLY_KINDS` addressed or whispered to him), up to
  a hard ceiling of 600 s (game) / 900 s (Discord) so a stuck Ollama never wedges the mind; log
  `owed_deadline_extended in_flight= seconds=` from two in flight. The deadline bounds the model
  queue wait, the generation and the first line's typing alike (`_first_typing`), so none of
  those cuts an owed answer short; the per-request client timeout (60 s) stays. The `[ollama]
  max_inflight` slots (3) are unchanged: the extra answers wait their turn with the longer
  deadline. The Discord lease is kept while answers to him on Discord are in flight
  (`discord_lease_kept`, checked every 30 s past its 10-min idle end, 15 min at most), so the
  live model stays pinned; his login lease pins it already. The night and the offline raid leases are untouched.
- **Health:** `owner-replies` counts his messages of the last 24 h with no member answer within
  5 minutes, by medium (a note from one, a FAIL from 10); counts only, never text. With `[life]
  availability` above 0 the bounds are her day's (life/README.md, "Health").

# The beat before his whisper and DM answers (`[talk] think_private`, 2026-11-16)

Owner, 2026-11-16: "go with structured thinking for dms and whispers". Orion's own thinking (the
`think` flag left out; `think: true` is an HTTP 400) was measured at 5-6 s and 400-550 words a
line, mostly a recap of the prompt, needing ~1,400 tokens of room, and LIVE's blank-line stop
fired inside it. So an answer to his whisper or DM gets a short structured beat first
(`talk/beat.py`, `Pipeline._with_beat` / `_beat`).

- **Which turns** (`Pipeline._beat_on`): an answer to his whisper (`_answers_his_whisper`, or a
  `whisper_reply` turn in a whisper to him) or to his Discord DM (`_answers_his_dm`, `dm:<guid>`).
  Never game channels, Discord channels, group DMs (`gdm:`), voice rooms, templates or the night.
- **The beat**: one request on the turn's own composed prompt with `beat.instruction` after it,
  `think: false` (never left out: Orion would think), Ollama `format` = `beat.schema` and
  `BEAT` options (LIVE's sampling and `num_ctx` 4096, `num_predict` = `[talk] think_budget`,
  no stop), 10 s at most including the queue. Fields: `read` (what he is actually saying, "one
  short sentence", kept to 200 characters), `lands` (how it lands on her right now, "a few
  words, not a sentence", kept to 60), `want` (vent, share, ask, tease, flirt, reassure_seek,
  gossip, deflect, small_talk, react, boundary, logistics, each with a one-line meaning in the
  ask and in the schema's description, `beat.MEANING`; "ask" is wanting something from him,
  not asking a question; `flirt` only for a member `romance.flirt_chance` lets flirt with him)
  and `use` (the number of one offered item, or null). Offered: the memories the prompt showed,
  then the others' earlier chat lines in it, newest first (never hers, never the line she
  answers), as plain text (no age, speaker or bullet), at most 6, each cut to 120 characters
  (`beat.items_of`); the last give way when the request would not fit `num_ctx`
  (`beat.fitting`). `use` 0 counts as null (Orion writes it for nothing to bring in).
- **Bake-off, 2026-11-16** (47 real turns, blind-judged 0-3): plain Orion 1.62, Orion with the
  beat 1.60, the beat with trimmed rules 1.62 and the most good lines (72%); Orion's own
  thinking 1.34 with 21% empty. With only the wants' names Orion chose "ask" on 34 of 47, hence
  the meanings; at num_predict 160 two beats were cut off inside a long `lands`, hence 240.
- **The line** is asked for exactly as before, with these after its instruction:
  ```
  You read it as: <read>
  It lands: <lands>
  You want to: <the want in words, e.g. "get something off your chest", "get something from Zan">
  Bring in: <the offered item>          (only when use is set)
  ```
  The beat is asked once a turn (`TurnMeta.beat`): the drafts, a guard's rewrite (which
  composes again), the fresh answer and the owed fallback all carry the same lines; retry hints
  and the owed fallback's own line come after them.
- **Skipped**: an error, 10 s, a paused GPU, an expired turn, an empty reply (no `read` or
  `lands`) or one that is not the schema (a want off the list, `use` out of range): the line goes
  exactly as it would without the beat, never empty (`think_beat_skipped: guid= reason= ms=`;
  reasons `empty`, `schema` (a cut-off JSON object too), `timeout`, `llm_error`, `gpu_paused`,
  `expired`, `scope_changed`, `room`, `error`).
- **Privacy**: the beat's words never reach a log, a store or a receipt; `think_beat: guid=
  want= use=0|1 ms=` only. Its time counts in the turn's `llm_s`.
- **Room** (estimates at 4 characters a token, real counts up to 25% over, 64 tokens of chat
  template): the beat's ask is 286 tokens with nothing offered and 484 with six items, its lines
  at most 118. His DM at `DM_BUDGET` 2600 with think_budget 240 keeps 4 items: (2600 + 1 + 422)
  x 1.25 + 64 + 240 = 4083; at 320, 2 items: 4085. The DM's line: (2600 + 118) x 1.25 + 64 + 344
  = 3806. A whisper at 1800 keeps all 6: 3160 (3240 at 320); its line 2542. All under `num_ctx`
  4096, so no prompt budget changes.
- **Learning**: a line asked for after a beat has recipe `model_id` `<model>+beat` and `beat`
  in its parameters (`learn/recipe.py` `called_model`; only then, so with the switch off a
  receipt is as before): `learn report` separates its rewards. The bandit's context key is
  unchanged, so its arms do pool with lines without a beat (a whisper opener to him shares the
  `whisper` context).
- **Golden prompts**: `golden_prompts.json` records the tuning scenario with the beat off (its
  one whisper answer is unchanged); `golden_beat.json` (`scenario.record_beat`) holds two answers
  to his whispers with it on: every request as sent.
- `[talk] think_private` (default true) and `[talk] think_budget` (120-320, default 240) are
  read at mind start; a login's reload of mind.toml does not change them. Rollback:
  `think_private = false` and restart the mind. Older code logs both keys as unknown and ignores
  them, so they need not be removed before rolling back.

# Wants (`talk/intent.py`, 2026-11-16)

What she wants from a turn (human-likeness spec section 2). There is one want enum and one block
for every channel, never a second.

- **One enum**: `intent.Intent`, the twelve codes of think-dm's beat in its order (vent, share,
  ask, tease, flirt, reassure_seek, gossip, deflect, small_talk, react, boundary, logistics),
  with the words "You want to:" reads (`WANT_WORDS`, the beat's words as merged in #329; `{who}`
  is the addressee: his called name on his turns, the member's name on a member's, "everyone" on
  an opener). `beat.WANTS` is built from it (keyed by code, `{human}`) and `beat.wants_for` lists
  its codes, so the beat's ask, schema and lines are unchanged (`golden_beat.json`).
- **One block**: `beat.render(read=, lands=, want=, who=, topic=, use=)`, a line for each field
  given, in this order: "You read it as", "It lands", "You want to" (`want_words`: the words,
  then the topic in brackets), "Bring in"; "" for none. `beat.lines` is `render` from the beat.
- **Who chooses**: on his whisper and DM answers the beat (the model) chooses; everywhere else
  code draws (`draw_intent`). Whenever his explicit whisper overlay is in the line's prompt
  (`romance.whisper_tail`, `Composed.tail`), the beat is not offered the wants it rules out
  (`beat.wants_for(..., tail=True)` drops `intent.TAIL_OFF`), whatever `[talk] intent` says
  (review-332). With `[talk] intent` (core plan Task 8; below) the beat chooses among the
  wants `allowed` permits, leaning to the drawn one, and every other eligible turn's want is
  drawn.
- **The draw** (`weights(ctx)`, `IntentContext`): a base row, then multipliers in order. Turn
  kinds outside `INTENT_KINDS` (reply_human, continue, open, whisper, flirt) want nothing. Rows
  (vent, share, ask, tease, flirt, reassure_seek, gossip, deflect, small_talk, react, boundary,
  logistics); his line's row by `select.classify_human_line` (a goodbye reads as a greeting), a
  flirt (`filters.flirty`) overriding it:

  | Situation | weights |
  |---|---|
  | his question | .7 3 1 1.5 1 .3 .3 .7 .5 1 .3 .5 |
  | his greeting | 1 2 .5 1 1 .3 .3 .2 3 2 0 .5 |
  | his remark | 1 2 1 2 1 .3 .5 .5 1 3 .3 .5 |
  | his flirt | .3 1 .5 2 4 1 0 1 .5 1 .5 0 |
  | continue | 1 1.5 1 2 0 0 1 .5 1 3 .3 .5 |
  | open | 1.5 3 1.5 0 0 0 1 0 1 0 0 0 |
  | whisper, flirt (hers to him) | 1 2 1 1.5 4 1 0 0 1 0 0 0 |

  Multipliers: flirt only from a woman, to him, with an attitude (fond 0.3, drawn 0.7, lust 1),
  times her forward dial (`IntentContext.forward`, from `romance.forward`: 0-2, 1 when unset; at 0
  she draws no flirt), never toward a member; reassure_seek only then too, x2 when anxious; neither
  in a group DM. Under his explicit whisper overlay (`tail`) vent, gossip, deflect, boundary,
  logistics and small_talk are 0 (PM log 2026-11-16: the overlay wins, for the draw and the beat;
  the beat drops them too, below). An owed answer never just reacts (react 0); owed and his
  question: deflect and boundary x0.3, vent x0.5. Vent x(1 + 2 max(0, stress)), x2 on a bad day or a
  sick kid, x1.5 at work; share x(1 + max(0, valence)), x1.5 on a night out; tease x1.5 for teasing
  humour or agreeableness 4 or less; boundary x(1 + max(0, stress)); deflect x1.5 when shy or
  guarded; small_talk and react x2 when tired (energy -0.3 or less); unsociable (social -0.3 or
  less): react and boundary x1.5, share and ask x0.6. Her feeling about him moves only her turns to
  him (review-332), never how she treats the others: tease 0 when she is sore with him (toward_him
  -0.3 or less), boundary x1.5 when toward_him is below 0, deflect x(1 + max(0, -toward_him)).
  Logistics x3 when his line has a plan (`questions.is_plan`) or a real time of day ("8pm", "8:30
  p.m.", "20:30", "server time"; never "lvl 80 pm me"); gossip 0 in a whisper or DM not to him; her
  last want x0.5. Without a state every state multiplier is 1.
- `allowed(ctx)`: the wants above 0, enum order. `draw_intent(ctx, rng)`: one weighted draw (one
  `rng.random()`), None when every weight is 0; `rng` is the caller's seeded draw, never
  `engine.rng`.
- **Topics** (`topic_for`, code-built, never invented; "" for the rest and on an opener, whose
  own topic is its subject): vent "about a rough day" (bad day), "about your kid being ill",
  "about being stuck late at work", "about work" (at work or commuting), "about the kids" (kids),
  else "about your day"; share "your night out" or "something from your day"; gossip "only from
  what you remember"; logistics "raid times, gear or who's coming".
- **Her day and feeling**: `block`, `event`, `kids` and `attention` come from her presence
  (`life.presence.presence_at`, her day plan); `state` from her feeling right now (`life.state`,
  plan Task 3, not built yet). An event or block compares by its code (`DayEvent` and `Block` are
  StrEnums); a state's five floats are read by name (a missing one 0), so `intent.py` needs no
  `life.state` at run time.
- `python -m leeroy_mind life intents --data-dir "$D" --guid N [--kind reply_human] [--family
  whisper] [--line TEXT] [--owed|--no-owed] [--tail] [--event CODE] [--at YYYY-MM-DDTHH:MM]
  [--samples 500]`: each want's share in percent from her stored persona and her presence at
  `--at` (this PC's clock, default now; Discord families read her Discord attention, the others
  her game attention), `--event` in place of her plan's event; a header with the kind, family,
  owed, block, attention and event, then a line per want (a `none` line when a draw has none).
  Read-only on mind.db `mode=ro`, codes and numbers only, never persona text, names or the line.
  It does not read her feeling (state none) or tuning dials beyond the stored persona's.

# The prompt as a situation (human-likeness core, 2026-11-16)

Spec section 9 (bake-off 1: the rule list primed compliance; the variant cut to the hard rules
scored best on public turns: with the beat 1.82 against 1.65, alone 1.71). The pipeline uses them
only with `[talk] situation` (core plan Task 8; below), so with it off every live prompt and
`tests/tuning/golden_prompts.json` are unchanged.

- **The hard rules** (`prompt.SITUATION_RULES`, `situation_rules`): a real person in 2009 in her
  WoW guild who never says the year, not an assistant, only her own words (no name in front, no
  quotes, no one else's lines, no narrated actions), shared history only from her memories, never
  guilt-tripping him, and "(nothing)" when she wouldn't say anything (the measured variant kept
  it; ruling S1 on #333). Same signature, prefix ("In your guild: ...") and suffix (flirt rule,
  tuning lines, nudges) as `comms_rules`, without `length_rule`, so the system message stays the
  same on every turn for a speaker (Ollama's prompt cache). Noted as `talk.situation_rules`.
- **The situation templates** (`prompt.SITUATION_INSTRUCTIONS` for reply_human, continue, open,
  whisper and flirt, `OPEN_TO_HIM_SITUATION`): no length of their own and no "respond to what they
  actually said". `render_instruction(..., situation=True)` uses them (a kind without one keeps
  its game template), noted as `talk.situation_instruction.<kind>`.
- **The length is the caller's.** `render_instruction(..., length_text=...)` puts the reply's
  drawn length where the template's own was (`DISCORD_DROPS`, then before `{extra}`), never cut,
  winning over the Discord length and `long`; on any template, so a game template can take a
  drawn shape too. A situation template with neither `length_text` nor a Discord length (a game
  channel, a voice room, Discord at 0 words) takes her usual line, `length_rule(typing)` (the
  `typing` keyword; `SHORT_LINE` without it), never an error, so no turn is dropped `bad_turn`
  for it. `long` does not apply to a situation template. The pipeline (Task 8) passes the shape,
  the two-lines sentence or the Discord length explicitly, and `typing=p.typing` for the rest.
- **Right now, last.** `PromptParts.situation` renders "Right now:" after the awareness card,
  just before the instruction (the want and "It lands" stay in the beat's block after the
  instruction). The trim treats it as before: never cut. `mood_line(..., where=, state_words=)`:
  where she is in her day right after the time of day, and her feeling in place of the mood words.

# Her state, want and situation in every prompt (core plan Task 8, 2026-11-16)

Three switches, each `false` by default (invalid means false, logged once by code:
`talk_intent_invalid`, `talk_situation_invalid`, `life_state_invalid`), read at mind start and
at each login: `[talk] intent`, `[talk] situation`
and `[life] state` (with `[life] availability` above 0 for where she is). With all of them off
every prompt is byte-identical (`golden_prompts.json`, `golden_beat.json`); with them on the
prompts are pinned in `tests/tuning/golden_core.json` (`scenario.record(..., core=True)`:
availability 6, state, intent and situation on, the beat off).

- **Her situation** (`Pipeline._draw_situation`, called once a composition, after the turn's
  checks and instruction hooks, before its prompt is built; a failure logs
  `talk_situation_failed: guid= reason=<class>` and leaves the turn as it would be without it):
  - `meta.life_presence` (availability above 0): her Discord presence now on a Discord turn (a
    whisper answer carried to her DM included: review-336), her game presence on a game turn
    the engine did not plan (a planned one keeps the presence it was planned with).
  - `meta.state` (`[life] state`): her feeling, from `LifeService.state_of` (the 30 s snapshot);
    None while the switch is off (`LifeService.state is None`).
  - On an **eligible turn** (`_want_eligible`: `intent.INTENT_KINDS`, the model's own line, and
    none of a template, his pushback's answer, a photo, a selfie request, an outreach item, the
    owner's string, his lore or game question with facts, the watchdog's rescue, a room he is not
    in, a hot mic or the 24/7 server's turn) that answers a line of his (`_his_line`: its trigger
    line when his, his Discord post, or his whisper a carried DM answers): `meta.lands` =
    `life.state.lands(state, tone(his line), agreeableness=, forms=his)`. Never why she is sore,
    and a feeling only, never how much she says ("it barely registers", "you're not feeling it
    right now"; the reply's length is the shape's: review-340).
  - `meta.intent` (`[talk] intent`, an eligible turn): one `draw_intent` over her
    `IntentContext` (her persona and forward dial, her presence's block, event, attention and
    kids, her state, owed by `owes_him`, his explicit overlay as `tail`, her last want in this
    conversation), seeded by `life.seed.seeded("intent", night, her, minute, his line or post)`,
    never `engine.rng`. His explicit overlay (PM log D13, the review-340 S1 ruling): the whisper
    overlay in a whisper to him (`whisper_tail` applies) or, in his DM, her explicit band with
    the owner's `romance_explicit` overlay (`romance.dm_explicit`), whether or not her romance
    text is rolled in this turn; never in guild chat. On a turn only he reads (his whisper and
    DM answers and her own whisper starts) a woman `romance.flirt_chance` keeps from flirting
    with him (a fond one, her forward dial at 0) draws no flirt (review-332, review-340).
    `want_source` "draw"; log `talk_want: guid= kind= want= source=draw owed=0|1`.
- **One block after the instruction** (`beat.render`, where think-dm puts its lines):
  - His private turns with the beat on: the beat still runs. With intent on its wants are
    `intent.allowed(ctx)` (within `beat.wants_for`, so the overlay tail, the owed rule and the
    flirt rules gate the model too) and its ask names the drawn want as a lean ("You lean toward
    tease (tease Zan)."); the beat's want, when it parses, is the turn's (`want_source` "beat";
    `think_beat: ... lean=<code>`), else the drawn one. With state on it is asked without
    `lands` (`ask_lands=False`) and "It lands" is `meta.lands`. Its block then carries the
    want's topic. With intent and state off it is think-dm's, except that under his explicit
    overlay in his DM (her romance text at the explicit band with the `romance_explicit`
    overlay) the beat, like under the whisper overlay, offers none of `intent.TAIL_OFF`
    (`Composed.tail`; the S1 ruling, live whatever `[talk] intent` says).
  - Every other eligible turn (any channel; his private turn with the beat off or skipped):
    `render(lands=meta.lands, want=meta.intent, who=<his called name, the member's name, or
    "everyone" on an opener>, topic=topic_for(...))`; nothing when neither switch gives a line.
  - With a want drawn, a learned "tease" arm yields to it (`learn.apply.yield_families`: its
    style line goes and the recipe credits tease's control).
- **The budget counts the block** (review of #333, requirement 3; Review Focus 5) while intent,
  situation or state is on: the prompt is trimmed to its budget less the block's room (the
  beat's block at its caps before the beat is asked, `beat.block_room`; its own lines after;
  else the code's block), so a DM answer's prompt with the block is within `DM_BUDGET` and,
  with the answer's `num_predict`, inside `num_ctx`. It applies to his whisper answers too
  (about 118-130 of `LIVE_BUDGET`'s 1800 before the beat) and with `situation` alone. Off: as
  before (the beat's block outside the budget, inside `num_ctx`).
- **Her last want** (`last`) is kept per conversation when a line is said, typed or spoken in a
  voice room.
- **Right now** (`mood_line`): with availability above 0 her own clock (`day_line` of
  `discord.presence.local_time`) and where she is (`life.presence.WHERE_TEXT` by block, medium
  and kids: "You're at work, the game open on your PC.", "Your phone woke you up.", ...; "You're
  at your PC, playing." while she is in his group in game as he plays, as PM log D15 has her
  answer him then; never in a raid call, CALLOUT or URGENT, nor in a voice room he is in:
  `Pipeline._where_line`); with state on her feeling (`life.state.words`, how she feels about
  him only on a turn of his, `_aimed_at_him`) in place of the mood words. While the where line
  shows, the awareness card's self line leaves out her Discord status, and a member free or
  gaming by her day has none anyway (`discord.presence.status_text`; review-340 P2-1: never
  "online (out for a walk), playing WoW"; at availability 0 a member playing WoW takes none of
  the clock's evening phrases either).
- **The situation mode** (`[talk] situation`): `situation_rules` for the system message,
  `PromptParts.situation` ("Right now:" just before the instruction), and every situation
  template's length from `prompt.situation_length` (review of #333, requirement 1): an explicit
  whisper's two lines ("In a line or two."), else the Discord length where one applies (without
  its "more than your usual one line", `prompt.USUAL_LINE`: a situation prompt states no usual
  line; noted as `talk.situation_discord_length`), else her usual line (`length_rule`). Other kinds keep their game
  templates and lengths. The learn recipe versions on the situation rules and template
  (`talk.situation_instruction.<kind>`; requirement 4).

# Reply shape

`talk/shape.py` (human-likeness design, section 4) draws what kind of message a member's reply
is before it is generated: an emoji reaction, a word or two, a short line, a two- or
three-message burst, a paragraph, a short line asking something back, a late reply that says
so, or nothing (left on read). It is pure (no clock, I/O or logging) and draws only from the
caller's seeded `rng`. **Nothing calls it yet**: the pipeline wiring, `[talk] shape` and
`[talk] dm_mirror` come with the plan's Task 9, so until then no prompt, room or send changes.

- **Odds** (`ROWS`, then renormalized): an answer he is owed is a short line, a burst or a
  question (game and whispers .65/.15/.20; Discord channels and group DMs .55/.10/.15 and .20 a
  paragraph; DMs .55/.20/.25), **never** left on read, a reaction or a word or two. A turn he is
  not owed: in game (a second answer, CONTINUE) .25 left, .25 a word or two, .35 short, .07
  burst, .08 question; in a Discord channel or group DM .20 left, .15 reaction, .12 a word or
  two, .30 short, .08 burst, .05 question, .10 paragraph. A whisper or DM is never left on read
  (channels only): its unowed turns take its owed row. An opener (OPEN) is .60 short, .15 burst,
  .25 question (and .15 paragraph on Discord); her own whisper or flirt .60/.25/.15. Other kinds
  (greetings, gz, raid calls, bits, asides) stay one short line.
- **Multipliers**: her intent (react x3 a word or two and reaction; ask x4 question; vent x2
  burst, x1.5 paragraph; boundary x2 short; logistics x1.5 question), her state (energy <= -0.3:
  a word or two and reactions x1.5, bursts and paragraphs x0.5; social <= -0.3: leaves x1.5;
  social >= 0.3: bursts x1.5 and leaves x0.5), her attention (glancing: paragraph x0.3, burst
  x0.5, short x1.5; half: paragraph x0.6), her typing (bursts x(0.5 + split_rate); a longest
  line of 6 words or fewer makes a word or two x1.5). A Discord burst needs `[talk] realism`; a
  reaction in game is a word or two; a paragraph needs Discord length (its setting above 0).
- **Forced**: a game or whisper answer 120 s or more after his line (`LATE_SHAPE_S`) is LATE
  ("You're only now seeing this (you were at work): ..."). A DM with `dm_mirror` 0 is today's DM:
  a paragraph of up to `dm_words` in `DISCORD_DM`'s words. The explicit whisper band
  (`two_lines`) keeps at least two lines: its short line, question and late reply are sized as a
  burst (twice her line's words over 2 lines), late or not.
- **Words and room** (`ShapeDraw`): a word or two is 1-3 words with `num_predict` 24; in game a
  short line, question or late reply takes her own line's words (`typing.words_per_line`) and a
  burst twice that over 2 lines, with the preset's room; on Discord a short line is 20 words
  (never over `discord_words`), a burst a couple of sentences over 2-3 messages, a paragraph
  `discord_words`, each with `discord_length.num_predict`. Discord at a setting of 0 is sized as
  the game. Left on read and a reaction write nothing.
- **DM mirroring** (`dm_words_for`, `[talk] dm_mirror` 0-10, default 5, Task 9). With
  `m = dm_mirror / 10`:
  - `mirrored` = his words x U(0.7, 1.8), within [max(3, her shortest line), `dm_words`];
  - `habit` = her longest line x U(1, 3), within [3, `dm_words`];
  - `target = mirrored^0.75 * habit^0.25`: his length leads, her habit (her game line, so only a
    tempering term) pulls toward about 16 words;
  - `words = dm_words^(1 - m) * target^m * lognormal(0, 0.35 m)`: the dial pulls from the
    owner's cap toward the target (10 follows it alone) and spreads it;
  - when he wrote 40 words or more, the full cap a quarter of the time; always 3 to `dm_words`.

  At 5, with her line (3, 8), his 3 words get a median of about 33 (p10-p90 25-42), his 28 about
  74 (57-96), his 120 about 141 (101-200). At 10: 5, 28 and 99. `dm_mirror` 0 (or `dm_words` 0)
  is exactly today's, with no draw. (Review of #331: the plan's `sqrt(mirrored * habit)` with no
  spread gave a 3-word DM about 39 words, and only +/-13% for one message.)
- **Shape lines** (`shape_line`): the one length line the shape puts in the prompt ("" for left
  on read and a reaction). Discord paragraphs keep `DISCORD_LENGTH` / `DISCORD_DM`; a Discord
  burst (2-3 messages) says "two or three quick lines", a game burst "two quick lines".
- **Inspect**: `python -m leeroy_mind life --data-dir "$D" shapes --guid N --family dm
  --his-words 28 [--owed] [--kind continue] [--dm-mirror 5] [--samples 1000]` reads her stored
  typing profile (mind.db opened read-only) and `[talk]` from mind.toml, and prints the shapes'
  shares and the words' p10/p50/p90 as JSON: numbers and codes only.

# Romance realism (`[romance] realism`, 2026-11-16)

The owner's sandbox audit (2026-11-16): a non-romance DM and a game whisper read as a person, a
lust woman's DM read as a bot: 60-150 words to his 5-24, a flirt on every beat, his question often
dodged, the same week-old plan (a "tonight ... tomorrow" intention of hers) in most
answers, and every draft rewritten by the phrase cooldown. Her romance text told her to escalate on
every turn (her heat, the stage's passage, her own recent moves, a rival, "Bring want, longing, a
tease", a game hook), his card's note carried that week-old plan with no age, and recall brought
back one of seven nightly copies of it each turn. With `[romance] realism = true` (default false;
read at mind start and at each login's reload, like `dial`):

- **Her romance text** (`romance.romance_instruction`'s `realism`): her heat line and the stage's
  passage only when his line turns it up (`romance.his_heat`: a flirt, a declaration, compliment or
  pet name of his, or desire and sex in his own words); never the "Between you two lately" scene
  notes or the rival line (the scene's notes are still written; the Lately line still counts her
  moves); the distance frame without "Bring want, longing, a tease" (`DISTANCE_REAL`,
  `DISTANCE_PLAN_REAL`; the whisper tail takes it too); and `REALISM_LEAD` last in place of the game
  hook (`SPECIFIC`): she is herself, answers what he said first, talks about her day and life as
  much as about him, flirts back to his flirting and goes further only where his lines lead. Her
  attitude, her way, her arc's lines, cooling, the band's wording (the owner's overlays, as
  before), `INVARIANTS`, the audience and the hour stay. Every turn that carries her romance text
  reads it this way, public ones included; nothing changes for men or women with no attitude.
- **His DM in her Chat** (`Pipeline._his_dm_row`): `discord.talk.dm_chat` leaves out the message
  she answers (the instruction quotes it), so the Chat she read ended on his previous message and
  her own answer to it, and behind ~300 tokens of romance text she often answered that one in the
  sandbox. With the switch on, a woman with an attitude toward him (fond, drawn, lust) answering
  his DM reads it as the Chat's last line too, shown as `dm_chat` shows his lines; it is kept out
  of the copy check's window, the claim support and the beat's items, as a trigger line is. Other
  members' DMs are as before.
- **His card** (`prompt.relationship_card`'s `realism`, his card only): her note toward him is one
  sentence (`prompt.realism_note`): the first without a time word (tonight, tomorrow, today, right
  now, this morning/week/weekend, next week) and without a "picked up" word list; none when no
  sentence is left.
- **Old memories** (`memory.recall`): a plan (kind `plan`, the night's intentions) older than
  `STALE_S` (48 h) is no recall candidate (`Recall.recall`'s `stale_plans`); any other memory that
  old says its time words as past in the prompt (`format_memory`'s `past`, `past_tense`: "tonight"
  -> "that night", "tomorrow" -> "the next day", "right now" -> "back then"). Stored memories are
  never changed. This applies to every turn's recall while the switch is on, not only romance
  turns. Game memories keep their own rule.
- **Not here**: the DM's length wording (`DISCORD_DM`, the dm-quality lane), the beat's bring-in
  ledger and question-first wants (dm-quality), the phrase cooldown's topic exemption
  (dm-quality), the reply-shape draw. The owner's overlays are read exactly as before.
- **Golden prompts**: with the switch off every prompt is `golden_prompts.json`'s;
  `golden_romance.json` (`scenario.record_romance`) is the same scenario with it on.
- Rollback: `realism = false` (or remove the key). Older code logs the key as unknown and ignores
  it.
