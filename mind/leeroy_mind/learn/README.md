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

Defaults are 0.40 explicit response, 0.25 engagement, 0.15 emote and 0.20
quality. Quality is a **deduction only**, never approval. A clean screen adds
zero; absent screening adds and deducts nothing.

Compute the weighted mean over measured owner-response components only
(explicit, engagement and emote). Explicit is measured when a vote, mute,
dial-down or negative nudge exists. Only the vote supplies its signed value;
controls with no vote have vote value zero. Engagement is measured when a reply,
speed, length or continuation metric exists; emote is measured when an emote
observation exists. If no measured component has positive weight, this mean is
zero. Then subtract the fixed owner penalties: 1.0 muted, 0.5 dial-down,
0.5 negative nudge and 0.1 ignored. This is the owner part.

The quality deduction is `w_quality * clamp(-(hits + guard_penalty), -1, 0)`.
The four detector hits and the maximum guard cost (rewrite 0.25, react 0.5,
drop 1) can only lower the result. A completed shared screen, a historical
signals receipt on a supported game row, a detector observation or a live
verdict establishes the available quality measurement. Discord, voice rooms
and overlong rows receive no invented game-quality measure. Retrospective
`judge_guard_*` observations do not claim that a live guard actually acted.

Add the owner part and quality deduction, keep the existing owner-veto floor
(a thumbs-down cannot score above -0.40, even with changed weights), and clip
to [-1,1]. Fixed penalties and the veto remain outside editable weights. Numeric
parts record measured flags, the owner-only weight denominator, the owner part
and quality deduction. Weight versions remain append-only; scoring uses the
latest valid version as of its timestamp. Disabled/read-only calculations use
defaults without writing. There is no weights editor yet.

With default weights, equal evidence gives these values in game, Discord and
voice. Quality-hit examples require an actual measured hit:

| Line | Value |
|---|---:|
| Silent, no owner evidence | 0 |
| Thumbs-up | +1.0 |
| Thumbs-down | -1.0 |
| Replied, median engagement | 0.5 |
| Ignored | -0.1 |
| Muted | -1.0 |
| Dial-down or nudged against | -0.5 |
| Repeat hit alone | -0.2 |
| Thumbs-up with a repeat hit | 0.8 |

A per-line weighted mean is not monotone in added evidence. A thumbs-up with a
median reply is about 0.81, below a thumbs-up alone at 1.0; both remain strongly
positive and above silence and negative responses. This is intentional.

Engagement averages only observed components: replied, speed, length and
continuation. Latency is stored as seconds/120, so speed is 1-latency; length is
codepoints/500. A rolling 14-day median uses prior observations in the same
channel kind and **UTC hour**, excluding later observations and the current
line, bounded to 2,048 prior lines. Divide an observed value by twice a positive
median, capped at one. With no positive baseline, an observed component is
neutral 0.5. Missing observations are not zero samples. This is a provisional
normalisation, not a calibrated satisfaction measure. Friendly/romantic emotes
add and rude emotes subtract, clipped to [-1,1].

The planning budget reserves 2 seconds per reward unit (at most 500) for shared
screening, numeric reads and an optional embedding batch of up to 17 texts.
This is a conservative allowance, not a measured embedding latency; lexical
screening measured about 0.012 seconds per unit. A failed pre-count logs only
`rewards_unavailable`, counts zero and leaves the rest of the night's plan intact.

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

Preference export, held-out evaluation, optional guard-evidence capture and the
manual training outline are documented in [FINETUNE.md](FINETUNE.md). All export
and scoring commands operate on a scratch backup without source writes or model
calls; capture is separately opt-in and does not train or alter talk decisions.
Snapshots live in the data directory's scratch folder and are always removed.
Guard pairs contain plain text on both sides; an owed fallback to the original
is not a rewrite. Discord parent IDs are resolved before vector attribution.
Offline ration checks use only mapped earlier delivery receipts; expired or
unmapped receipts have no coverage. Private capture expires after 90 days in
the reward night, and capture errors cannot roll back recipes.

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
