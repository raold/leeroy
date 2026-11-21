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
0–12, `in_game_share` 0–1, and `dms_per_day` 0–12 (default 3). `reactions` and
`channel_topics` (below) default true inside the otherwise disabled server feature. Changes
require a mind restart.

At most two conversations run, with one per channel and 5–20 messages each.
Subjects come from observed raids, loot, life beats, public plans, calendar dates,
public rival activity and officer-only business. A seed is held for three days
after the first attempted opener, even if its lines are dropped; channel-purpose
seeds renew daily. Live plans, officer attendance and trials return at most once
per ISO week. Attendance is one weekly officers' thread (2026-11-15), never one seed per
member: the regulars dropping off (`seeds.dropping_off`), who missed 3 or more of the last 4
raids they could have joined after attending at least 2 of the 8 before them. Only raids with
attendance rows count (a failed offline start counts for nobody), a raid he called counts only
for its roster and bench (it records only his group), and an offline raid for everyone. A
member on leave is never named. Up to six are named and the rest counted, and there is no
thread when more than a tenth of the cast would be named, nor until six raids with attendance
have finished (four in the window, two before it). Read-only on the live history, at the end
of each of the last four raids: 0, 0, 0 and 4 of 145 flagged (the per-member rule of the first
version would have named 66 and 72 in two of those windows). Public decisions (a lineup, a
loot rule, a boss strategy) count as raid or loot business by their channel
(`seeds.seed_subject`); officers' ones stay officer business. An opener draws its kind of
subject first (`seeds.SUBJECTS`: raid, loot, life, meme, chat, rival, calendar, officer, plus
his godmode intents; weights `rhythm.SUBJECT_WEIGHT`, scaled by the godmode hangouts), then a
seed and channel within it by their own weights, so a kind with many seeds (a life beat per
member) no longer crowds out a singular one. Before the change the per-member officer seeds
(33%) and the life beats (53%) made 86% of the drawn openers on the live seeds; after it, raid
25%, loot 21%, life 16%, chat 13%, meme 12%, calendar 8%, officer 6% (a static draw over one
snapshot; a used seed then rests for its freshness window). Life beats written from
2026-11-15 on are a short first-person chat message (`nightly/life.py`), not prose. Private
poaching offers never become seeds. Only a life beat's owner
may open it, and only observed participants may open his group's kill or loot.
Native `loot_won` events carry no member list today, so his loot seeds nothing
(fails closed); only his group's boss kills seed his day.
An unavailable owner leaves the seed unused; a dropped restricted opener ends
the conversation. Later speakers see only what was actually posted. Officer
meetings stay in officer chat. Purpose seeds are topics, and rival activity carries
no invented contents.
With `[decisions]` on, each guild decision seeds one conversation per stage (proposed, poll
open, settled) with a code-built fact and its stakeholders as `about` (`decisions/seeds.py`).
With `[talk] recall_diversity` on (2026-11-16; `seeds.varied`, read each tick): the officers'
weekly attendance thread is the only attendance seed, and starts at most once in 7 days
guild-wide whatever the ISO week (the W46 and W47 threads had started hours apart); a life
beat or plan about the bench or attendance seeds nothing. A topic seed (`seeds.TOPIC_KINDS`:
life, plan, purpose, officer, rival) whose fact's content words are 0.5 Jaccard or more like
one that opened a conversation within 7 days is passed over while any other seed is
available; raid, loot, calendar, decision, intent and culture seeds are new events or keep
their own cooldowns. Its receipt (`server:subject:<seed key>`, written when the opener is
posted) holds the words as hashes, never the fact, and the daily sweep drops it after a week.
In practice a channel's purpose is talked about about once a week instead of once a day while
other seeds wait. Off: no receipt, no filter.
New and running conversations stop during raid holds and his login quiet period.
The inherited login quiet period is 180 seconds. The D2 plan's ten-minute review
target is not implemented or certified by this rehearsal.
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
and no model call, with visibility and ties checked again when their delay ends.
Each message first draws how many react, from a skewed (negative binomial) count
whose mean (0.9) grows with how reactable it is: a laugh or joke, a meme or photo,
a win and his own post draw more; a question and a reply of a few words fewer. Most
messages get none or one, some two to four, a rare popular one up to eight; then
that many members who may react are picked by tie, and later reactors often pile
onto an emoji already there. Feuds never react; a dramatic rival can only roll eyes.
(Until 2026-11-15 each member reacted on her own at 12%, so 62% of member messages
hit the old cap of three.)

