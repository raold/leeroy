# Lab and feedback

Lab reads `mind.db` through a fresh read-only connection with a one-second SQLite
query deadline. It never migrates or creates that database. Missing and older
databases show **Not set up yet**. The tab polls only while open and visible;
failed reads keep its last good data and show the existing stale indicator.

Rewards show the last 14 days, grouped by the computer's local date. Averages use
stored reward values; thumbs also include lines which have not yet been scored.
The summaries contain numeric scores and line IDs, not chat text. **Read line**
fetches text separately, only if the owner can see the line in Comms or its live
Discord message. Deleted Discord messages and private whispers between other
members do not disclose text. At most 100 requested lines remain in page memory.
Summary lists show at most 300 members/arms and 64 arm contexts. Lessons have a
member selector and pages of 50, so later members and proposed lessons remain
reachable. Weight history shows its latest 100 immutable versions.

Generated member lines in the game feed, whisper history, voice transcript and
Discord have thumbs with seven optional reason chips. Votes go through the
existing `feedback` op and `talk_signal`. Each saved vote has a numeric revision,
so a late snapshot cannot undo an acknowledged choice and another page's newer
choice can replace it. No votes or private text go into browser storage or logs.
Signal capture must be enabled for the mind to accept feedback; its refusal is
shown if it is disabled. Human lines and native game lines not authored by the
mind do not receive controls because the feedback op does not accept them.

New Discord lines always save a `discord:line:<message-id>` receipt, independently
of learning or preference capture. For older messages without one, read-only
recovery requires a unique author/channel/exact-text match within one second in
the chat store and a unique message in Discord. It never guesses among repeated
lines. A historical message without a unique match shows why feedback is
unavailable. DM channels stay `dm:<guid>` in both stores.

## Changes and freezes

Every mutation needs the console key and its own Origin, enabled console control,
and an available running mind. The mind checks control again. No control means
read-only; no bridge or logged-in game session is required. The new `learn` op
supports:

- `freeze`: `{frozen: true|false}` for the global switch.
- `family`: `{family, frozen: true|false}` for a known arm family.
- `arm`: `{id, verb: "accept"|"reject"|"retire"}`. Controls cannot be retired.
- `lesson`: `{id, verb: "approve"|"veto"|"expire"}`. At most five active lessons
  per member; expired or vetoed lessons cannot be approved.
- `weights`: `{values: {explicit, engagement, emote, quality}, version}`. Values
  must be finite, between zero and one, and sum to one. The page shows a
  confirmation; the mind rejects an edit based on a stale version.

Freeze overrides live in `meta` under `learn:control:` and survive restarts.
They add to the corresponding `learn.freeze` / `frozen_families` choices and
cannot lift TOML freezes, enable learning or change the feature flags in `mind.toml`. Explicit owner
edits remain available while frozen. Global freeze pauses conversational reward
scoring, learned prompt application, bandit selection/credit/rejection/automatic retirement,
coach lessons/proposals, preference draft/vector capture, and voice-fingerprint
updates. Writers recheck after asynchronous work before saving. It does not
disable chat, delivered recipes, raw observations/feedback, memory, safety guards, game simulation or
combat training. Existing learned state remains stored. An unfreeze resumes the
configured learners on their next eligible work; it does not start a night run.
Family freeze pauses that family's selection, credit and automated arm changes.
Vetoes persist independently of expiry; the coach cannot resurrect a vetoed note.

The weekly report slot is reserved for task 43's numeric JSON object at
`meta['learn:weekly-report']`. It accepts bounded nested numbers only; without a
report it says **Not set up yet**. Task 43 must also honor `learn:control:veto:*`
when deciding promotions. This task adds no migration or live configuration.

Validation uses scratch databases, the fake mind/bridge/model browser rehearsal,
keyboard actions, 390 px layouts and both themes. No live service or model is
needed to exercise the Lab.
