# One mind per member: audiences, not media

Each member keeps one mind across the game and the guild's Discord. What she knows reaches a
conversation by **who is in it**, never by where it was said.

## The audiences

| Audience | Lines | Reaches |
|---|---|---|
| Pair | a whisper between two people; a Discord DM (`dm:<guid>`, her and him) | a whisper or a DM between the same two, in either medium |
| Group | a small DM (`gdm:<id>`) with captured readers | a pair or group whose complete current audience is a subset of those readers; never a public, officer, game-local or voice turn |
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
- **Recall diversity** (`[talk] recall_diversity`, `memory/diversity.py`). Its rules run after
  `scope_ok` and `_shareable`, so they only drop or lower a memory the turn's audience already
  allows. "This conversation" is the pair in either medium (her whispers to him and their DM),
  else the channel, a group DM its own; her lines to him are read for tired subjects only in a
  whisper or DM turn with him, and only as counts of shared words.

## Replies on her own time (`[talk] dm_own_time`)

Group DMs are restricted and never `is_private` (which means a pair). Their memories
use `privacy.GROUP` (-2) and retain the original reader sets through nested evidence.
`scope_ok` and `hidden_ok` must both authorize recall. New members receive no old
group observations. The pair timeline remains pair-only. See `groups/README.md` for
the switch, membership rules and per-member answer timing.

Default false; invalid means off; read with the rest of `[talk]`. With it on, a member answers
his Discord DM when her Discord presence says she would see it (`discord/owntime.py`):

- online, or talking with him (her last line to him, in this DM or a whisper, within 10
  minutes before his post): at once, as before;
- idle (at work): when she next glances at her phone, 1-20 minutes, or soon after she comes
  online; do not disturb: 20-90 minutes, likewise;
- offline (asleep): 2-15 minutes after her presence is next online or idle;
- an urgent post ("are you up?", "need help", "asap", "call me", ...) cuts any wait to 45 s - 5
  min, asleep included; three posts of his in a row cut an idle or dnd wait the same way.

