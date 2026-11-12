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

The planning budget uses completed reward units' own wall time, separately for
lexical and embedding screening. It uses the median nightly seconds per unit
over the last seven nights, beginning after the first measured night. Until then,
lexical work reserves 0.02 seconds and embedding work reserves a conservative
2 seconds per generation. This is not a generation-model call. A failed pre-count logs only
`rewards_unavailable`, counts zero and leaves the rest of the night's plan intact.

## Caps, time and channel scopes

The stored value is raw, clipped evidence. A separate `cap_weight` is 0.9^n,
where n is this member's line ordinal on the mind's local calendar day. Game and
Discord ranks are independent. The ordinal includes unobserved lines to prevent
volume from evading the cap. The report applies the weight only to positive
credit: infinitely many perfect lines cannot exceed 10 per member/medium/day.
Penalties and vetoes remain undiluted. Reports additionally apply a 21-day
half-life at read time.

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

### Nightly talk coach

`[learn] enabled = true` and `coach = true` opt into `talk_coach`, one reflection
JSON call per member with at least ten rewarded lines in the most recent day
of the night's window, at most twenty members. Both switches default off;
invalid settings disable learning. Planning reserves one JSON call per possible
member before rewards run, so a first scoring night also budgets the coach.

Each call sees the best and worst three exchanges in that member's most-observed
room, with the owner's preceding line and bounded recent context. It never mixes
rooms or game and Discord text. Whispers and DMs contain only that member/owner
pair and can produce only whisper lessons; group/voice evidence only group
lessons. Officer, private/unknown voice rooms and unsupported channels are excluded. An exchange without an
identifiable recent owner line is omitted. Choosing one room per night is a
deliberate privacy boundary; other rooms may be coached on later nights.

The strict schema accepts up to two third-person style notes of at most 25 words,
a no-manipulation attestation and a minimum forward dial. Real-name, explicit,
manipulation, person, scope and dial screens apply before storage. These are
bounded rule-based screens, not a proof that arbitrary prose is safe. The later
live-talk integration must recheck scope and current dials before using lessons.

Lessons are proposed by default. `auto_lessons = true` allows up to five active
lessons per member; overflow stays proposed. Both expire after fourteen days.
The model receives numeric per-lesson counts and mean rewards with versus without
the lesson in the same room, and can reinforce displayed IDs (extend expiry) or
drop them (expire immediately). These are observational comparisons, not causal
estimates. No text from earlier lessons is included in that feedback payload.

When the optional bandit registry is installed, the schema also accepts one closed
numeric/enum arm proposal. The registry validates its parameters, dial bounding
must leave it unchanged, and `arms.propose` stores it inactive. Without that
registry, the schema requires a null arm; coaching still works.

`python -m leeroy_mind learn lessons --data-dir <data> [--guid N]` opens the
database read-only, without migration, and prints unexpired active/proposed notes
to the terminal. Text never enters logs. The stage stores notes but does not yet
change live prompts; live integration and the Lab controls follow separately.

The central outgoing-line commit writes one recipe per generation, anchored to
its first delivered line, in the same transaction as `chat_line`. Later fragments
from that generation link to the anchor through `talk_recipe_line`. This includes
game, voice and Discord delivery paths. The recipe uses a nested savepoint: a failed recipe rolls back only analytics,
logs a fixed code and keeps the acknowledged chat row and world bookkeeping. Existing disabled behavior
uses the original insertion path.

The night waits until the last fragment's observation window closes and writes
one `talk_reward` row at the recipe's anchor. Numeric evidence from all fragments
is pooled before the existing formula: repeated signal kinds count once (maximum
value; minimum latency and minimum explicit vote), a reply cancels a fragment's
ignored observation, and any thumbs-down wins over another fragment's thumbs-up.
Screening hits are unioned and penalties are not multiplied by fragment count.
Silent fragments do not dilute an actual response. Positive saturation uses the
first line's existing daily ordinal once. A later vote on any fragment replaces
that generation's score and revision, so recipe consumers receive one observation.
Reports' scored-line counts now count generation anchors for new bursts. Existing
recipes remain independent: the migration does not guess old generation boundaries.

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

## Variant arms and the bandit

