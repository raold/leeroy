# Memory callbacks

The selector and replay are model-free. Live talk uses them only when learning
application and the selected callback arm also enable it; see
[learning application](../learn/README.md#applying-learning-to-live-talk). The default is off:

```toml
[callbacks]
enabled = false
```

`load_callbacks_config(path)` returns false for a missing, unreadable or invalid
table, including unknown keys and non-boolean values. The live talk caller
passes that result as `CallbackContext.enabled`. Calling the selector without an
enabled context does not even read the database. Golden prompts are unchanged.

`pick_callback(conn, guid, context, now=seconds)` is read-only. The context carries
the current channel, private partner, observed people present, captured room
readers and an optional existing 512-dimensional memory-model embedding. Never
substitute a different embedding model's vector. Missing or malformed vectors
fall back to recency and salience. The function returns `Callback(memory_id,
seed, reason, age_days, scope)` or `None`. The seed is exactly the memory's own
stored summary, capped by the existing memory schema at 200 characters.

Eligible memories belong to the member, are active, 2–60 days old inclusive,
have importance at least 6/10 and confidence at least 0.8, and include at least
one other currently present participant. Plans, stories, hearsay and offscreen
sources are excluded. Event time determines age; creation time must also precede
the turn. The newest/highest-salience 300 candidates are considered. Similarity
(mapped to 0–1) or recency supplies 60% of the score, salience 30%, and an
emotional bonus 10%. Stored absolute valence at least 0.5 or a boss identifier
provides that bonus; no sentiment classifier or text keyword detector runs.
Ties resolve by newer event time and then smaller memory ID.

Privacy uses the existing whisper pair and memory scope rules. Every evidence
branch must also be visible: mixed public/private evidence remains private;
officer evidence requires a current officer room and the stored officer role;
historical party/local/other room evidence requires a matching channel and an
audience containing every current cast reader. Game recordings contain cast only;
the owner's guid (from `human_guid` meta) is excluded from this reader subset test,
but remains in the observed participants for the shared-actor check.
Mind-origin evidence additionally
checks the recalled memories in its paired, non-dropped utterance. Recursive
memory and chat provenance is checked with a 32-level, 500-node budget. Missing
or unsupported roots, cycles and exhausted budgets fail closed. Supported roots
are memories, chat lines and public boss/death/loot/achievement/battleground/zone
events. This deliberately omits other legitimate history whose visibility
cannot yet be proved, including owner-authored roots and encounter/story roots.
It never reads another scope's raw text into the returned seed.

Selection does not consume a cooldown. After a successful send,
`record_callback(conn, guid, context, callback, now=seconds)` belongs **inside
the same `Store.transaction()` as the sent-line write**. It takes a write lock,
rechecks eligibility and quotas, and writes only IDs and time to `callback_use`.
It never commits. A failed send must roll back; a SQLite concurrent-writer error
requires retrying the whole transaction. A rejected or changed offer returns
false. The talk pipeline connects it to the first acknowledged fragment's transaction,
even if learning froze after composition: delivery accounting still starts the rest.

A memory rests for 14 wall-clock days after use. A member may use one callback,
and the whole guild three, during the last hour of recorded owner play. Session
intervals are merged so duplicate/overlapping sessions do not grant extra quota;
offline gaps do not refill it. The boundary includes the exact previous-hour
timestamp conservatively. No active recorded owner session means no offer.
Unprovable/truncated session history also means no offer. Receipts are kept so
a long offline absence cannot erase the play-time quota. Missing older schema
returns no offer and inspection never migrates it. Database errors other than
missing tables/columns remain errors.

From `mind/`:

```sh
python -m leeroy_mind callbacks eval --data-dir /path/to/data --since 7
python -m leeroy_mind callbacks eval --data-dir /path/to/data --show-text
```

Replay opens the source with `mode=ro` and backs it up to an in-memory scratch
database. It never migrates or writes the source, contacts services, or loads a
model. It explicitly simulates the switch on at the first 10,000 recorded member
turns in the requested 1–60 days, preserving existing receipts and hypothetical
cooldowns. The report labels this simulation, its cap, and unavailable schema.
It reports offers, age buckets, privacy scopes and reason counts. It estimates
availability from stored audiences/sessions and current persona roles; it is
aware that stored guild audiences contain cast members only, and adds the owner
only when that turn's recorded session establishes his presence. It is
not a historical audience reconstruction or a measure of conversation quality.
No embeddings are generated for replay.

Default output is counts only. `--show-text` writes selected seeds solely to a
new random-named JSON file under that data directory's `scratch/`; they never
appear in terminal output or logs. Scratch symlinks and Windows junctions are
refused. Treat these optional artifacts as private. Tests use invented members.