A reply never answers its own author's message: the speaker is drawn from the cast
minus the last speaker and the parent's author, and when only that author could
answer an earlier message the reply takes the last one instead. A member who is the
seed's subject is told to say it in the first person. The thread is the messages each
turn stored (`TurnMeta.discord_messages`), never a lookup by turn id; turn ids now
carry their run's nonce, and older rows' ids restarted at `t1` each run. With
`[talk] realism`, 12% of replies are drawn as a quick burst: a couple of sentences
split into 2-3 messages a few seconds apart (realism's ninety-second cooldown
applies); only the first is a formal reply.

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
laughter, congratulations, thanks and sympathy in reply become reactions 40% of the
time (`guards.review.FILLER_REACT_SHARE`, drawn from the speaker, parent and words,
never the clock); the rest are posted as their short message, exempt from the
repeat, nothing-new and relevance checks. Without a parent, filler is dropped.
ASCII emoticons such as `:P`, `:D`, `xD`, `:)`, `:(` and `<3` do not turn filler
into a text line. D1 replies retain their existing guard rules.

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
Deploy after review with the normal mind-only
`bash scripts/ac-release.sh <commit>` flow, then restart the mind/console only when
the replay is accepted and the owner is ready to enable the switch. Nothing in the
implementation or tests changes live configuration.

## Channel topics