`[learn] enabled = true` plus `bandit = true` opts into the model-free `bandit`
night stage after rewards. Default false. `freeze = true`, `frozen_members = [101]`
or `frozen_families = ["length"]` provides an owner veto. Invalid learn settings
disable the entire table. Restart after configuration changes.

The registry stores versioned, closed numeric/enum parameters for length,
questions, humour, callback, tease, typing and followup. Control means the existing
behaviour, not an approximation to a numeric preset. Coach proposals are excluded
until separately activated. Selection applies `arms.bounded` before recording,
using effective humour/forward and the lesser of member/world romance dials,
gender and callback/fingerprint switches supplied in `Context`. A capped choice
records control, and disabled callback/typing families also record control.
Renderers still recheck current caps and use fixed wording; no instruction text
is accepted in parameters. This task adds the
selection/credit API; task 39 wires selection and rejection receipts to live talk.
Enabling the bandit alone does not alter prompts or choose arms for live lines.
Its first enabled night registers the built-ins for inspection. Frozen members
are excluded before the night's batch limit, so they cannot crowd out other credit.

`choose(conn, guid, Context(..., settings=cfg))` returns family-to-id choices.
The caller owns its transaction, and captures the choice IDs and `context.key`
in the generation's recipe Snapshot. Disabled/frozen choices return stable control
IDs without writes or randomness, even on an old schema. Enabled choices reserve
an attempted turn, so cancellation cannot inflate the exploration allowance.
No more than floor(turns/5) choices per member explore an unqualified arm. Other
turns sample control and arms with at least 30 rewarded uses and a posterior mean
above control. An arm below 30 uses also cannot exceed half of its context's turns.
Context is channel kind (individual Discord rooms), stance and talkative bucket;
all members in that key share posteriors, shares and the 30-use threshold.
Game whispers, DMs, officers, guild and group channels keep separate statistics.
Uses count credited lines, not split Beta mass; 30 seven-family lines carry only
30/7 observations of mass before volume weighting and decay.

`update(conn, line_id, settings=cfg)` credits only recorded arms, splitting one
observation equally among families. It maps the merged reward as `(1+r)/2` into
Beta success and failure mass. Silence 0 is therefore neutral 0.5, not approval;
thumbs-up +1 is success, downvote/mute -1 failure, dial-down -0.5 maps to 0.25,
ignored -0.1 to 0.45, and median reply +0.5 to 0.75. For positive rewards,
cap_weight scales the observation weight, never the value; negatives keep full
strength. Frozen families receive no credit, rejection or retirement; other
families retain their original split weight and continue learning. Revisions replace the old
credit atomically, so a later veto never leaves stale success behind. All evidence
decays from the original line timestamp with a 21-day half-life; Beta(1,1) priors
do not decay. A line with no recipe arms is not attributed by guesswork.

Guard rewrites/reactions/drops count as trips. Failed-open and not-run reviews
(including unavailable/skipped reviews) count as neither clean nor tripped screens.
The future
send adapter can call `reject` for unsent guard/content refusals with an idempotent
attempt ID; these do not count as rewarded uses. Retirement happens only at night,
pooling screened uses across contexts: at least 20 arm and 20 family-control uses,
an excess trip rate of at least 10 percentage points, and the arm's 90% Wilson
lower bound above the observed control rate. The scan runs off the event loop.
`bandit_guard_retired` logs the arm id, both counts and rates; `bandit_credited`
logs the credited-line count. There is no un-retire command.

`python -m leeroy_mind learn arms --data-dir <data>` is read-only: registry,
decayed Beta posteriors, rewarded uses, failures and observed choice shares. Empty
and older schemas report without seeding arms or migrating. No content is printed.

`python -m leeroy_mind learn report --data-dir <data> --days 14` opens SQLite
read-only, without migration, logging setup or any service. It reports counts,
mean rewards and discounted totals by member ID, channel kind and each recipe
field. Older schemas report unavailable. Days must be 1–3650.

Coach review safeguards: eligible members rotate by last coaching time, then evidence count. Shared romance and content screens gate notes using effective forward, member romance and world romance dials; a false non-manipulation attestation discards only its note. The system prompt uses prompts/nightly_talk_coach.txt with immutable night invariants. Named evidence drops oldest exchanges to fit the context and answer margin. Duplicate arms of any status are ignored, with at most five new proposals per night. Dropped notes cannot be resurrected in the same reply.
