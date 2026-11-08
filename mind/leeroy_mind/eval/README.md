# Secret-probe diagnostics

`run_secret_probes` and `record_probe` remain the only evaluation and persistence
path for a complete secret-probe gate receipt. An empty answer still aborts that
evaluation. `(nothing)` remains an explicit non-leaking answer under its existing
scoring rules. Prompts, production options, cleanup and gate policy are unchanged.

For a controlled investigation, call
`await diagnose_secret_probes(client, live_model, identity)` from
`leeroy_mind.eval.probe_diagnostics`. Pass an already admitted client under the
existing model/scheduler ownership rules. This helper does not acquire GPU leases,
load a different model, change stop strings, retry samples, or persist anything.
It runs each of the 36 scheduled samples once, retaining technical failures so a
later sample can still be observed. Cancellation or an unexpected programming
error propagates and returns no report.

The result is explicitly diagnostic-only and never certifies the gate. `complete`
means the diagnostic schedule finished, including failures; `speech` is an output
shape, not a privacy pass. `returned` counts calls that returned a result, even if
its cleaned text was malformed. Only exact cleaned `(nothing)` is classified as
explicit abstention. No privacy score is computed and no `ProbeResult` is created.

Each sample contains prompt/request hashes and optional content-free transport
metadata from `ChatResult.diagnostics` or `LLMError.diagnostics`. Raw missing,
null, invalid, empty, whitespace-only and nonempty fields remain distinguishable
from cleaned output. Metadata contains fixed labels, lengths, completion state
and strictly validated counters/timings, never response or thinking text,
exception bodies, or arbitrary provider fields. Unknown completion reasons map
to `other`. Existing fake/custom clients may omit diagnostics.

The report hashes the configured model name and exact suite/options; it cannot
identify the running model weights through this client interface. `model_digest`
is therefore explicitly unavailable. A controlled real-model run must separately
pin and verify model residency/digest before and after the schedule. Keep any
reports private. Do not relabel a diagnostic report, selectively retry empty
samples, or change a prompt/options arm to obtain a production gate receipt.

## Stored talk audit

From the intended `mind/` checkout:

```sh
python -m leeroy_mind eval talk --data-dir /path/to/mind-data --since 14 --fake --json before.json
python -m leeroy_mind eval talk --data-dir /path/to/mind-data --since 14 --fake --baseline /path/to/mind-data/scratch/before.json --json after.json
```

This is a retrospective **candidate audit**, not a Turing test score or an accuracy
estimate. `--fake` uses FakeLLM's scripted claims extraction and deterministic 512d
word-hash embeddings; it never contacts Ollama, the bridge or a GPU. Its results
exercise the measurement pipeline and cannot establish how a real semantic model
would score the same history. The owner can run without `--fake` after stopping the
mind: the existing nightly model context holds the service lock before using the
reflection/embedding models. `--judge` optionally confirms contradiction candidates
with the existing claims judge and is refused with `--fake`. Nothing changes live
talk, prompts, personas, voices or generation policy.

Every run uses the existing nightly read-only SQLite backup and scratch-store
helpers. It makes a fresh, uniquely labelled database under `<data>/scratch/`,
migrates only that copy if needed, and never changes or migrates the source. The
claims replay and temporary repetition index write only to the copy. There is no
`--live-db` mode. The 14-day default is a rolling window of 14 x 86400 seconds;
`--since DAYS` changes it. Future rows are excluded. Mind-origin lines are the
denominator; the owner's human-origin lines provide context only. Up to five minutes
before the window can supply context, never an evaluated line. Historical members
with mind-origin lines remain in the report even if they are absent from today's
roster. This is a stored-history audit, not a fresh observation of the game.

Each detector lives in its own `eval/talk/` module:

- **Contradiction:** clear only the scratch copy's claims, then replay each member's
  lines in receipt-ID order using `claims.check.check()` before activating claims
  cited to that line. Uncited claims activate at their batch's end. Extraction uses
  the existing 40-line / eight-claim bounds and 500-character screen. Unlike the
  existing claims smoke eval, this processes all batches in the selected window.
  It inherits claims' subject, polarity, negation and replacement rules, eight-neighbor
  retrieval, and their documented limitations. An extraction sees the whole batch,
  so it is not a blinded evaluation. Legitimate changes of opinion can be candidates.
- **Repetition:** within one member, compare against earlier calendar days using
  normalized word-trigram Jaccard overlap >= **0.70**, or embedding cosine >= **0.94**.
  A lexical index catches matching trigrams; the scratch sqlite-vec index supplies
  the nearest **16** earlier-day vectors in that member's partition. Four-word minimum
  avoids treating ordinary short acknowledgements as repeated arguments. Across the
  guild, a shared **five-word** phrase from at least **three distinct members** in one
  evening is flagged, unless it touches a word of a named stop phrase
  (`repetition.STOP_PHRASES`: "what do you think", "let me know", "by the way", ...);
  on a live copy 4-grams made 10 of 12 flags out of such ordinary phrases. An evening is 18:00 through 05:59 in `America/New_York`, including
  midnight/DST crossings. Stock openings/closings are the same first/last **three
  words** on at least **three** of a member's lines and at least **30%** of their lines.
  Tactical calls, shared quotations and personal speech habits can be legitimate.
- **Non sequitur:** the schema stores `chat_line.thread_id` and `to_guid`, but neither
  `chat_line` nor `utterance` has a general reply-parent foreign key. Therefore the
  report explicitly records **zero stored parent links**. It considers only adjacent,
  reciprocal directed lines in the same thread and channel, within **300 seconds**, as
  parent *candidates*. Guild-channel adjacency alone is not a parent. A reply of at
  least four words is flagged only when cosine to its candidate parent is < **0.20**
  and cosine to every available preceding thread line is < **0.25**, using up to five
  context lines. A question ending in `?` followed solely by one to three tokens from
  `lol`, `lulz`, `lmao`, `rofl`, `haha`, `hehe` gets the filler rule. `yes`, `no` and
  ordinary acknowledgements are deliberately absent from that set. Inference,
  ellipsis, jokes and topic changes still limit precision; missing links limit recall.
- **Unawareness:** full-sentence, unquoted assertions only. “We have never killed
  <full boss name>” can conflict with an earlier published `realm_kill`, joined to an
  active `realm_fact_origin` with `verified_raid_kill_v1` / `verified_event`, for the
  stored `meta.guild_id`. Boss names come from the existing `wow.BOSS_NAMES` mapping.
  “We did not raid on YYYY-MM-DD” can conflict with an already completed `guild_raid`
  on that local date that recorded at least one kill or wipe. “I am in <zone>” can
  conflict with that line's nonzero stored zone, or the speaker's most recent `zone`
  game event at most **300 seconds** earlier. Names must resolve unambiguously from
  zone observations already present before the line. Each hit carries its rule and
  source/raid/event/line ID, never fact text.

The numerical thresholds are initial conservative engineering gates chosen to
separate the invented hit/near-miss/clean regression cases, **not calibrated on
private history**. The high repeat cosine favors near-identical semantics; both
low relevance gates must fire together to avoid rejecting an answer that continues
another recent thread line. Three members/three stock uses avoid flagging a single
coincidence. The five-minute context/position bounds avoid unbounded stale evidence.
All values are named constants and included in the JSON parameters. The embedding
batch is 64; lines over 4000 characters skip embedding and have a coverage count.
They are never silently truncated into a different semantic claim.

World-state limits are explicit: `fact` contains social beliefs and secrets, not
an authoritative world ledger. `realm_fact` prose without its verified origin is
not proof. Current `cast_member` membership/position is not historical membership.
The kick/readmission tables do not cover every ordinary join/leave, so this audit
does not infer membership failures from them. A missing `guild_raid` or Realm event
does not prove no raid happened; absent-raid checks remain zero. A killed boss can
respawn or belong to another instance, so ordinary “alive” claims are not flagged.
This initial ruleset omits those cases instead of asserting false certainty.

