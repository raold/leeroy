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

Remaining work includes runtime acceptance of the task 4 adapters, task 5 dummy controller and task 6
benchmark, plus task 7 integrated lifecycle/raid acceptance. Python ingestion ships separately in PR48;
analysis, scoring, search and application work remains. The module loader must stay
unwired until the accepted slice has complete restoration and shutdown behavior. Configuration defaults are
off when the tap reads them; this foundation does not add active configuration settings.

Job integration must capture the nonzero token returned by `MarkJobRun(leader)` after entering the intended
instance and pass that same token to `ClearTrainingRun(leader, token)` at teardown. Never resolve the current
token for an old job. Registration records ownership without changing the game and is independent of the
training switch. These job receipts survive off-state override cleanup, allowing training to be enabled
during an already registered raid. Job teardown retains its receipt if restoration fails.
`RestoreJournaled` reports success separately from its optional verified-change count;
a refusal is not completed restoration. Failed cleanup blocks new training mutations and is retried.
The cleanup pass logs which adapters keep it pending (`training cleanup pending: dummy_trial(<id> unreturned=..),
journals(N)`, on change and at most once a minute) and `training cleanup verified` when they clear. While it is
pending, `TrainingCleanupPending()` fences every body (Realm `busy:training_cleanup`, play style, queue fence) for
at most ten minutes (`LeeroyTrainingFence.h`); then `training cleanup fence released` is logged once and only the
per-body fences hold: job owners, journals, DC runs and pending bot commands. New training stays refused
(`cleanup_pending`) until a pass proves every adapter clean.
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
Creating that party sends the bot an empty group list, and playerbots' stock "reset botAI" answers it by dropping
any master outside the bot's group (the driver always is) and resetting its strategies. That used to be read as an
ownership change and blocked every accepted trial (`cleanup_failed:ownership_changed` on 2026-11-10).
While the solo party is still ours and every other ownership fact holds, the controller now puts the driver back and
strips follow again (`master_reasserted guid=`, first and every 50th); during the fight the member is dropped
`ai_reset` instead, since the reset took its trial strategies. A foreign master is still an ownership change.
Reset completes while the exact driver/session/group still prove ownership, then cleanup removes only that solo
member, restores the prior leader flag, rebuilds stock defaults for the restored ungrouped state and verifies return.
AI target/engine state is cleared too, including on same-map returns; pending logout is never cancelled for cleanup.
Added members, invitations, group transfers, or a
participant becoming the configured human block further mutation. A partially created group retains its identity
for cleanup rather than destroying an object that may already be linked to the player. As with other in-memory
job receipts, abnormal process termination requires reconciliation of any remaining harness parties.
A blocked member logs why (`dummy trial <id> blocked member <guid>: <reason>`). A trial stopped because training
cleanup is pending ends `cleanup_pending`, not `training_off`. Ten minutes after its `cleanup_failed` terminal a
trial that no longer holds any member (no group object or solo party of ours lists it, and its same body does
not have the driver as master) ends: `cleanup_released after_ms= unfinished=` and a `dummy_cleanup` event
`{id, state: "released", unfinished, after_ms}`. A recovery that proves every return sends `state: "restored"`.
A member still held keeps the job (`cleanup_held` once) and, from then on, only the held members stay fenced.
Journals are never discarded by either path.
Admission excludes existing non-permanent instance bindings because native group removal would discard them.
Bindings are checked again before cleanup; a later non-permanent binding blocks disband instead of being erased.
Permanent raid bindings are preserved by that native path. The controller never snapshots or recreates saved locks.

Dummy `ms` records elapsed trial time from an accepted normal attack until stopping/dropping that participant;
it does not infer uninterrupted casting uptime. The incoming-damage alarm conservatively observes positive
pre-AI requests from a source other than the dummy, so an absorbed/adjusted request may abort a trial. Damage
and spell results retain the measurement bases below, and aborted/partial participants carry a drop reason.

## Benchmark candidate and shared pins

Task 6 adds a thread-safe owner pin set and the single upstream pin composer. Empty contributions remove only
that owner; overlapping owners retain their own pins and flags. Benchmark admission requires an unused
`benchmark` owner, and verifies every requested GUID with `RandomPlayerbotMgr::IsPinned` after composing.
The upstream accepted count describes the whole union, so subtracting two counts cannot verify this job.

