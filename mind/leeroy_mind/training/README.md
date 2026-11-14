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
