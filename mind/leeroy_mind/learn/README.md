# Numeric line rewards

`[learn] enabled = true` enables recipe writes and the model-free `rewards` night
stage. Missing, malformed, unknown or non-boolean settings mean off. Normal
application migration creates the empty tables; disabled learning writes no
recipes, rewards, weights or reward plans and changes no prompt or send decision.
Signals are separately opt-in. Without observations a score is zero, not evidence
that the owner disliked a line.

`reward(conn, line_id, *, now=None)` is read-only and returns a `Reward` with
numeric parts and the weight version. Supply receipt seconds for deterministic
replay; otherwise it uses the current time. Missing learning tables return
`available=0`. No text is returned, persisted in rewards, or logged.

## Formula and coverage

Defaults are 0.40 explicit + 0.25 engagement + 0.15 emote + 0.20 quality, minus
1.0 muted, 0.5 dial-down and 0.5 negative nudge. Clip to [-1,1]. Weights are
append-only versions with timestamps; the latest valid version as of scoring is
used. `weights.change` validates finite nonnegative values summing to one and
joins the caller's transaction. Read-only calls use version zero defaults when
no valid version exists; enabled scoring first persists a version. There is no
weights editor yet.

The owner veto is outside these weights: a thumbs-down forces at most -0.40,
even if engagement is high or an edited weight would ignore the vote. Neither
the fixed penalties nor this veto can be adjusted by a learner.

Engagement averages only observed components: replied (or an explicit observed
ignored), speed, length, continuation. The signals contract stores latency as
seconds/120, so speed is 1-latency; length is already codepoints/500. For each
component, a rolling 14-day median uses prior observed lines in the same channel
kind and **UTC hour**, excluding later observations and the current line.
Each read is bounded to the latest 2,048 eligible prior lines. Divide by twice a
positive median, capped at one: median engagement is 0.5. With no positive median
use the already normalized value. Parts include sample counts and medians;
missing observations are never invented zero samples. This is a provisional
normalization, not a calibrated satisfaction measure.

Friendly/romantic emotes add, rude emotes subtract, capped to [-1,1].
Quality has a base of one only when a completed signals receipt proves that its
bounded detectors ran on this supported game row, or an actual live guard
verdict is recorded. Unsupported Discord rows and overlong rows skipped by
signals do not earn that base. Missing coverage starts at zero. Subtract the
four detector hits and the maximum guard cost (rewrite 0.25, react 0.5, drop 1);
clip quality to [-1,1]. Retrospective `judge_guard_*` observations are screening,
not a claim that anything was actually rewritten or dropped. Live guards are
not wired by this task; recipes currently say `not_run`. These incomplete
detectors cannot establish general correctness or hearing.

## Caps, time and channel scopes

Positive credit has a fixed geometric volume discount: line ordinal n among
this member's UTC-day mind-origin lines gets factor 0.9^n. Thus even infinitely
many perfect lines cannot earn more than 10 positive points per member/day.
The ordinal includes lines without signals or recipes, so spam cannot evade it.
Negative values, including the owner's veto, are never weakened by that volume
discount. A separate `decayed` read helper and report total use a 21-day half-life;
stored scores remain immutable snapshots. No variant arms exist yet.

Discord channels and DMs have their own baseline/report kinds, identified by
the shared Discord channel helpers. They cannot supply a game baseline or
game detector receipt. The per-member daily positive budget intentionally spans
both media; it is a numeric credit limit, never game activity, an AFK clock or
a game session. Existing DM/whisper privacy is unchanged. No content flows into
another context.

The night scores at most 500 cast-member lines from the latest 24 hours inside
its frozen window. A saved ID selection survives resumes and later backlog
inserts. Each reward and completion receipt commit together; existing rewards
are not overwritten. Scores describe evidence available then: later feedback
does not silently rewrite that night's snapshot. The stage uses no model.
Both normal startup and `nightly run` attach it when enabled, after signals if
that stage is present, otherwise after threads.

## Recipes and inspection

The central outgoing-line commit writes its recipe in the same transaction as
`chat_line`. Its primary key permits only one recipe per line. This includes
game, voice and Discord delivery paths. A failed recipe write rolls back the
chat row too; no world bookkeeping has happened yet. Existing disabled behavior
uses the original insertion path.

Recipes contain only the fixed template identifier/version, SHA-256 of effective
member and world dial values, model identifier, guard verdict, and empty arm and
lesson ID arrays. Dial values are captured at composition before any model wait;
the model ID is captured at the actual call. A dial change during generation
therefore cannot relabel the old prompt. Template text, names, prompts and persona content are absent.
Template identifiers distinguish live, multi-line bit and supplied-template
paths; supplied templates have model `none`. Version 1 identifies these base
templates; future template changes must update their recipe version. Generation
parameter attribution and arm/lesson assignment remain for their respective
learning tasks.

`python -m leeroy_mind learn report --data-dir <data> --days 14` opens SQLite
read-only, without migration, logging setup or any service. It reports counts,
mean rewards and discounted totals by member ID, channel kind and each recipe
field. Older schemas report unavailable. Days must be 1–3650.
