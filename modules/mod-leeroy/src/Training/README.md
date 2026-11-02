# Training-ground foundation

This is an **unwired foundation**, not an enabled training system. The main script loader does not register
these scripts, and the existing bot-command handler rejects training-only mutations. No live configuration
change or raid acceptance is implied by the unit tests or an object compile.

Implemented pieces:

- Shared C++/Python protocol examples and strict C++ command validation.
- Fixed-capacity pull recorder, bounded live-cast ring, sparse one-second tracks, and complete-message counts.
- A candidate telemetry tap with four recording slots and four prepared tables. Generation-stamped publication
  separates map-thread recording from world-thread sampling and encoder-thread reset. Terminal boss intents
  are checked against the committed instance state after map updates; a rejected `DONE` is not a kill.
  An ambiguous same-boss re-engage before that barrier is discarded rather than merging two pulls.
- Pure strategy/value and per-setting override journals. These are the foundation for task 4, not completed
  runtime levers. Runtime undo must apply and verify a journal copy before consuming the original receipt.

Remaining work includes the task 4 runtime journal integration and verified cleanup, task 5 dummy jobs,
task 6 benchmark/pin integration, and task 7 integrated build/lifecycle/raid acceptance. The Python ingestion,
analysis, scoring, search and application work in tasks 9 onward also remains. The module loader must stay
unwired until the accepted slice has complete restoration and shutdown behavior. Configuration defaults are
off when the tap reads them; this foundation does not add active configuration settings.

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
