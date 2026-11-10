# Standalone draft guards

`review(GuardStores, Draft) -> Verdict` performs bounded reads and comparisons.
It never sends, writes, migrates, logs text or invokes a model. The opt-in talk
adapter in `talk/awareness.py` calls it before delivery. Disabled configuration
returns `send` before touching any input or store.

The rules reuse the single-candidate helpers in `eval.talk`: token shingles and
optional cosine for own repetition, five-word phrases used by two other members
in the current evening, stock openings/closings, filler and parent/thread
relevance, existing `claims.check`, and narrow positive world-evidence checks.
The scratch-only batch detectors are never called. Existing `talk_move` topic
memory adds a repetition reason; a new fact about an old topic remains allowed.

## Configuration

The core config accepts this extension. Call `load_guards_config(path)` separately
when constructing `GuardStores`. No live configuration is changed by this package.
Missing, malformed or unknown settings disable the whole table.

```toml
[guards]
enabled = false
semantic_enabled = false
repeat_overlap = 0.70
repeat_cosine = 0.94
parent_cosine = 0.20
thread_cosine = 0.25
repeat_seconds = 259200
gag_daily_cap = 2
stock_daily_cap = 3

[guards.gag_caps]
"7" = 1
```

These are provisional task-12 thresholds. Its embedding measurements used fake
word-hash vectors, so semantic enforcement requires a separate explicit opt-in
after calibration with the caller's embedding model. Lexical overlap and cosine
trigger at `>=`; relevance requires both scores strictly below their thresholds.
Fewer than four words bypass near-duplicate and semantic relevance checks.

## Inputs and visibility

Use `guild`, `officer`, `whisper`, `game`, `discord` or `dm` for `Draft.channel`.
For `game`, `room` is the original say/yell/emote/party/raid/raid_warning channel.
For Discord and DM it is an opaque ASCII room identifier, not a display name.
Whispers and DMs include `to_guid`. Parent and thread IDs are explicit evidence;
the guard does not infer a parent from general guild adjacency.

Stored game chat is read from `conn`. Discord history is caller-supplied through
`HistoryLine` and must include the captured reader IDs. All history IDs share one
namespace with parent/thread references; do not supply a second copy of a stored
row. DM pair, room and audience fences and stored officer role apply before any
comparison. Private history cannot veto a public-channel draft. Own public lines
can be compared across game channels. Discord phrase reuse stays in the visible
room. Guild evening and daily ration dates use America/New_York, including DST.

`vectors` maps stored chat IDs to cached vectors; external lines carry their own.
Every vector must be a valid 512-float packed vector in the draft's embedding
space. Embeddings are never generated here. Optional `facts` is an already-loaded
`eval.talk.unawareness.load_facts` snapshot; load and refresh outside the fast path.
That checker recognizes only a few assertions contradicted by positive prior
evidence and has very low coverage; it does not replace an awareness card.

Set `claims_enabled` only when claims are enabled. Existing claim checks run only
with a draft embedding and available tables. Chat-source conflicts additionally
require visible provenance from this member; owner-source claims are allowed.

Reads are bounded to 128 own lines, 256 guild lines from the last 12 hours,
16 recent context lines (including parent), 64 topic moves and 100 adopted active
lexicon gags. External history is limited to its last 400 entries. Old schemas
omit unavailable sources and expose a numeric `missing_sources` score; they do
not gain tables. Coverage is therefore bounded, not exhaustive. Pass `line_id`
only for historical replay to exclude the stored candidate and later row IDs.
Own-repeat, five-minute parent/thread and 12-hour evening-history windows are
independent; shortening `repeat_seconds` does not disable the other checks.

## Engine contract

`Verdict` carries action, fixed codes, numeric scores and IDs, never line text.

| Action | Caller responsibility |
|---|---|
| `send` | Deliver the draft, then record the confirmed delivery. |
| `rewrite` | Request one new draft with the codes, then review it again; abandon an unsuccessful retry. |
| `react` | Use the suggested laughter reaction instead of sending the filler text. |
| `drop` | Send nothing; the line is empty or repeats an existing contribution. |

A rewrite takes priority over drop, which takes priority over react. The optional
`await judge_relevance(reasoner, stores, draft)` is a separate model call that may
clear only the relevance finding. Errors remain rewrites; changed context is
rejected and independent guards are rerun after the await. The caller owns the
reasoner's privacy/logging policy, model budget and cancellation.

