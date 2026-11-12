# Guild Discord engine

`[server] enabled = true` enables the quiet-window lease, Discord knowledge and
autonomous conversations, reactions and private openers while he is away.
The table defaults off; invalid settings disable it. Restart the mind and console
after changing the switch. New server lines also require `[guards] enabled = true`;
without it the engine logs one `server_needs_guards` notice a day and stays quiet.
With claims disabled it also logs `server_needs_claims` once per local day:
contradiction checks need `[claims] enabled = true`.
It never sends game chat, game emotes or unattended voice audio.

The default quiet window remains 03:00–08:00 local. Conversation starts average
2.5/hour before time-of-day and chattiness weights; while he is in game the rate
is multiplied by `in_game_share` (default 0.5). `conversations_per_hour` accepts
0–12, `in_game_share` 0–1, and `dms_per_day` 0–12 (default 3). `reactions` defaults
true inside the otherwise disabled server feature. Changes require a mind restart.

At most two conversations run, with one per channel and 5–20 messages each.
Subjects come from observed raids, loot, life beats, public plans, calendar dates,
public rival activity and officer-only business. A seed is held for three days
after the first attempted opener, even if its lines are dropped; channel-purpose
seeds renew daily. Live plans, officer attendance and trials return at most once
per ISO week. Private poaching offers never become seeds. Only a life beat's owner
may open it, and only observed participants may open his group's kill or loot.
An unavailable owner leaves the seed unused; a dropped restricted opener ends
the conversation. Later speakers see only what was actually posted. Officer
meetings stay in officer chat. Purpose seeds are topics, and rival activity carries
no invented contents.
New and running conversations stop during raid holds and his login quiet period.
His post ends the conversation in its channel and holds new starts there for five
minutes; D1 still answers him. Threads also end
on shutdown, quiet-window entry, lease loss, a deleted parent or two dry turns.
Members currently in play, away or unavailable are excluded and checked again
before sending. Late replies retain the exact conversation's message IDs.

Each conversation owns its pacing guard and drawn chain length, so these messages
do not consume the game's talk budget. The live model scheduler keeps them at
BACKGROUND priority, below answers to him. Queueing an answer owed to him cancels
that member's in-flight server turn
or DM opener, releasing her review lock before the answer waits; pending pipeline
speakers are excluded from new turns. Banter yields its Discord leg to this engine;
its game-chat leg is unchanged. Reactions use observed ties
and no model call, at most three per message, with visibility and ties checked
again when their delay ends. Feuds never react; a dramatic rival can only roll eyes.

DM openers run only while he is out of game, spread across server hours. Their rate
follows the world chattiness dial: zero disables openers and the gap rate scales
with the dial relative to four. Logout re-draws the gap, with at least five minutes
before an opener. A member opens at most once per local day and waits for his answer or 48 hours before
reopening. Successful posts consume the daily limit atomically; rejected drafts
do not. Men and members with forward dial zero use casual openers. Pending DMs
drop when he logs in or the recipient/owner scope changes. His own DM cancels an
opener still being selected or written and gives that pair five minutes of quiet;
the normal D1 answer still runs.

Members have deterministic regional status text and observed game participation.
The mind writes `server:playing` every 60 seconds; the console ignores it after
180 seconds. His online group and the accepted raid rosters override scheduled
presence. Planned members who have not joined do not count. A called raid has no
roster yet. Discord activity never advances the game activity clock.

With `[talk] aware = true`, Discord cards show presence, recent company, claims,
directed emotes and public guild facts. Company means the channel she is speaking
in; her last reading channel is always a layout channel, never a DM. Romantic
emotes appear only in her DM with him. Officer bank details stay in officer
channels. DMs retain conversation, presence, claims and scoped drama only. Claims
cross from game to Discord and back without widening a private or officer audience.
Game-local claims retain their existing card scope with the server on or off.

`[guards] enabled = true` reviews `server:` turns with semantic checks
enabled, even if `semantic_enabled` is false. It compares the member's own last
64 Discord lines and the channel's last 50 lines within a day. Private repeat
evidence never enters context. A cold embedding batch contains at most 32 texts,
including the draft and anchor, prioritizing a late reply's parent and thread
before newest repeat evidence. Older cached vectors
and lexical evidence still participate. State reads run off the event loop.
Openers use `anchor_cosine = 0.35`; short agreement,
laughter, congratulations, thanks and sympathy become reactions. Without a parent,
filler is dropped. ASCII emoticons such as `:P`, `:D`, `xD`, `:)`, `:(` and `<3`
do not turn filler into a text line. D1 replies retain their existing guard rules.

The real-embedding replay covered 1,082 lines: 804 sends (74.307%), zero rewrites,
232 reactions (21.442%) and 46 drops (4.251%) at the prior defaults. The 0.20
anchor threshold flagged no opener. At 0.35, the invented probe flagged 0 of 18
on-topic openers and 21.1% of 90 off-topic openers. On replay, it flagged 6 of 155
openers (3.9%) against a generic anchor, or 21 (13.6%) against a bare channel
topic. Those replayed openers were not seeded, so these are flag rates, not a
false-positive estimate for seeded conversations. The shared `parent_cosine = 0.20`
and `thread_cosine = 0.25` stay unchanged; they flagged none of the 638 scored
replies with this embedding model and remain inert in that replay.

The additional durable state uses existing `meta` rows: `server:day:<date>` counts
successful conversations, messages, reactions and DMs; `server:seed:<key>` records
admissions; `server:conversation:<id>` prevents double-counting a conversation;
`server:dm:open:<guid>` and `server:dm:day:<date>:<guid>` fence openers; and
`server:recent` retains the last twenty conversation summaries without message text.
The engine prunes its dated counters and old timestamp receipts after seven days.
Conversations themselves are not resumed after a restart. Guild source/state reads
use read-only worker connections with a two-second SQL budget; the live loop only
writes receipts and performs small indexed validity checks. Item names use the
optional read-only attached library, otherwise item IDs.
Posts and their budget receipts commit before in-memory delivery hooks run.
A malformed daily counter stops new server posts and logs `server_day_counter_bad`
once; D1 answers and reactions remain available without repairing or
overwriting the counter.

No migration or native rebuild is required. Claude's real-embedding replay informed
the anchor default above; implementation regressions use fake services and vectors.
Task 54 still owns the full-day rehearsal, including low-priority-slot contention.
Deploy after review with the normal mind-only
`bash scripts/ac-release.sh <commit>` flow, then restart the mind/console only when
the replay is accepted and the owner is ready to enable the switch. Nothing in the
implementation or tests changes live configuration. Room topics, away voice seats,
the full-day rehearsal and the status CLI remain task 54.