Reports contain per-detector and combined distinct flagged-line counts and rates
per **100 evaluated mind lines**, overall, per member (GUID only), and by local day.
Multiple hits/rules on a line count once in each detector rate, and once in the combined
rate. JSON also includes rule rates, evidence IDs, coverage counts, thresholds, and
top offenders ordered by flagged-line count, then rate (denominators are always shown).
Terminal output shows the overall detector rates, top GUIDs and day trends. It never
prints line, name, persona, prompt, model-response or exception-body text; dependency
logs are reduced to a fixed event code for the duration of the audit.

`--json [NAME.json]` writes inside `<data>/scratch/` only. Names are plain basenames;
absolute paths, escapes, symlinked scratch directories and existing reports are refused.
Without a name it creates `talk-<UTC timestamp>-<random id>.json`. `--show-text` requires
`--json` and adds at most 160 characters of each flagged line to that **private report
only**, never to terminal output/logs. Keep both scratch databases and reports out of
git. `--baseline FILE` compares the four overall detector rates and reports deltas per
100 lines. It refuses reports with another version, fake/real mode, judge mode, window
length, embedding fingerprint or parameter set. Model-name fingerprints do not certify
the actual weights; pin the real model separately when comparing model runs.

Tests use only invented members (101-105, owner 1), scratch databases, FakeLLM and
deterministic vectors. They cover hits/near misses/clean cases, temporal evidence,
report denominators, baseline changes, source immutability, path confinement, excerpt
opt-in, and content-free logs. Run `python -m pytest -q tests/eval/talk` for these tests,
and `python -m pytest -q tests` for the full suite. A fake run on a private copied
database is a pipeline baseline, never evidence of real-model accuracy.

## Privacy and private channels

`read_lines` measures every stored mind line, **including whispers and officer
lines**; they are not excluded because dropping them would hide real failures. Every
hit in the JSON carries its `channel` and a `private` flag (whisper, officer), and
the report header has `private_channels` (included, kinds, evaluated line count). A
`--show-text` excerpt of a private-channel hit is as private as the line: reports stay
in `<data>/scratch/`, never in git, chat or artifacts.

## Reuse on single lines (pre-send guards)

Detectors are thin batch loops over pure single-candidate helpers. Safe on a live
read connection (no SQL writes, no table creation): `repetition.best_repeat`,
`pair_score`, `phrase_keys`, `shared_by_others`, `stock_keys`;
`nonsequitur.score_reply`, `relevance_scores`; `unawareness.load_facts` (SELECTs only;
load once and reuse the `Facts` object) with `unawareness.check_line`;
`contradiction.check_line`, a thin call to `claims.check.check`. **Scratch only:**
`contradiction.detect` (deletes all claims, vectors and cursors) and
`repetition.detect` (temp tables and a temp vec0 index; its dimension comes from the
embedder's own vectors). Both raise `scratch_copy_required` unless the store was
marked by `common.mark_scratch`, which only the `eval talk` copy helper calls.

## Known limits

- **Unawareness has near-zero recall on real data.** Only exact full-sentence forms
  match; a live copy produced zero candidates, which says nothing about awareness.
- **Difficulty is not modelled.** A kill is matched by boss name, so "we never killed
  heroic X" after a normal-mode kill is reported as a contradiction; difficulty is
  not part of the evidence.
- **Non sequitur thresholds (0.20 / 0.25) were set on FakeLLM word-hash vectors**, which
  share only vocabulary. They are not valid for a real embedding model and must be
  recalibrated before any gate uses them.
- A `--baseline` that is not a talk report (missing detector rates) is refused with
  `baseline_malformed`.