`record_sent(stores, delivered_draft, receipt_id=...)` is the explicit write API.
`delivered_draft.now` is required and must be the fixed confirmed delivery time,
preserved across retries, including retries after midnight. Receipt IDs are stable
opaque ASCII delivery IDs. The call joins the caller's transaction and never
commits. Retrying the same ID with the same matched keys is a no-op; reusing it
with different keys on that delivery day is an error. Serialize review, delivery
and recording per member: review is not a reservation and cannot enforce caps
against concurrent sends by itself. Persist delivery identity/time through crash
recovery; recording cannot prove a remote send succeeded.

Receipts live in existing `meta` keys `guards:sent:<local-day>:<guid>`; no migration
is needed. Values contain hashed receipt IDs, hashed stock phrases and gag IDs,
not text. Rejected candidates and reactions spend no text allowance. Caps count
across channels per member/day; lexicon joke/slang caps require active adoption.
Confirmed delivery prunes receipt keys older than the preceding fourteen local
calendar days, in the caller's transaction. Retries outside that retention window
are not deduplicated. Other metadata is untouched. Missing receipt tables omit the
check; malformed receipts return a fixed rewrite code rather than resetting caps.

## Live adapter

`[guards] enabled = true` admits review independently of `[talk] aware = true`.
Both remain off by default. The card uses 600 estimated tokens for guild/officer/
Discord, 450 for whispers/DMs, and 400 for other game channels, plus a fixed
delimiter. Voice uses its existing typed fallback scope. Game and Discord chat
evidence never cross media. DMs and whispers retain the exact pair; owner claims
need visible cited provenance too. Uncited owner corrections are omitted.

Every styled draft is checked before typing. One rewrite per turn goes through
the existing real-name, content and tell screens, then review again. A signed
exact-text template cannot be rewritten and is dropped instead. Reaction verdicts
send a game nod/laugh or react to the Discord parent. A whisper has no private
game-emote transport, so it stays silent. Reactions spend the turn's pacing budget
without recording a spoken line, memory use, game session or Discord game activity.
Typed lines are checked again after typing; a newly failed check drops the line.
Per-member locks cover review through acknowledged delivery and receipt recording.

Any review/embedding exception logs only `guard_review_failed_open` and the guid,
then sends through the ordinary delivery gates. Cancellation propagates. Semantic
checks still require `semantic_enabled`; enabled claims use the existing embedder
and contradiction checker. At most seventeen texts are embedded per check, with
a five-second timeout, no open read transaction, and a 512-vector memory cache.
World evidence refreshes at most once a minute. These are bounds, not a latency SLA.

Run `python -m pytest -q -s tests/guards/test_rehearsal.py` for the four-turn
FakeLLM/FakeBridge comparison. Off: four text lines, one contradiction, one filler
after a question, one location mismatch. On: three text lines, one reaction, zero
of those flags. Thus each of those detector rates is 25% to 0% per attempted turn
(25% to 0% per sent line); repetitions are zero in both. The script selects its
rewrite answers and uses fake embeddings; this measures integration, not live
quality, semantic calibration or a false-positive rate.

## Measurement limits

`calibration.measure(stores, [Case(draft, acceptable=...)])` returns only aggregate
counts and latency. `acceptable=None` is unlabeled and yields no false-positive
estimate. Human-labeled acceptable cases supply the false-positive denominator;
synthetic fixture accuracy does not estimate live-history accuracy. Replaying
observed history measures flag rate, not counterfactual behavior after rewrites.
Historical ration evaluation needs contemporaneous delivery receipts; historical
claim evaluation needs contemporaneous claims. Current mutable records cannot
reconstruct those states. The caller must report those omissions and missing
parent/vector coverage alongside rates.

The 2026-11-08 private read-only-copy replay covered 1,529 supported text lines
from 92 members in 14 days; 618 voice lines were outside this API. Overlap
thresholds 0.70, 0.80 and 0.90 each flagged one line (0.065402 per 100).
The default 0.70 path took 1.3514 ms median / 3.7511 ms p95. Enabling fake
word-hash semantics flagged 109 lines (7.128842 per 100), including 108 relevance
findings, at 2.2403 / 8.8337 ms. Of 277 reciprocal directed parent candidates,
263 had usable, visible relevance context. There were no stored parent links,
human labels or delivery receipts. Claims were disabled for this replay, and
current roles/adoptions were not historical reconstructions. World evidence had
no verified kills and generated no findings. These rates cannot identify a
false-positive rate or justify enabling semantic thresholds in production.
