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
is no mute-toggle event to observe. No D1 Discord sender is wired by this change;
its later adapter must supply confirmed hearing and scoped line IDs.

`ignored` requires an addressed heard line, 120 seconds without its reply,
recent owner activity, online/not AFK, no observed combat interruption, and an
authoritative out-of-combat observation no older than five seconds both at send
and expiry. **Today's
bridge has no general combat/rest observation, so production ignored capture is
conservatively suppressed.** Boss engagement proves combat; a kill/wipe does not
prove outdoor rest. `Signals.combat_observed(bool | None)` is the narrow future
observer entry point. Existing activity evidence is owner chat or zone changes,
not every movement. Tests supply synthetic authoritative rest explicitly.

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
