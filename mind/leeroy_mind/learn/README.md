# Numeric line rewards

`[learn] enabled = true` enables recipe writes and the model-free `rewards` night
stage. Missing, malformed, unknown or non-boolean settings mean off. Normal
application migration creates the empty tables; disabled learning writes no
recipes, rewards, weights or reward plans and changes no prompt or send decision.
The switch is read at startup (restart required), including the nightly settings.
Signals are separately opt-in. Unscreened game lines stay pending, including a
CLI night without signals; failed screening retries can supply the missing receipt.
Discord uses explicit evidence only, with no game screening credit. Unobserved
evidence is neutral, never an inferred dislike.

`reward(conn, line_id, *, now=None)` is read-only and returns a `Reward` with
numeric parts, the weight version, and a separate `cap_weight`. Supply receipt seconds for deterministic
replay; otherwise it uses the current time. Missing learning tables return
`available=0`. No text is returned, persisted in rewards, or logged.

## Formula and coverage

Defaults are 0.40 explicit + 0.25 engagement + 0.15 emote + 0.20 quality, minus
1.0 muted, 0.5 dial-down, 0.5 negative nudge and 0.1 ignored. Clip to [-1,1]. Weights are
append-only versions with timestamps; the latest valid version as of scoring is
used. `weights.change` validates finite nonnegative values summing to one and
joins the caller's transaction. Read-only calls use version zero defaults when
no valid version exists; enabled scoring first persists a version. There is no
weights editor yet.

The owner veto is outside these weights: a thumbs-down forces at most -0.40,
even if engagement is high or an edited weight would ignore the vote. Neither
the fixed penalties nor this veto can be adjusted by a learner.

Engagement averages only observed components: replied, speed, length, continuation. The signals contract stores latency as
seconds/120, so speed is 1-latency; length is already codepoints/500. For each
component, a rolling 14-day median uses prior observed lines in the same channel
kind and **UTC hour**, excluding later observations and the current line.
Each read is bounded to the latest 2,048 eligible prior lines. Divide by twice a
positive median, capped at one: median engagement is 0.5. With no positive median
use neutral 0.5 for an observed component. Parts include sample counts and medians;
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

The stored value is raw, clipped evidence. A separate `cap_weight` is 0.9^n,
where n is this member's line ordinal on the mind's local calendar day. Game and
Discord ranks are independent. The ordinal includes unobserved lines to prevent
volume from evading the cap. The report applies the weight only to positive
credit: infinitely many perfect lines cannot exceed 10 per member/medium/day.
Penalties and vetoes remain undiluted. Reports additionally apply a 21-day
half-life at read time. No variant arms exist yet.

Discord and DMs have separate baseline/report kinds, through the shared channel
helpers. They supply no game baseline, screening, session or activity. Existing
DM/whisper privacy holds; only numeric evidence is pooled, never content.

The night selects at most 500 cast lines: changed evidence first, then newest
unscored lines with a 14-day lookback. A saved selection survives resumes. Later
signals or a changed owner vote revisit an existing score even outside that
initial lookback; each update and its revision receipt commit together. A pending
count is logged without content. Missing screening leaves the unit incomplete
and retryable. Signal retention is 120 days; this layer does not invent evidence
that was never captured or restore pruned evidence. If the signals stage's own
cap never reaches a line, its reward remains pending rather than biased.

The stage uses no model. Normal startup and `nightly run` attach it when enabled,
after signals when present, otherwise after threads. Schema availability is
cached per Store at attach; disabled learning writes nothing.

## Recipes and inspection

The central outgoing-line commit writes its recipe in the same transaction as
`chat_line`. Its primary key permits only one recipe per line. This includes
game, voice and Discord delivery paths. The recipe uses a nested savepoint: a failed recipe rolls back only analytics,
logs a fixed code and keeps the acknowledged chat row and world bookkeeping. Existing disabled behavior
uses the original insertion path.

Recipes contain only the turn-kind/template identifiers and a 16-character
SHA-256 version of the effective built-in templates and active overlays, SHA-256 of effective
member and world dial values, model identifier, guard verdict, and empty arm and
lesson ID arrays. Dial values are captured at composition before any model wait;
the model ID is captured at the actual call. A dial change during generation
therefore cannot relabel the old prompt. Template text, names, prompts and persona content are absent.
Template identifiers name the turn kind and the template/overlay names actually
loaded at composition. Task-local tracing hashes those texts without retaining
them, consuming randomness or changing the prompt. Changes while delivery waits
cannot relabel the captured recipe. Supplied templates have model `none`.
Generation
parameter attribution and arm/lesson assignment remain for their respective
learning tasks.

`python -m leeroy_mind learn report --data-dir <data> --days 14` opens SQLite
read-only, without migration, logging setup or any service. It reports counts,
mean rewards and discounted totals by member ID, channel kind and each recipe
field. Older schemas report unavailable. Days must be 1–3650.