`[server] channel_topics` (default true; read at mind start with the rest of the table) keeps
each channel to what it is for (owner, 2026-11-21: "the discord channels like #memes are not
topical at all its just random prattling").

Where the posts came from: read-only on the live `discord_message` rows and the `say_sent` log
events (ids and counts only), the 882 member posts outside DMs of the three days before were all
server conversations but two D1 answers and three unattributed. #irl's 177 were life beats (151
replies, 26 openers); #general's 236 plans (100), life beats (58), jokes (29), birthdays (17)
and his day (14); #loot-and-dkp's 211 loot wishes (207) and raid recaps (4), and the class
channels' 92 loot wishes; #food's 48 life beats (46) and two D1 answers; #professions-and-gold's
42 plans (27) and fishing events (15). Over nine days #food's 206 posts were 186 life beats and
only 93 of the 206 named any food; #memes' 32 were text memes and their replies (18), the
channel's own purpose seed (9: "talk about this topic" with nothing posted) and a meme picture's
replies (5). Three causes: each seed kind named fixed fallback channels whatever its words (a
life beat went to #food, a plan to #professions-and-gold or #lfg-and-alts, a joke to #memes);
every channel's purpose seeded a conversation of its own, also where talk needs something posted
(#memes, #pics, #logs); and replies ran 5-20 messages on "keep this conversation going
naturally" with nothing holding them to the channel, so a meme's thread drifted into coffee,
work and the calendar. Volume made it worse: #general 82 and #irl 78 member posts a day.

With it on:
- Every layout channel has a purpose (`discord/layout.py` `about` and `not_here`; #general
  takes anything). A conversation's seed keeps only the channels it fits (`server/topics.py`
  `route`): a life beat, a weekend or a plan goes where its words take it (#food, #pets, and
  for a plan the WoW content channels, by words no one uses outside them:
  `discord/topics.py` `subjects`), else #irl when it is her real life, and #general; a joke
  opens only in #general; a channel's purpose seeds nothing where talk rests on something
  posted (`event_only`: #pics, #memes, #logs, #applications, #bank-requests); a public seed
  with nowhere left falls back to #general. His godmode intents, decisions, bank requests,
  heroics and a group's introduction keep the channels their modules chose.
- #memes holds only meme posts: a text meme (asked for in a meme format, "POV: ...",
  "nobody: ... me: ...", "me when ...", "... be like") or the night's meme picture, and one to
  three reactions, each answering the meme post itself (its poster may answer a reaction), a
  quick message told to react in a few words.
- No channel floods: a channel takes `topics.DAILY` member posts a local day (#general 60,
  #irl and #loot-and-dkp 30, #officer-chat 25, #raid-planning 20, #food and #strategy 15,
  #pets and #pics 12, #memes 8, a class channel 10, any other 15), counted from the store
  (`posted_today`, in the snapshot's read). A conversation opens only where `MIN_ROOM` (3)
  more fit, runs no longer than the room left, and ends `channel_full` when the channel is
  full. The guild's own business above is never held back; answers to him are counted but
  never refused.
- Every server turn on a channel with a purpose (an opener, a reply, an aside; never #general,
  a DM, a group DM, a template or the post his godmode intent asked for, whose subject is his)
  gets the channel's hold line, "Stay on what this channel is
  about (<about>): <not_here>." (`discord/topics.py` `hold_text`), as #394's `[talk]
  wow_topic` holds a WoW turn; outside #irl her unasked topics, life thread, "lately" and
  where she lives are left out, as a WoW hold leaves them. A WoW channel's turn under `[talk]
  wow_topic` keeps that switch's own hold and prompt exactly; with `wow_topic` off the
  channel's purpose holds it instead.
- The guards check the same turns (`Draft.channel_topic`, `discord/topics.py` `off_topic`):
  a draft clearly off its channel's purpose is the soft `channel_topic` (`channel_meme` in
  #memes), one rewrite with its hint in words, and dropped if the rewrite still strays. It is
  lexical and narrow: any word of the channel's own subject keeps a draft on it; off takes
  two distinct words of another subject the message answered did not bring, or one in a
  message of 12 words or more (a word the game shares with everyday English, "fire", "kill",
  "party", never counts against a draft); in #memes an opener without a picture must be a
  text meme, and a reaction runs at most 20 words and brings in no real-life, food or
  calendar talk of its own. Replayed read-only on the nine days above it flags 88 of 3,001:
  18 of 32 in #memes, 21 of 206 in #food, 22 of 699 in #irl, 5 of 297 in #officer-chat, 3 of
  334 in #loot-and-dkp and none in #general. An answer owed to him is never checked or held:
  his posts in a channel are answered there whatever their subject. A turn that strays is
  dropped rather than moved to #general: its parent and thread are in its own channel.

Off: the seeds, lengths, prompts and guard verdicts are as before. Rollback without code:
`channel_topics = false` under `[server]`, restart the mind (older code refuses the key and
turns the whole table off: remove it before rolling back). No migration, no native rebuild.
`tests/server/test_channel_topics_rehearsal.py` runs six fake hours through the real engine,
rhythm, routing, pipeline and guards with a fake model that drifts on purpose in #memes.

## Settings and the quiet-window budget

```toml
[server]
enabled = false
quiet_window = ["03:00", "08:00"]
conversations_per_hour = 2.5
in_game_share = 0.5
dms_per_day = 3
reactions = true
channel_topics = true
```

Times require two distinct zero-padded `HH:MM` values; a window may cross midnight.
An unknown key, wrong type or out-of-range value disables the entire table. With
the switch off the existing night clock, prompts and leases remain in force.
The server holds a live lease outside the window and releases it at 03:00. A night
still running at 08:00 receives at most one hour of overrun before the lease
preempts it; unfinished work resumes inside the next window. His game login or
Discord activity can preempt a night separately; after the Discord lease's ten
idle minutes the night can resume within that same window.

The booth renders inside the quiet window only while the server is on. His Render selfies
button (console/RENDER.md) renders at any hour he is out of WoW: it releases the server lease for
its run, keeps it released while it renders and takes it back at its end. Generic
voice room topics guide room conversation. Silent away seats represent scheduled
presence only: they start no session or audio and refresh every five minutes
between 17:00 and 03:00 local. The tick calculates regional presence directly,
without reading Discord messages. It clears seats outside those hours, while he
plays, or during a real voice session, and excludes members in play or away.
Snapshots become stale after fifteen minutes; a crashed voice session cannot
hide a newer snapshot. The browser uses server age plus elapsed monotonic time,
so a phone's wall-clock skew does not hide fresh seats.
`server:voice_seats` stores that snapshot alongside
`server:playing`; neither is a chat or a game-activity clock.

At parallelism two, five quiet hours provide ten model call-hours. The earlier
read-only timing sample (November 2026) was:

| Night | Wall hours | Model call-hours | Context |
| --- | ---: | ---: | --- |
| 11-11 | 1.9 | 3.8 | Reflection 89 minutes, encounters 20 |
| 11-10 | 2.1 | 4.0 | Ordinary night |
| 11-09 | 4.6 | 6.8 | Two days; reflection 157 minutes, notes 36, encounters 48 |
| 11-07 | 12.5 | 3.4 | Enrichment and photo work waited out logins |

The measured call-hours fit ten; the long interrupted wall time does not promise
completion in five hours. Keep the default 03:00–08:00 and inspect unfinished
nights rather than increasing chatter based on a synthetic message estimate.

## Status and operator checks

`python -m leeroy_mind server status --data-dir PATH [--json]` opens `mind.db`
read-only, without migration, model calls, service connections or a control key.
It reports the startup file's config, local quiet window and current membership,
today's successful-post counters, five latest ended conversations (channel, seed
kind, message count and end reason), and outstanding DM opener timestamps. It is
a durable receipt view, not proof of current lease ownership or running config:
editing `mind.toml` still requires a restart. Missing or malformed state reports
`server_status_unavailable` instead of repairing the database.

Before enabling server lines, the owner reviews real-embedding off-topic and
relevance replay on a scratch database copy. The earlier replay above informed
the 0.35 anchor default; synthetic unit vectors cannot establish live quality.
After one live day, run
`python -m leeroy_mind eval talk --data-dir "$D" --since 1` against a scratch copy
and compare Discord contradiction, repetition, non-sequitur and unawareness rates
with guild chat. Preserve counts and the input/time window for comparison.

The fake-only regression rehearsal uses its own loopback bridge and control
ports, FakeLLM, synthetic embeddings and a fake-admin arbiter. It advances a real
MindApp from 07:30 through 09:30 the next day, including his 19:00–21:00 session,
night interruption by a 05:00 Discord post, and shutdown cleanup. Its controlled
availability and ample synthetic topic stock isolate admission rates from roster
coverage and source exhaustion (with `channel_topics` off: its daily room per channel would
end the starts it measures once the three stocked channels fill). Interarrival draws are fixed at their exponential
mean for this wiring test; the separate rhythm tests exercise the production
distribution. Its phase-count acceptance band is ±40% of `starts_per_hour`
integrated over available hours. It is not a production
messages-per-day benchmark.

One controlled follow-up run admitted morning/day/evening/late starts of
6/14/16/9 against integrated targets 5.25/14/18.75/10 (zero in the dead phase).
It stored 45 conversations, 386 server messages including five DM openers, two
additional D1 replies and 142 reactions. His game question was answered while an
extraction job held the low slot and a background embedding remained queued.
Scheduling of real SQLite workers can change aggregate counts on later runs;
the assertions check the band and boundaries, not an exact daily total.

The contention probe preserves the real single low-priority slot and the real
five-second guard embedding timeout. With an EXTRACTION job occupying that slot,
the sampled review dropped as `review_unavailable` after 5.014 seconds; a
BACKGROUND occupant produced the same drop after 5.016 seconds. Concurrent
REPLY_HUMAN embeddings used the reserved slot in 0.002 and 0.001 seconds.
Both saturated samples dropped (2/2); neither rewrote. This demonstrates the
remaining P3-7 limitation: priority protects his request, but sustained low-slot
load can dry out conversations. These artificial held jobs are not a live-load
frequency estimate. The test prints fresh timings on each run.

`SERVER_DAY_SYNTHETIC` reports actual guard verdicts for that run's `server:`
conversation and DM attempts: total reviews, send/rewrite/drop/reaction counts,
and rewrite/drop counts and rates per verdict code. The denominator is all
review calls, including pre-send rechecks and rewritten drafts; codes may co-occur, and an absent
code does not establish that an independent check ran and passed. Fake embeddings
make these wiring measurements, not false-positive rates or live calibration.
The same run above made 772 reviews, all sends: every listed check's rewrite and
drop frequency was zero. A separate 60,000-message fixture measured the seat tick
at 1.422 ms of event-loop delay (50 ms regression ceiling) without a Discord-message query.

Controlled guard classification probes separately pair a clean draft with a deliberate failure:
off-anchor and parent/thread relevance rewrite one of two; own Discord repeat,
channel repeat and filler opener drop one of two. These constructed rates test
classification paths, not false-positive rates or real-embedding calibration.

D3 photo sending and his photo's private vision step are already implemented
behind their own switches. The photo rehearsal waits for real read workers
before each fake-clock step, retaining its 100-step, 200-second budget. Its
two-line conversation fixes prompt-reply draws: production's optional 600–2400
second late gap is covered by pacing tests, not this photo-wiring fixture.
The follow-up passed 100 consecutive photo rehearsals with 24 pytest workers
in 14.40 seconds, using only fake services and isolated temporary directories.
D4 adds decision
ledgers and guarded game consequences; D5 adds guild culture and server bots;
D6 adds day plans, intent to action, shared activities and operator nudges/undo.
Those later integrations have their own switches and acceptance evidence; this
rehearsal does not certify them.
