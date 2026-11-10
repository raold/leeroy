# Persona Insight

Choose a member in Personas. Insight reads the existing database and audit reports;
it cannot edit a persona, adjudicate a contradiction or send a line.

- **Claims:** the newest 100 live claims and 100 superseded rows, with kind, subject,
  polarity, seen count, last seen and plan expiry. Expired plans are not live.
  The owner can inspect claim metadata; the awareness card applies the member's
  narrower visibility rules.
- **Awareness:** the awareness package's exact render at 200, 600 or 1200 estimated
  tokens, with dropped parts and missing sources. This is a guild-context view of
  stored observations, without a query embedding, private reply partner, current
  game observer or live bank request. It is not a copy of a particular live prompt.
- **Talk quality:** the latest readable task-12 JSON audit in the data directory's
  `scratch/`, four detector rates and their denominators, up to eight compatible
  runs, and rank by distinct flagged-line count (highest first, then rate and guid).
  Fake and judged modes are labelled. These are candidate flags, not accuracy
  measurements. Older-than-seven-day reports are marked stale. No report excerpts,
  private messages, file paths or embedding model names reach the page.
- **Guards:** task 20 does not persist verdict history. The panel says counts are
  unavailable; successful-send allowance receipts cannot reconstruct rewrite,
  react or drop decisions. No fabricated counters or new telemetry migration.

Wall/table badges contain counts only. The claim badge counts **potential** open
conflicts: live rows of the same kind and normalized subject with opposite
polarities. There is no stored adjudicated-conflict journal, so compatible nuances
may still trigger that heuristic. Superseded and expired claims do not count.
The repetition badge counts flagged lines only when the member's rate is strictly
above the guild's nearest-rank 90th percentile in a nonstale report. Members with
no measured lines are excluded. A missing claim table does not hide a valid
repetition badge.

## Check a line

The form uses `POST /api/persona/check` so the typed line is absent from URLs,
history and access logs. It is a read and works with `--no-control`; it still
requires the console key, allowed Host, same Origin and JSON content type. The
console uses its trusted mind link and the mind's new `claim_check` op/key.
Input is one guid and 1–500 characters. A stopped, unavailable, old or busy mind
returns words rather than silently attempting a different operation.

The mind checks live claims and embedding compatibility, asks which model is
already resident, then makes one background embedding call and invokes the
existing `claims.check`. It never requests a model load, chat call or judgement.
The read has a single-flight lock and a 15-second deadline. The library-style
residency check is an observation, not a reservation against an external actor
unloading the model between inspection and inference. Returned candidates contain
claim id, subject, score and reason, not the input or stored claim text. The line
and model exceptions are never logged or persisted. Changing members clears the
form and discards an old check's reply; polling preserves the current draft only
in page memory.

## Reads and missing sources

`GET /api/persona/insight?guid=` is key/Host checked and returns independent claims,
awareness, quality and guards sections. SQLite connections use `mode=ro`; source
queries have a 300ms VM budget. Badges ride on the existing roster request rather
than making a request per tile. Report reads inspect at most 256 directory entries,
4MiB per file and 16MiB per scan with a one-second scan budget. Symlinks, junctions
and reparse entries are skipped. A scan that cannot identify the latest report
within those limits says so. Eight bounded cache entries share scans for 30 seconds.

When no report exists, the panel shows:

```text
python -m leeroy_mind eval talk --data-dir D --since 14 --fake --json
```

Replace `D` with a prepared scratch copy's data directory. The command is text,
not an action: an operator chooses when to copy data and run an audit, so opening
a read-only profile never starts an evaluation or touches a live model.
