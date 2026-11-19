# Signals

`[signals] enabled = true` records observations without changing talk decisions.
Missing, malformed, unknown or non-boolean settings mean off. Enabling needs a
normal application migration; `signals status` and `signals member 101` only open
`mind.db` read-only and never migrate. Both accept `--data-dir`. Output contains
UTC day, kind, source and count, without source words or names.

## Consumer contract

**Values are normalized, including the historically named `reply_latency_s`:**

| Kind | Value | Source |
| --- | --- | --- |
| `reply_latency_s` | elapsed seconds / 120 (multiply by 120 to recover seconds) | implicit |
| `reply_len` | min(Unicode codepoints / 500, 1); capped, not tokens | implicit |
| `replied`, `thread_continued`, `ignored` | 1 | implicit |
| `emote_friendly`, `emote_romantic`, `emote_rude` | 1 | implicit |
| `muted`, `dial_down`, `nudged_against` | 1 | implicit |
| `asked_back`, `laughed`, `affection`, `topic_kept`, `topic_changed`, `objected`, `sent_media` | 1 (Discord only) | implicit |
| `turns_after` | min(his further replies in that conversation / 5, 1) (Discord only) | implicit |
| `react_positive`, `react_negative` | 1 (Discord only) | implicit |
| `explicit` | +1 or -1 | explicit |
| `reason_*` | 1; hyphens in accepted reasons become underscores | explicit |
| `judge_repetition`, `judge_nonsequitur`, `judge_unawareness`, `judge_contradiction` | 1 | judge |
| `judge_guard_rewrite`, `judge_guard_drop`, `judge_guard_react` | 1 | judge |

The last row is **retrospective screening of already stored lines**. It does not
say a guard actually rewrote, dropped, reacted to or sent anything. Future live
guard telemetry must use a distinct contract. Missing rows are not negative
feedback, proof of passing a detector, or proof that a member was heard.

Rows have a `line_id` or reserved future `act_id`, member `guid`, kind, numeric
value, observation timestamp and source. Each target/kind/source is unique.
The explicit control op replaces that line's current vote and reason chips:
`feedback` args are `{"line_id": 12, "value": 1, "reasons": ["funny"]}`.
Accepted reasons: funny, in-character, boring, repetitive, weird, too-much,
wrong-facts. Normal control key/enabled/stopping gates apply, and signals must be
enabled. The line must exist and originate from a member. No console UI yet.

## Attribution and coverage

Replies select the latest runtime-observed member line visible by the channel
rules below, addressed to the owner or spoken in his group, in the same
channel/pair and within 120 seconds.
Game channels also require the owner online; officer requires known officer role.
The owner's absence from the cast-only audience is not absence from guild chat.
Party/raid require the owner's current shared group and share a scope only until
the group changes. Local say/yell additionally require matching map/zone evidence
from owner game chat within 120 seconds, invalidated by a zone change. Distance
is unavailable, so this is a conservative group/area proxy, not exact range.
Unknown officer authority or position suppresses capture. Voice requires the
owner's current room and the existing Comms hearing check. Unsupported external
channels fail closed. Whispers and DMs keep their pair. Repeated exchanges in
the same stored thread/channel/member within 120 seconds set `thread_continued`.
Commands count as activity, never replies. Replayed backlog is excluded.

Emotes select the latest heard line from that target in 60 seconds. Dial and
nudge interventions use 120 seconds and the nudge's channel scope. Tuning must
actually lower effective talkative, forward or drama. Only negative nudge cues
(`stop`, `avoid`, `less`, `quit`, `don't`, `do not`, `no more`, `tone down`) classify
as `nudged_against`; this is a limited lexical heuristic, not intent inference.
Voice records only a confirmed `Played(dropped, muted)` delivery receipt. There
is no mute-toggle event to observe. Discord has its own adapter (below).

`ignored` requires an addressed heard line, 120 seconds without its reply,
recent owner activity, online/not AFK, no observed combat interruption, and an
authoritative out-of-combat observation no older than five seconds both at send
and expiry. **Today's
bridge has no general combat/rest observation, so production ignored capture is
conservatively suppressed.** Boss engagement proves combat; a kill/wipe does not
prove outdoor rest. `Signals.combat_observed(bool | None)` is the narrow future
observer entry point. Existing activity evidence is owner chat or zone changes,
not every movement. Tests supply synthetic authoritative rest explicitly.

## The guild Discord

His console Discord is observed too (migration `*_discord_signals.sql` adds the kinds above).
Before it, a Discord line was never visible to the capture and his posts never reached it, so a
week of DMs produced no observation at all.

- **Which lines wait for him.** A member's Discord line addressed to him: her DM to him, an
  answer to his post, a line naming him. The guild Discord is his own server, so he reads every
  channel and group DM whether or not he is in WoW; a DM counts only when it is his. His WoW
  login, logout, AFK, group and combat never touch Discord's attribution.