`get_rivals` returns available level-qualified random bots, sorted by item level then GUID. Live admission
also excludes queued LFG/BG players, test-driver reservations, existing masters, cast/human characters and
dummy reservations. Every benchmark member must still qualify when grouping, with the leader's faction.
Group join/leave can discard non-permanent instance bindings in the installed core. Admission therefore rejects
pre-existing temporary saves and any target-map save. Cleanup permits only the exact newly created trial bind,
plus unrelated permanent saves that core preserves; an externally added temporary save retains recovery instead
of triggering a blanket reset. Benchmark mode never invokes offline mode's target-map unbind loop.
The shared raid reservation covers all requested members from admission through completed teardown, even
before they have a group. The included task 5 controller supplies the reciprocal `DummyTrialOwns` implementation.

`StartRaidJob` takes the request ID explicitly because `OfflineRaidCmd` does not carry the bridge envelope.
Ordinary offline raids keep their existing roster/loot policy. Both modes reset the training strategy journal
after an intentional AI strategy reset, capture a fresh training-run token after verified instance entry,
and clear that exact token on teardown. The benchmark-only leader exception proves the active job's exact
driver, group, roster, map and instance; its scope is owned by the configured guild, not the rivals' guilds.
Per-key restoration still works after return/group removal without operating on a newer run.

Benchmarks use master loot at threshold zero, remove the ordinary noncombat loot strategy, and issue no
loot-award, equipment or gear-up commands. These settings alone do not prevent automatic FFA currency,
quest rewards or loot-money packets. A separate bounded loot guard publishes a pending ownership snapshot
before the first teleport. A matching reward on the target map before verified instance binding contaminates
the trial without suppressing that reward. Completed leader worldport binds the instance once and preserves
any contamination. Hooks then suppress newly generated creature/GO item rewards before automatic currency
storage, and matching reward money before sharing, only for the captured bodies, group and instance with
an exact source/Loot match. Callback classification and mutation share a mutex; no map thread reads the job.
Personal inventory containers and unrelated actors are untouched. An owned actor encountering an unproven
public/foreign creature or GO reward leaves that reward intact, marks the trial contaminated, and fails the
benchmark at the next safe world tick. There is no inventory rollback. Baseline analysis must require clean,
successful benchmark completion and inspect the reason: `done` with `human_online`, a timeout or another
cancellation is not a completed clean baseline. Normal combat consumption of ammunition, reagents and consumables remains
allowed: this is an owned-loot-reward guard, not a claim that bags never change. Admission fails closed unless
the guard hooks are registered. Only allowed boss IDs are registered for benchmark telemetry.
An unapproved `IN_PROGRESS` boss stops the job and initiates owned DC disable/combat stop/return in the
same world tick that observes it. This is a post-map barrier: it cannot prevent damage during the map update
that first entered combat. Returns finish asynchronously; they are not falsely reported as completed teleports.

Human login, identity changes, training disable, lost pins, timeout and normal completion all enter owned
teardown. Restoration failures and unverified rival returns retain the job/pins and block another admission.
The 60-second cleanup grace publishes one failed terminal; it does not authorize abandoning a rival or deleting
its journal. A later successful recovery releases the reservation without publishing another terminal. Return
attempts remain tick-driven. Captured Player/AI/session identities and irreversible logout revocation prevent
a reconnected body from inheriting old cleanup, even if pointer addresses are reused. Journals restore before
disbanding their owned group; a pending restore retains the group/controller proof while safe returns proceed.
Generic DC cleanup and watchdogs cannot bypass that captured-body proof. Final strategy reset runs after group
and master release, and guarded `Reset(false)` clears old raid targets and the combat engine without cancelling
logout. Final reset has its own completion receipt: a reset exception is caught, stays incomplete and retries
without renewing the cleanup deadline. The owned DC token/journal and the job's WaitAtBoss value restore before
the leader is released, so normal wandering cannot strand or redirect old recovery. Human login is latched even
if the human logs out again before the next world tick. Loot callbacks also check the atomic current human GUID:
if any captured member becomes the configured human, shared rewards are left intact and the trial is contaminated.
An externally replaced benchmark pin contribution is never erased.

The speaker filter excludes muted rivals until their owner releases them, including retained cleanup after
module disable. Ordinary cast exclusion still follows the module-enabled flag. **Deployment prerequisite:** mod-ollama-chat must recheck that
filter at delivery (`OLLAMA_SPEAKER_FILTER_AT_DELIVERY=1` in the reviewed dependency patch); an admission-only
filter cannot suppress a generation queued before benchmark admission. This work does not modify that dependency,
register the training loader, start a server, or claim live raid/loot/restore acceptance.

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
