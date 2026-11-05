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