- **His reply.** His next message in that conversation (the same DM, group DM or channel) within
  30 minutes is his reply to the newest waiting line there. A message he sent as a reply to one
  message by hand credits that member line, addressed or not, and only it: a reply to a bot's
  row, to a line from before a restart or past the 512 held, credits nobody. In a channel or
  group DM where several members wait, his message credits the one it names (her character
  name as a word), else nobody. A brb/back line (`pair_line`) and a photo's caption never wait:
  the real answer before them takes his reply. A thread is its own conversation: his post in a
  thread answers a line in that thread, never one in the channel's feed, and the other way
  round. A post of his made while her answer was still being written (stored before it, and
  not among the posts that answer was built with: `discord/covered.py`) is never his reply to
  that answer; it answers whatever line of hers was waiting before it. `reply_latency_s` is elapsed / 1800
  for a Discord line (multiply by 1800), `reply_len` as in game. A message of his with no waiting
  line before it (he wrote first, or twice running) is no observation. Discord and game lines
  never answer each other.
- **What the reply says** (`signals/discord.py`, the lexicons talk already reads him with):
  `asked_back` (a question of his own: not pushback, not her line said back, and when shorter
  than four words only if it keeps her topic, so "that's it?" and "really?" are not interest),
  `laughed` (never with `objected`: "lol what" is pushback), `affection` (thanks, praise, a
  flirt, a heart), `topic_kept` (a shared content word), `objected` (talk's pushback cues, "you
  already said that", "not what I asked", "did you even read", "you're not making sense",
  "that's it?", "is that all you have to say?", a bare "what", "huh", "wtf", "ew" or "stop");
  "no" or "k" alone is a reply and nothing more. `topic_changed` (she asked a question
  and he, sharing no content word, asked about something else: an answer in his own words shares
  none either, so only his own unrelated question counts; observed only, the reward gives it
  no weight), `sent_media` (his photo or voice
  note; a voice note's words are not read). These are narrow lexical heuristics with no
  calibrated accuracy.
- **The talk going on.** `thread_continued` when the same pair exchanged within the 30 minutes
  before; `turns_after` rises on her earlier answered lines (the last 8) with each further reply
  of his in that running conversation.
- **Emoji.** His emoji on a member's message: listed positive ones `react_positive`, listed
  negative ones `react_negative`, the guild's own emoji and unlisted ones nothing. The rows follow
  his emoji as they stand: removing one removes its row.
- **Silence is nothing.** A Discord line he never answers just stops waiting. `ignored` is never
  recorded for Discord, and his leaving is never read as dislike.
- **Where it is written.** His Discord actions are written through the mind's own connection,
  on the event loop between its units, never on the capture's second connection: a commit from
  a second connection while the pipeline holds a read snapshot makes the pipeline's next write
  fail at once, and that write can be an answer he is owed. Each such unit takes the write lock
  before it reads (`Store.write_first`). The Discord answer's own unit does too and retries a
  busy store once (`discord_store_retry`), each try waiting 2.5 s for the lock, so both together
  wait no longer than the one try did. Game observations still use the worker.
- **Privacy.** The queue carries line ids and flags. The two lines' text is read only inside
  the classifier, which stores kinds and numbers; logs carry codes and counts. A DM line is
  credited only by his message in that DM.

`python -m leeroy_mind learn backfill-signals --since YYYY-MM-DD [--go]` replays the stored
Discord exchanges through the same attribution (`signals/backfill.py`): the dry run opens
`mind.db` read-only and prints counts (his messages, answered lines, observations by kind, lines
by DM / group DM / channel, and per arm family the uses that would carry an observation). `--go`
needs the mind stopped (it holds `mind.lock`), snapshots first (`mind-pre-backfill-signals-*`),
never migrates, writes only rows the table lacks (each at the time it happened) and leaves a
numeric receipt (`signals:backfill:<time>`). It never reaches further back than 90 days. Which
lines were addressed to him is rebuilt from stored facts (a DM to him, a reply to his message, a
recorded trigger or learning parent that is his line); an emoji he removed is gone. As live,
a voice note's words are never read (transcribed or not), and pair lines and captions never wait.

## Execution and retention

Talk hooks only enqueue IDs, codes, lengths and flags. A background task drains
a 512-item inbox through one owned SQLite worker thread, joined at shutdown.
Pending lines, heard lines and exchange histories each cap at 512. Capture is
best effort: restart loses pending evidence; queue loss discards evidence across
the gap; write failure resets attribution. Neither failure may invent an ignored
line. Logs contain only codes, counts and exception classes. A busy database can
lose observations without delaying a send; explicit writes return failure.

The night stage follows threads, screens at most 200 outgoing lines from the
night's latest 24 hours, and uses the shared read-only guard adapter and task12
single-candidate helpers. It preserves their time/privacy fences, never calls
destructive batch detectors, and writes resumable per-line receipts. Lexical
checks need no model; contradiction needs enabled claims and embeddings;
semantic relevance needs enabled guard semantic checks. Guard replay runs only
with guards enabled. World facts use the shared historical facts loader; its
metadata scan is not subject to the 200-line cap. These detectors are incomplete
heuristics and have no calibrated accuracy claim here.

Rows older than 120 days are pruned on writes (at most hourly in capture) and
night screening. Foreign-key deletion follows a removed chat line. Old schemas
without the table are reported unavailable, never migrated by capture or CLI.
