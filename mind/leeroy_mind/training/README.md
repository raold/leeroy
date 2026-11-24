# Pull ledger foundation

The ledger is not attached to the application. It accepts parsed training
events through `Ledger.on_event`; future wiring must call
`Ledger.on_connection` at connection changes and `Ledger.expire` periodically.
No command, model, game connection, or live database is opened by this module.

The `pull` header is the final marker. Counts must match distinct, valid member,
death, and track parts. Duplicates, overlaps, inconsistent owners, missing
parts, malformed arrays, and out-of-range samples prevent completeness. A
header seals the record even when incomplete; late parts cannot repair or
replace it. The database preserves the first committed pull ID across restart
and replay, including an incomplete receipt and already-pruned tracks.

Sparse tracks retain actual sample seconds in `at_s`, aligned with every
nonempty data array. Array position alone is not elapsed time. Missing seconds
are never fabricated as zero positions or continuous living intervals. A
cast-only boss track has no positional samples. Downstream checks must join
tracks by `at_s` and require consecutive observed seconds for duration rules.

Assembly allows eight pending pulls, forty members, eighty detailed deaths,
1,200 samples per entity, and 600 points per wire chunk. Sparse chunks may
number up to 24,600 per pull; the aggregate pending points and chunks are
capped at twice one maximum pull. A bounded tombstone set rejects recent late
parts. Orphans expire 600 seconds after their first local receipt, irrespective
of old wire timestamps. Connection changes discard partial assembly so two
socket epochs cannot manufacture a complete record. A fresh complete backlog
can still be ingested on the new connection.

The ledger retains up to eight sealed records for exact-header retry after
context or database failure. A process crash before persistence may lose that
buffer; a later complete retransmission can be ingested normally. After the
atomic write, callback failures are isolated. Callbacks are best effort after
commit: replay does not run them again, and a crash between commit and callback
delivery is not a durable work queue.

Run attribution uses the pull's start time, map, size, difficulty, and a known
instance binding. Historical raid rows provide backlog context. In-play
changes are timestamped and scoped to a raid; an old pull cannot take a newer
run's changes or unscoreable marker. The marker applies to the first successfully
committed matching pull; a failed pull's stale token cannot veto another retry
after a later pull consumed it, or delete a replacement marker. Receipt-time
persona roles and drunk/flask state are used only for a still-open matching run
with an encoder timestamp within 60 seconds of receipt, and labelled as such.
Historical roles use recorded tank/spec evidence; historical drunk/flask state
is `unknown`. The first-observation timestamp and exact run/context veto
consumption commit with the pull. An old backlog timestamp therefore cannot
bypass the observation period.

Completeness is separate from measurement provenance. Damage quantities are
requested before final core adjustments; spell attribution is best effort;
interrupt counters describe completed interrupt-spell casts. Those labels are
preserved unchanged. Independent deaths, casts, elapsed time, and observed
positions remain available; incomplete or aborted pulls and non-fight steps
never become scoreable. Future metric producers must apply the per-metric
restrictions documented in `store.py` and the C++ training README.

# The overhead gate measures his raids too

While the gate is in `ab` it measures his raids (raid nights and the add-command
groups the raid night adopts) as well as offline raids. Each kind keeps its own
per-family parity counter (`tg_gate:family:<map>:<difficulty bits>` for offline
raids, `tg_gate:family:his:<map>:<difficulty bits>` for his) and its own
`tg_perf` rows; the verdict pairs an armed and an unarmed fight only within one
kind and one boss context, so his presence can neither stand in for telemetry
cost nor hide it. The fail-closed verdict is unchanged: the mean of at least
`gate_pulls` differences (each one boss context within one kind, so a boss
measured in both kinds counts twice), `passed` at most `gate_max_ms`, `failed`
final, after which nothing is armed. The pass time is saved (`tg_gate`
`passed_at`).

A raid led by a bot sends dungeon-clear's muster naming the next boss, and
alternates by boss index as offline raids do. A raid he leads sends no muster,
so the gate musters itself at the run's start and after every finished pull. Its
parity slot is the kills so far plus the wipes since the last kill: a wipe's
retry falls on the other side, and each boss's first pull keeps its kill
position, so a repeated route puts every boss on the other side on the next
visit whatever the wipes. The same self-muster arms every pull of his lead once
the gate has `passed` (before, a raid he led was never armed).

A disarm never follows an armed pull's terminal at once: native drops a pull
whose epoch moves before it is confirmed and encoded. `record=false` waits until
that pull's header is stored or 10 s pass, and the sampler's ticker sends it;
arming is never held back.

While the gate measures, an armed pull of his raid is evidence only: cast-timed
callouts (`voice_lead`) and the combat-log facts in the lead's kill and wipe
calls (`facts.TalkHook`) wait for `passed`, and tactics still need the applier's
`passed` gate and observation month. Health notes a gate in `ab` with no new A/B
fight for 7 days (`training-gate`).

Before the gate passes, stored his and offline pulls are its sample, not every
pull. Bests and records are therefore claimed only from pulls that started
after `passed_at` (`store.full_since_ms`): the Realm's "faster than us"
(`realm/race.py` `_guild_best`) and the lead's "fastest kill yet"
(`facts.TalkHook`). Medians and descriptive counts may still use the sample.

# What hurt them

