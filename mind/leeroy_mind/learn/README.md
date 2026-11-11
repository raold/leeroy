# Numeric line rewards

`[learn] enabled = true` enables recipe writes and the model-free `rewards` night
stage. Missing, malformed, unknown or non-boolean settings mean off. Normal
application migration creates the empty tables; disabled learning writes no
recipes, rewards, weights or reward plans and changes no prompt or send decision.
The switch is read at startup (restart required), including the nightly settings.
Signals are separately opt-in. Rewards screen eligible game lines themselves through the same historical screening
function as signals, including CLI nights and when signal capture is off. Failed
screening stays pending for retry. Discord and voice rooms use measured numeric
evidence without borrowing game quality or history. Empty voice transcripts are
scored too, including muted receipts; unsupported channels and overlong text also
receive only their measured components. Unobserved evidence is never inferred dislike.

`reward(conn, line_id, *, now=None)` is read-only and returns a `Reward` with
numeric parts, the weight version, and a separate `cap_weight`. Supply receipt seconds for deterministic
replay; otherwise it uses the current time. Missing learning tables return
`available=0`. No text is returned, persisted in rewards, or logged.

## Formula and coverage

Defaults are 0.40 explicit + 0.25 engagement + 0.15 emote + 0.20 quality, minus
1.0 muted, 0.5 dial-down, 0.5 negative nudge and 0.1 ignored. Divide the weighted
component sum by the sum of weights for **measured components only**, then subtract
penalties and clip to [-1,1]. Explicit is measured only when a vote exists;
engagement only when a metric exists; emote only when an emote observation exists;
quality only after successful screening, a detector observation or a live verdict.
With no measured positive-weight component the component sum is zero. A lone
thumbs-up is 1.0 on Discord, as is a thumbs-up with clean game quality and no other
measures. Numeric parts record the measured flags and denominator. Weights are
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
Quality has a base of one after the reward stage completes the shared bounded
screening on a supported game row, or an actual live guard verdict is recorded.
The standalone read-only calculator can also recognise a completed signals receipt.
Discord, voice rooms and overlong rows do not earn a game-quality base. Missing
coverage contributes no quality weight; observed detector hits remain measured. Subtract the
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

The night selects at most 500 cast lines whose 120-second observation window has
closed: changed evidence first, then newest unscored lines with a 14-day lookback. A saved selection survives resumes. Later
signals or a changed owner vote revisit an existing score even outside that
initial lookback, bounded to the 120-day signal retention; each update and its
revision receipt commit together. Counts logged without content are scored this
pass, pending with an open observation window, and left_over eligible work
(including failed screening). Saved selections survive resumes. Selection, changed-
evidence scans, quality review and reward calculations run on owned read-only
SQLite snapshots off the event loop. Budgeting caches one prepared count. Failed
screening leaves its unit incomplete; the signals stage's separate 200-line cap
cannot prevent rewards from being screened and scored.

The stage uses no generation model. When claims or semantic guards are enabled,
it uses the existing background embedder, as the shared signals screening does. Normal startup and `nightly run` attach it when enabled,
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
