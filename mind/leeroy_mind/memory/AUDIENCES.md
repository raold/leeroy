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
  re-derives stored memories once (`backfill_officer_scopes`): game officer-chat memories, which
  were open, become officers-only; officers' Discord ones, which reached nobody, reach the
  officers. It first drops his officer-chat lines from slang memories' evidence (slang is
  learned from public lines only; they were taken as public before), so only memories with real
  officer content move: 15 of 12,559 on the live guild's copy, with 683 slang memories cleaned.
  Private memories with their partner already crossed media; `bond:` moments now reach
  a DM to her partner as well as a whisper. A DM turn is its member's own: a pair memory never
  reaches another member's DM.
- **Lines.** `recall.whisper_ok` is the conversation rule: a private line reaches a private turn
  between the same two in either medium; a game officers' line only the officers' game turns
  (it used to reach every game turn); every other Discord line only its own channel; other game
  lines every game turn, as before.
- **The pair timeline.** A whisper or DM turn between her and him also shows their lines from
  the other medium (`talk.pipeline.PAIR_LINES`, 6, within `PAIR_WINDOW_S`, a day), labelled
  "(Discord DM to …)" or "(whisper to …)". An opener shows only hers. A line in one medium is
  still never the line she answers in the other (R11).
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

A late answer's instruction says how long ago he wrote and why she missed it ("asleep", "at
work", "busy"), so she answers as someone catching up. Every draw is seeded by his post id, so a
restart finds the same moment; the owed DMs are re-read from the store each tick (`DiscordTalk.
tick`, every 30 s from the mind's ticker), with the newest answered post in meta
`discord:dm:answered:<guid>`. A non-urgent late answer goes out only while a live lease already
holds the model (his login, the server's, his Discord's, voice, a raid), so it never wakes the
night; an urgent one takes his Discord lease. A DM still owed after 3 days is let go. A member
away (`away` table) answers when she is back, within those 3 days. #259's DM openers skip a
member who owes him an answer. Logs: `discord_dm_later`, `discord_dm_answered_later`,
`discord_dm_let_go` (guid, message id, seconds), never words.