`python -m leeroy_mind training hurt [--context MAP:BOSS:DIFFICULTY|MAP:*:DIFFICULTY]
[--last N] [--kind his|offline|benchmark]` reads stored pulls (read-only, never
migrates): per boss context, newest first, deaths by the killing blow's spell,
the spells that hit cast members hardest (requested damage, ranked by share),
the failed mechanics the checks proved (`tg_failure`) and, for the boss's
avoidable effects in `mechanics.py`, which members the records show hit and how
often past the mechanic's allowance. The owner is left out of every section. Kinds default to his and offline raids;
`--last` keeps each context's newest N pulls. Only complete killed or wiped
pulls are read. Spell names come from the client's Spell.dbc (local files, no
database login); without it, from meta `tg:spell_names`, which the training
ground fills with the names of the spells its stored pulls name and of every
avoidable effect's spells. Its pass at each book load reads only pulls stored
since the last one (meta `tg:spell_names:rowid`). The console's Training tab
shows the same report for the 12 most recently fought bosses.

The rotation lab (`[training] lab`) starts a night only after this process has seen a hello whose
`features` list `dummy_party_on_arrival` (`lab_runner.DUMMY_FEATURE`). A native without it formed each
dummy-trial bot's solo party before the teleport to Orgrimmar, and the core disbands a one-member group as
its bot lands on another map, so every trial it accepted failed `ownership_changed` and blocked the lab
(2026-11-10..14). Against such a native the lab claims nothing, sends nothing and logs
`training_lab_native_unsupported` once; its reason reads `native_dummy_unsupported`.

A lab blocked on its current trial clears itself once native has said that trial let go of its bodies:
the trial's `dummy_cleanup` receipt (`released` or `restored`), or a hello from a worldserver that started
after the trial (`lab_recovery.recover_blocked_lab`, on the lab's next tick; `training_lab_recovered id=
trial= evidence= members=`). It ends the night exactly as the operator's `training lab-clear` does (no
result, every receipt kept, a summary with the halt as its reason, `recovered.by = "native"`) and releases
the lab's body claims. A member with a standing setup or a record that would not validate keeps the block
(`training_lab_recovery_refused reason=`, once per reason); any other block (no trial in flight, a mind-side
restoration) stays the operator's. On 2026-11-23 native released its trial at 07:01 (`unfinished=1507`) and
the lab's 8 members stayed held for more than a day until an operator could stop the mind for `lab-clear`.

The lab reads its members' bodies in one snapshot (`TrainingPorts.holders_of`, `BodyLedger.holders_of`):
its eligibility pass over the cast, its "still ours" check and its preemption check. One `holder()`
per member read every body source again, the whole Realm journal included, and on 2026-11-22 held
the mind's event loop at 100% CPU for 40 minutes until `[training] lab = false`.
Discovery speakers and the weekly spread score pass also use one snapshot per eligible
cohort. Spread refreshes the selected recipients and sources together again immediately
before committing plans. Missing entries remain held; a batch never extends an old proof.

## Raid debrief

`python -m leeroy_mind training debrief --data-dir <mind-data>` prints a JSON
report for the latest completed `guild_raid` (the current raid if none has
completed). `--raid ID` selects an exact saved raid, including an open one;
an unknown ID never falls back to another run. The console's **Ops → Raid
debrief** uses the same report, cached for a minute. Boss sections expand with
the keyboard or mouse; older runs remain available through the command.

The command opens `mind.db` with `mode=ro` and `query_only`. It can run while
the mind holds its lock. It does not migrate, rescore, collect new telemetry,
change feature switches or connect to a service. Native logs default to the
data directory's sibling `build/bin/RelWithDebInfo`; `--native-log-dir PATH`
selects another retained log directory. Native timestamps use the server
host's local zone. Use `--log-timezone America/New_York` when reading copied
logs on a different host. Ambiguous DST seconds are rejected and reported.
Mind JSON timestamps must include an offset.

Evidence is deliberately separated:

- Exact `tg_pull.raid_id` records supply boss attempts, incomplete/aborted
  recordings, per-member spell hits, deaths, checker failures and missing
  check coverage. Damage is the training hurt report's **requested damage**,
  not actual health lost. Hits alone cannot prove someone stood in a hazard.
- `mechanic_response` supplies completed responder activations and bots issued
  movement. The report requires a matching map, instance, playbook mechanic
  and a unique pull containing the entire logged second. Boundary/overlap
  ambiguity remains unassigned. Repeated activations count move
  **participations**, not distinct members or successful arrivals. Periodic
  empty activations can be suppressed by native logging.
- Every retained `group_rez_blocked` reason/fence combination is counted,
  including comma-separated fences. These logs are rate-limited and lack
  boss/group identity. They and hazard observations stay in an **unassigned**
  section for the raid's time window; other groups may be represented there.
  Hazard maxima are reported samples, not distinct bots or actual damage.
- Offline `short`, `full_wipe`, stopping and terminal records require an
  explicit raid-to-native-command binding from the owned receipt or the
  locally produced `guild_offline_raid` result event. Discord events cannot
  bind a job. Mind stop/outcome records use their explicit raid ID. Bound
  result events and terminal logs may arrive up to an hour after the saved
  session close; that grace never extends the unassigned hazard/rez window.

RS, Ulduar, EoE and Big Bang use the playbook responder IDs. Twin essence
clicks, Valithria healing/portal actions and successful group resurrection do
not currently emit direct success telemetry: the report says **unverified**,
even if a boss died. Unpulled bosses are also shown as unverified. No empty
log section or incomplete recording is a pass.

Reads are bounded: 256 pulls, 10,000 rows per evidence table, 20,000 member
spell entries, 64 log files, 32 MiB and 12,000 log events; long/partial lines
are skipped. Offline timelines and unassigned responder rows show at most
256 records. Every reached cap is labelled. Paths cannot traverse links or
junctions, active logs open with rename/delete sharing, and file identity is
checked after opening. Output contains selected IDs, codes, counts and static
playbook labels, never raw logs/chat/payloads. The CLI has an eight-second
query/read budget and Ops four seconds with one worker at a time. Retention
gaps and suppressed records cannot be disproved, so log counts are always
lower bounds. A read-only report requires no migration or native rebuild.
