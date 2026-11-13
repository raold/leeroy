# One mind per member: audiences, not media

Each member keeps one mind across the game and the guild's Discord. What she knows reaches a
conversation by **who is in it**, never by where it was said.

## The audiences

| Audience | Lines | Reaches |
|---|---|---|
| Pair | a whisper between two people; a Discord DM (`dm:<guid>`, her and him) | a whisper or a DM between the same two, in either medium |
| Officers | game officer chat, the officers' voice room, every officers-only Discord channel | every officers' channel, in either medium |
| Guild | game guild chat and the other game channels; public Discord channels | public knowledge reaches every turn; a public *conversation* stays in its own place (R11) |
| Closed | a `discord:` channel the layout does not have | that one channel (fail closed) |

- **Memories.** `memory.privacy.derive_scope` gives a memory resting only on officers' lines the
  scope `private` with `scope_with = OFFICERS` (-1, no guid); `recall.scope_ok` lets it into the
  officers' turns of either medium and nowhere else. Officers' and pair evidence together, or a
  closed channel's, still give `private` with nobody (R10). Migration `0039_officer_scope`
  re-derives stored memories once (`backfill_officer_scopes`): open memories resting only on
  officer lines become officers-only; a stored scope is only ever raised, never widened. It
  first drops his officer-chat lines, taken as public before, from the evidence of slang
  memories, of every memory owned by a member who is not an officer (they never read officer
  chat) and from the lexicon's retained sources, keeping the rest. On the live guild's copy:
  687 memories and 24 lexicon terms cleaned, 7 memories of 12,559 officers-only, the night's
  `scope_leaks` 946 before and 123 after (the rest pre-existing). The night's provenance walk
  (`forget._node`, `mood`) gives an officers' root the same scope as the writer.
  Private memories with their partner already crossed media; `bond:` moments now reach
  a DM to her partner as well as a whisper. A DM turn is its member's own: a pair memory never
  reaches another member's DM.
- **Lines.** `recall.whisper_ok` is the knowing rule: a private line reaches a private turn
  between the same two in either medium; a game officers' line only the officers' game turns
  (it used to reach every game turn); every other Discord line only its own channel; other game
  lines every game turn, as before.
- **The pair timeline.** The chat window she answers keeps to the turn's own medium (R11:
  `Pipeline._may_carry`). A whisper or DM turn between her and him also shows their lines from
  the other medium in a block of its own before the chat, "Earlier between you two,
  elsewhere" (`PromptParts.elsewhere`; `talk.pipeline.PAIR_LINES`, 6, within `PAIR_WINDOW_S`, a
  day), labelled "(Discord DM to …)" or "(whisper to …)", never as the line she answers. An
  opener shows only hers; a DM post he deleted is left out; the block is trimmed first.
- **Secrecy.** His "don't tell" in a whisper or their DM covers the pair's later threads in both
  that night (`privacy._secret_threads`).
- **Guards and claims.** Her repeat check covers her own whispers and DM lines to him together;
  a claim from their DM guards her whispers to him and the reverse.

## Replies on her own time (`[talk] dm_own_time`)

Default false; invalid means off; read with the rest of `[talk]`. With it on, a member answers
his Discord DM when her Discord presence says she would see it (`discord/owntime.py`):

- online, or talking with him (her last line to him, in this DM or a whisper, within 10
  minutes before his post): at once, as before;
- idle (at work): when she next glances at her phone, 1-20 minutes, or soon after she comes
  online; do not disturb: 20-90 minutes, likewise;
- offline (asleep): 2-15 minutes after her presence is next online or idle;
- an urgent post ("are you up?", "need help", "asap", "call me", ...) cuts any wait to 45 s - 5
  min, asleep included; three posts of his in a row cut an idle or dnd wait the same way.

The tick leaves a post alone until the desk hands it over (a photo's look runs first) or
`HANDOFF_GRACE_S` has passed, and the desk's hand-over never answers a post the tick did; her
answer to an earlier post that lands after his next one is no answer to it. A late answer's
instruction says how long ago he wrote and why she missed it ("asleep", "at
work", "busy"), so she answers as someone catching up. Every draw is seeded by his post id, so a
restart finds the same moment; the owed DMs are re-read from the store each tick (`DiscordTalk.
tick`, every 30 s from the mind's ticker), with the newest answered post in meta
`discord:dm:answered:<guid>`. A non-urgent late answer goes out only while a live lease already
holds the model (his login, the server's, his Discord's, voice, a raid) and takes no lease of
its own, so it never wakes or delays the night; only an urgent one takes his Discord lease.
Presence is read through UTC, so a daylight-saving change in the wait is honoured. Turning the
switch off answers what it deferred in this run at once; with it off nothing is written. A DM still owed after 3 days is let go. A member
away (`away` table) answers when she is back, within those 3 days. #259's DM openers skip a
member who owes him an answer. Logs: `discord_dm_later`, `discord_dm_answered_later`,
`discord_dm_let_go` (guid, message id, seconds), never words.
