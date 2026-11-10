# Member claims

Remember what a member said about their own facts, opinions and plans, so a later
line can be checked for contradictions. Enabled live guards use this checker;
the claims night stage attaches to both the app and standalone night runner.
Neither switch is enabled by default.

Enable the extension with `[claims] enabled = true` in `mind.toml`. It is off by
default; an invalid table means off. `claims` runs after `notes` on the reflection
model. `NightlySettings.claims_per_night` defaults to 40 **members**, each with up
to 40 unread `chat_line` rows where `from_guid` is that member and `origin='mind'`.
Oldest unread input goes first, including deferred backlog. A night freezes its
member allocation so a restart cannot exceed its cap. Budget degradation may
reduce that allocation to zero; those lines wait for a later night.

Each member gets one `reasoner.json` invocation (with Reasoner's existing retry
for malformed JSON), no thinking pass. At most eight screened outputs are kept.
The prompt asks only about the supplied lines. Schema checks, own-statement and
lexical evidence screens reject malformed/unsupported output, meta text, the
owner's real name, and claims about another member. Another member may be the
object of an opinion. Lines longer than 500 characters are skipped, not truncated.
The optional `source_line` must belong to the supplied batch; without a citation,
the claim becomes effective at the batch's end and its stored `source_line` is
null. This is a conservative timestamp, not a fabricated citation.

The `member_claim` table keeps provenance, first/last observation, count, expiry
and supersession history. Sources are `chat` (extracted) and `owner` (reserved for
explicit corrections through the store API); this PR adds no correction UI.
Subject keys are casefolded and whitespace-normalized. Embeddings reuse `Embedder`
and sqlite-vec: 512 float32 dimensions, cosine distance, per-guid partitions,
16-vector chunks. Superseded and expired rows retain history; their vectors are
removed. Readers filter expiry even before nightly cleanup. Plans expire after
the supplied 1–365 days from the source timestamp, or seven days when undated.
Reinforcement does not silently extend a deadline.

All writes join `store.transaction()`. Claims, vectors, the per-member last-line
cursor and `night_step` receipt commit together. Cancellation or failure before
commit retries that unit; already committed input is never extracted again.
Logs contain only ids, counts, codes and timings. CLI inspection can display the
stored text, with the existing owner-name screen, but never logs it.

## Merge and check

For a given subject and kind, a compatible claim at cosine `T_SAME = 0.92`
reinforces its observation count and last-seen timestamp. This initial gate matches
the existing memory writer's conservative dedup threshold. An opposite polarity
or recognized replacement supersedes the earlier claim, even at high similarity:
opposites often embed closely. Other claims are added separately.

`check(conn, guid, text, embedding)` returns `Conflict(claim_id, subject,
claim_text, score, reason)` candidates without calling a model or writing. It uses
one KNN query with the guid and live-row filter applied before selecting eight
neighbours. It then compares short English clauses for polarity, negation, job or
home replacement, and cancelled plans. An explicit subject/predicate anchor is
required; a deictic reference such as “that fight” additionally needs cosine
`T_SUBJECT = 0.65`. Negation requires predicate-token Jaccard `T_NEGATION = 0.60`.
These last two gates were chosen to admit the invented regression paraphrases
while excluding unrelated subjects; they are heuristics, not calibrated scores.

Expected check cost is `O(512 × N)` vector work for this member's N live/indexed
claims, indexed row filtering, and at most eight short text comparisons. Expect
a few milliseconds for hundreds of claims on a local SSD, not a latency guarantee.
A local Python 3.14/Windows smoke benchmark of 500 checks over two invented
members with 500 claims each measured 0.75 ms median and 1.17 ms p95 (warm cache,
synthetic vectors, eight returned neighbours).
Embedding the proposed line is a separate caller cost. `async judge(reasoner,
text, conflict)` is an optional single JSON confirmation call; nothing calls it
automatically. A future integration must decide its own failure/timeout policy.

## CLI and evaluation

From `mind/`, using the intended Python environment:

```sh
python -m leeroy_mind claims --data-dir /path/to/mind-data status
python -m leeroy_mind claims --data-dir /path/to/mind-data list 101
python -m leeroy_mind claims --data-dir /path/to/mind-data check 101 "I hate Saurfang"
python -m leeroy_mind claims --data-dir /path/to/mind-data eval --fake
python -m leeroy_mind claims --data-dir /path/to/mind-data eval
```

`status`, `list` and `check` open the existing database read-only, load sqlite-vec
on that connection, and never migrate. An older schema reports unavailable.
`status` and `list` use no model. `check` uses only the configured embedding model
to embed its argument, then the model-free check.

`eval` uses nightly CLI's snapshot, scratch-store and model/lock helpers. It
creates a fresh copy under `<data>/scratch/`, clears only the copy's claims, and
extracts from each member's latest 40 own lines. It replays those lines in receipt
order, checking each line **before** activating claims cited to it. Uncited claims
activate at the batch's end; they cannot flag an earlier line. Reports give the
member/line/claim counts, conflict ids, reason codes and similarity scores, and
the scratch database path. The source database stays unchanged.

Without `--fake`, evaluation uses the real reflection and embedding models, holds
the service lock, and requires the mind to be stopped because the GPU is shared.
`--fake` uses `FakeLLM`, deterministic word-hash embeddings and a small scripted
extractor for opinions/jobs. It needs no network, Ollama or GPU and is a smoke
test, not an accuracy estimate.

## Limits

This is candidate detection, not a guarantee of consistency. A person can change
their mind or job legitimately. Sarcasm, implication, aliases, ambiguous pronouns,
multiple predicates, evolving subject keys and unsupported English constructions
can produce misses or false alarms. Named topic overlap is not proof of the same
aspect of a topic. Top-eight retrieval can miss a relevant older claim. Lexical
grounding cannot prove a model's assertion follows from its cited line.

Evaluation extraction sees the whole batch, so its report is a retrospective
audit, not a blinded precision/recall measurement. Model citations still need
review. No private history, persona content or real model evaluation is included
in this public repository. Claims about private conversations must not be exposed
by a future chat integration merely because they are stored here.

Run the offline tests with `python -m pytest -q` from `mind/` after
`python -m pip install -e ".[test]"`.
