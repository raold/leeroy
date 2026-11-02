# Training-ground foundation

This is an **unwired foundation**, not an enabled training system. The main script loader does not register
these scripts, and training mutations require the configured training gate and completed cleanup. No live configuration
change or raid acceptance is implied by the unit tests or an object compile.

Implemented pieces:

- Shared C++/Python protocol examples and strict C++ command validation.
- Fixed-capacity pull recorder, bounded live-cast ring, sparse one-second tracks, and complete-message counts.
- A candidate telemetry tap with four recording slots and four prepared tables. Generation-stamped publication
  separates map-thread recording from world-thread sampling and encoder-thread reset. Terminal boss intents
  are checked against the committed instance state after map updates; a rejected `DONE` is not a kill.
  An ambiguous same-boss re-engage before that barrier is discarded rather than merging two pulls.
- Strategy/value journals with typed runtime setters, verified restoration from journal copies, and retryable
  cleanup. Wildcard undo affects journaled names only; an explicit value restore can use its documented stock
  fallback. Queued mutations recheck the gate and reject an obsolete off/on control epoch.
- Immutable custom-strategy cache priming with trigger/action/known-spell checks before any strategy is installed.
  Vocabulary discovery has a 64-job queue, at most 64 spell lookups per tick, and rechecks the human-online gate
  before each scan slice. Oversize results produce a failed `get_vocab` job, never a partial success.
- Per-key dungeon-setting restoration that preserves unrelated overrides and detectable later replacements.
  Cleanup tracks the leader's map, instance, group and generation token. The boss-hold watchdog ignores door
  pauses and pre-existing holds, verifies the actual resume, and retains a refused resume for retry.
- A bounded dummy-trial controller using the normal playerbots attack path. It admits idle masterless cast bots,
  establishes a separate owned solo party and harness driver for each candidate before moving,
  watches incoming damage requests and distance, and verifies
  the original location after asynchronous return. An admitted request reserves its participants through cleanup.

Remaining work includes actual runtime acceptance of the task 4 adapters and task 5 dummy controller,
task 6 benchmark/pin integration, and task 7 integrated build/lifecycle/raid acceptance. The Python ingestion,
analysis, scoring, search and application work in tasks 9 onward also remains. The module loader must stay
unwired until the accepted slice has complete restoration and shutdown behavior. Configuration defaults are
off when the tap reads them; this foundation does not add active configuration settings.

Job integration must capture the nonzero token returned by `MarkJobRun(leader)` after entering the intended
instance and pass that same token to `ClearTrainingRun(leader, token)` at teardown. Never resolve the current
token for an old job. Registration records ownership without changing the game and is independent of the
training switch. These job receipts survive off-state override cleanup, allowing training to be enabled
during an already registered raid. Job teardown retains its receipt if restoration fails.
`RestoreJournaled` reports success separately from its optional verified-change count;
a refusal is not completed restoration. Failed cleanup blocks new training mutations and is retried.
The dungeon-clear API has no ownership generation for overrides: removing and recreating an identical value
is indistinguishable from the original (ABA). In-memory journals also cannot promise restoration after a crash;
explicit stock-value restores remain available after restart. These limits require lifecycle acceptance before wiring.

Dummy setup has a 20-second deadline; fighting lasts the requested interval, and return has a 30-second deadline.
A failed return emits one failed terminal job and retains its receipt/reservations for bounded recovery attempts.
Cleanup never resets or teleports a participant acquired by another master, group, session, or reserved job.
Transferred or logged-out sessions require operator reconciliation; the in-memory receipt cannot survive process exit.
Shutdown makes one bounded cleanup attempt and reports unresolved return failure instead of claiming completion.
Strategies return through the harness's stock reset; later standing-setup application remains the mind's responsibility.
The installed playerbots AI clears a random bot's driver whenever it is ungrouped, so trials temporarily create
one solo party per participant. Admission still requires an ungrouped bot; candidates never share these parties.
Reset completes while the exact driver/session/group still prove ownership, then cleanup removes only that solo
member, restores the prior leader flag, rebuilds stock defaults for the restored ungrouped state and verifies return.
AI target/engine state is cleared too, including on same-map returns; pending logout is never cancelled for cleanup.
Added members, invitations, group transfers, or a
participant becoming the configured human block further mutation. A partially created group retains its identity
for cleanup rather than destroying an object that may already be linked to the player. As with other in-memory
job receipts, abnormal process termination requires reconciliation of any remaining harness parties.
Admission excludes existing non-permanent instance bindings because native group removal would discard them.
Bindings are checked again before cleanup; a later non-permanent binding blocks disband instead of being erased.
Permanent raid bindings are preserved by that native path. The controller never snapshots or recreates saved locks.

Dummy `ms` records elapsed trial time from an accepted normal attack until stopping/dropping that participant;
it does not infer uninterrupted casting uptime. The incoming-damage alarm conservatively observes positive
pre-AI requests from a source other than the dummy, so an absorbed/adjusted request may abort a trial. Damage
and spell results retain the measurement bases below, and aborted/partial participants carry a drop reason.

## Measurement contract

Every new pull identifies the basis of its measurements. Older input without these fields is `unknown`.

| Field | Basis | Permitted interpretation |
|---|---|---|
| `damage`, `damage_boss`, `taken`, damage totals | `damage_basis=requested_pre_ai` | Damage requests observed before AI/final core adjustments, not verified health removed. Do not use for actual DPS or damage-based raid scoring. |
| `healing` and historical `overheal` | `healing_basis=effective_gain_with_pre_absorb_raw` | Healing is effective health gain. The raw-minus-gain field includes absorbed healing; it is not exact wasted overhealing and must not drive efficiency scores. |
| Damage/heal spell attribution | `spell_basis=best_effort_thread_pair` | Single-use, thread-local pair tags. The hooks have no shared event ID; corroboration is required for exact causal attribution. |
| `interrupts` | `interrupt_basis=successful_interrupt_spell_cast` | Successful casts of interrupt-effect spells, not confirmed landed interruptions. |

Dummy results carry damage and spell bases too: requested throughput is useful for controlled comparisons,
but must retain that label. Metric restrictions are independent; they do not discard useful death, cast,
effective-healing or position evidence from an otherwise complete pull. A verified post-application damage
source and landed-interruption evidence are separate follow-up work.

`active_ms` counts observed alive sample intervals only. First valid sample per second wins; missing seconds
contribute zero and each interval is clipped to the closed pull duration. It is not inferred combat uptime.
Sparse samples produce separate contiguous tracks, each with at most 600 points. A dense maximum pull has
82 track lines; extremely sparse input can have many more, so consumers must validate final member/death/track
counts. The existing bounded bridge queue can lose records; a header alone does not prove complete delivery.

Counters saturate at `UINT64_MAX`. Unknown/overflow damage folds into spell zero instead of disappearing.
Repeated equal hits remain separate events because the hook provides no event ID. Death evidence contains
only the retained recent hits after the prior accepted death and no later than the current death.

## Validation

The standalone `modules/mod-leeroy/tests` CMake target discovers these pure tests automatically. Run
`leeroy_tests Training_` and then unfiltered `leeroy_tests`. Core-bound translation units additionally need
the actual installed core/playerbots interfaces, with dungeon-clear enabled and disabled. These checks do
not substitute for the remaining live lifecycle and restore acceptance tests.