Only a post she deferred is owed: `on_his_post` (after a photo's look) records it in meta
`discord:dm:deferred:<guid>`, so the tick never answers a post before the desk hands it over,
nor one she let be (turning the switch on answers no old DMs), and her answer to an earlier post
that lands after his next one is no answer to it. A late answer stays owed until hers is
stored: a turn that comes to nothing (a restart, a drop) is queued again after 10 minutes, and
the post is let go after two tries. An edit of his reads the moment again (urgent words added or
dropped). A late answer's instruction says how long ago
he wrote and why she missed it ("asleep", "at work", "busy"), so she answers as someone catching
up. Every draw is seeded by his post id, so a restart finds the same moment; the owed DMs are
re-read from the store each tick (`DiscordTalk.tick`, every 30 s from the mind's ticker), with
the newest answered post in meta `discord:dm:answered:<guid>`. A non-urgent late answer goes out
only while a live lease already holds the model (his login, the server's, his Discord's, voice,
a raid), then holds his Discord lease beside it so the model stays for the answer; held beside
another lease it hands nothing over, so it never wakes the night (it may keep the model up to
10 minutes longer). An urgent one takes the lease itself. Presence is read through UTC, so a
daylight-saving change in the wait is honoured. Turning the switch off answers what she
deferred at once, across restarts; with it off and nothing deferred, nothing is written. A DM
still owed after 3 days is let go. A member away (`away` table) answers when she is back,
within those 3 days. #259's DM openers skip a member who owes him an answer. Logs:
`discord_dm_later`, `discord_dm_answered_later`, `discord_dm_let_go` (guid, message id,
seconds), never words.

## A scope is never wider than its evidence (2026-11-17)

`derive_scope` runs when a memory is written. When a memory it cites is made stricter later
(a cleanup, an ask for secrecy, an owner's edit, a keyed rewrite), the scope stored on the
citing memory used to stay as wide as it was, and one level was all recall checked. Now:

- **Readers go by the effective scope** (`privacy.resolve`): the stored scope made as strict
  as the memory's evidence implies, where a cited memory counts with its own effective scope,
  to a fixed point over `memory:` refs (cycles included; scopes only rise, so it ends).
  `Store` hands out every `MemoryRow` with it (recall, the night's provenance walk, outreach's
  sources, the lesson walk's `Visibility._effective`), `derive_scope` reads cited memories with it (the writer, culture's and the
  lexicon's source checks, growth, supersede), and the readers that take rows from SQL use
  `privacy.gated` (the callback selector), `effective_scopes` (the ledger walk,
  the notes' evidence, the server's life-beat seeds).
- **It is stored, never trusted when missing.** `memory.derived_scope`, `derived_with` and
  `derived_at` keep the last check (what the evidence implies now, derived afresh each time;
  the stored scope is the one that never widens). Triggers (migration `scope_evidence`) keep `memory_cite`,
  the index of each memory's `chat:` and `memory:` refs, and clear `derived_at` on every memory
  resting, at any depth, on one whose scope or evidence changed, in that change's own
  transaction. A row without `derived_at` is derived by the reader (at most
  `RESOLVE_NODES`, 2,000, unchecked memories a read; a memory beyond that, and whatever rests
  on it, reads as secret with nobody, never as another member's). That bound is a reader's
  only: nothing is ever stored from it. The writer derives a new memory from the strictest
  scope known for such a cited memory (`derive_scope(known=True)`), and the store leaves the
  row unchecked for the night. `derived_complete` is 0 when the evidence, at any depth, is
  malformed or cites a memory with no row: the scope stands as it always did (a missing ref
  claims nothing), but the lesson walk takes no such memory as proved, checked or not.
  Every "what rests on this" walk goes queue first, cites by index (`dep CROSS JOIN
  memory_cite`): the planner's own order read every cite once per dependant (6.7 s for the
  largest live hub, 2,391 dependants; now 2.4 ms).
- **Writes settle it** (`memory/scopes.py`): `insert_memory`, `upsert_keyed_memory` and
  `merge_memory` check the memory they wrote and raise its stored scope, then up to 500 of the
  memories resting on it; `nightly life-contact` does the same for the beats it makes private;
  a line that asks for secrecy (inserted, threaded, or added by an edit) makes every memory
  resting on what the ask covers be derived again. Whatever is left waits for the night.
- **The night's stage `scopes`** (model-free, after `forget`, always attached) checks the
  memories written in the last three days again and whatever is unchecked, at most 20,000 a
  night; the state is the rows' own, so a night cut short loses nothing. Log
  `scopes_settled night= checked= raised= left= receipt=`.
- **`python -m leeroy_mind memory rescope`** derives every memory again. A dry run (the
  default, read-only, also on an unmigrated mind.db) prints counts: by old and new scope, status,
  kind, source, the kind of evidence, and the sent turns of the last `--days` (7) that carried
  such a memory to an audience it no longer reaches. `--go` (mind stopped, snapshot
  `pre-rescope`) raises the stored scopes and writes a receipt (`memory_rescope`); `--receipts`
  lists them; `--undo N --go` restores the stored scopes of receipt N that are still what it
  made them. An undo restores what is stored only: readers still go by the evidence, and the
  night raises a stored scope again when it next checks that memory. Never text.
- The migration's data step checks every stored memory once and writes the derived columns
  only; it changes no stored scope.
- **Stored scopes change without `--go` too.** From the first night and the first write
  after the migration, the running mind raises the stored scope of whatever it checks: the
  `scopes` stage (receipt source `night`; 172 rows on the first night of the live copy), a
  write that touches a memory (`write`), an ask for secrecy (`secrecy`; 462 rows for the
  widest live thread), `nightly life-contact` (`life-contact`). Each batch writes one receipt
  (ids and scopes, no text); `memory rescope --receipts` lists them all with totals by source.
  `memory rescope --go` raises what is left in one step (`cli`), and `--undo N` restores only
  receipt N's rows.
- Measured on the live copy (17,823 memories, 171,920 cites): migration 7.8 s, `--go` 9.8 s,
  `--undo` 2.7 s; a raw raise of the largest hub 63 ms and its settle (565 rows) 250 ms; an
  ask for secrecy in the widest private thread 208 ms, each later line there 1 ms; the night's
  stage after a hub raise 0.75 s.
- **What it does not follow.** `memory:` refs and the lines a memory cites, nothing else: a
  public line counts as public evidence even when the prompt that produced it carried a
  private memory (ruling B2: what was said on a public channel is public; the lesson walk
  opens those carried memories for a note's own source lines only), and `encounter:`,
  `beat:` and the night's other refs carry no scope here (the stages that write from them
  ask for one).
- A scope only ever gets stricter on this path. A status change (archive, supersede, reject,
  the owner's erase) changes no scope: a memory of any status passes its scope on, as before.
