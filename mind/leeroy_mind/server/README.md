# Guild Discord engine

`[server] enabled = true` enables the quiet-window lease, Discord knowledge and
autonomous conversations, reactions and private openers while he is away.
The table defaults off; invalid settings disable it. Restart the mind and console
after changing the switch. New server lines also require `[guards] enabled = true`;
without it the engine logs one `server_needs_guards` notice a day and stays quiet.
It never sends game chat, game emotes or unattended voice audio.

The default quiet window remains 03:00–08:00 local. Conversation starts average
2.5/hour before time-of-day and chattiness weights; while he is in game the rate
is multiplied by `in_game_share` (default 0.5). `conversations_per_hour` accepts
0–12, `in_game_share` 0–1, and `dms_per_day` 0–12 (default 3). `reactions` defaults
true inside the otherwise disabled server feature. Changes require a mind restart.

At most two conversations run, with one per channel and 5–20 messages each.
Subjects come from observed raids, loot, life beats, public plans, calendar dates,
public rival activity and officer-only business. A seed is held for three days
after admission, even if its lines are dropped; channel-purpose seeds renew daily.
New conversations stop during raid holds and his login quiet period. His post
ends the conversation in its channel and D1 still answers him. Threads also end
on shutdown, quiet-window entry, lease loss, a deleted parent or two dry turns.
Members currently in play, away or unavailable are excluded and checked again
before sending. Late replies retain the exact conversation's message IDs.

Each conversation owns its pacing guard and drawn chain length, so these messages
do not consume the game's talk budget. The live model scheduler keeps them at
BACKGROUND priority, below answers to him. Banter yields its Discord leg to this
engine while enabled; its game-chat leg is unchanged. Reactions use observed ties
and no model call, at most three per message, with visibility and ties checked
again when their delay ends. Feuds never react; a dramatic rival can only roll eyes.

DM openers run only while he is out of game, spread across server hours. A member
opens at most once per local day and waits for his answer or 48 hours before
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
Openers use `anchor_cosine = 0.20`; short agreement,
laughter, congratulations, thanks and sympathy become reactions. Without a parent,
filler is dropped. D1 replies retain their existing guard rules.

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

No migration or native rebuild is required. Guard calibration uses a scratch
database and fake embeddings; its rates are replay measurements, not production
semantic accuracy or false-positive estimates. A real-embedding scratch replay
by Claude is required before enabling autonomous server lines live, with its check
recorded in task 54. Deploy after review with the normal mind-only
`bash scripts/ac-release.sh <commit>` flow, then restart the mind/console only when
the replay is accepted and the owner is ready to enable the switch. Nothing in the
implementation or tests changes live configuration. Room topics, away voice seats,
the full-day rehearsal and the status CLI remain task 54.
