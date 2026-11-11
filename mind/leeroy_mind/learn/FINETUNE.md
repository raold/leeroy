# Optional, manual preference training

These commands export private data and evaluate supplied outputs. They do not
train, download or load a model, start a service, change a live model or deploy
anything. Training is a separate owner decision on an isolated machine or while
the live GPU is not in use. Keep the current model available for rollback.

## Make datasets

```powershell
python -m leeroy_mind learn export --data-dir F:/private-mind-data --out F:/private-learning/train --since 30
python -m leeroy_mind learn evalset --data-dir F:/private-mind-data --out F:/private-learning/eval
```

`export` excludes the last seven days; `evalset` uses only those seven days.
Both mine a temporary SQLite backup, then remove it. They never migrate or write
the source database. Each output directory contains `pairs.jsonl` and a manifest;
evaluation also has an ID-to-source index. Output files are created exclusively,
so reruns need a new directory. Any path inside a Git checkout, including a linked
worktree or a symlink into one, is refused. The default eval directory is under
the data directory's scratch folder; if that is inside a checkout, pass an
explicit outside path with `--out`.

A pair has `context`, `chosen`, `rejected`, `source`, and `member_guid_hash`.
Context contains up to six prior lines in the same room, channel kind and a
name-free persona summary (age, gender, typing and trait scores), with no system
prompt. Member hashes use a fresh random key for each export. This is private
conversation data, not an anonymous public dataset. Evaluation pairs add an `id`.

Sources, in precedence order when the same pair qualifies twice:

- A stored rejected guard draft and the rewrite that was actually delivered.
- Up/down votes for the same member and channel kind within seven days; exact
  Discord/voice rooms, private pairs and group audiences stay separate.
- Rewards at least 0.5 versus at most 0, with parent-line cosine at least 0.8.
  Parents must be proven by a trigger receipt or reciprocal directed adjacency.
  Stored vectors must match both the text hash and embedding-model fingerprint.

No original draft or embedding is reconstructed. To retain future evidence,
explicitly set `[learn] enabled = true` and `capture_preferences = true`, then
restart the mind. Capture defaults off. It records acknowledged rewrites and
reuses parent/context embeddings already computed by enabled semantic guards;
it makes no extra model request. Plain lexical guards provide no vectors. Old
history without this evidence can still produce vote pairs. Burst membership,
when installed, pools fragment votes and supplies the whole delivered generation;
a generation crossing the train/held-out boundary is omitted.

Whispers, DMs and private voice rooms require `--include-private`. Officer chat,
the officers' voice room, restricted Discord and unknown rooms never export.
Known real-name matches drop the whole pair, including its context. Explicit
content is excluded unless `--include-overlay-content` is supplied; that option
prints a warning. Text screening uses bounded patterns and is not a substitute
for inspecting a dataset before training. Neither flag changes live content rules.

Mining is capped at 20,000 candidate lines and 5,000 pairs per run; the manifest
reports truncation. `--since` is 1–3650 days (default 30). A training window of
seven days or less is empty because that period is held out. Keep the evaluation
files out of every training and hyperparameter-selection input.

## Evaluate a candidate without running it here

On the owner's isolated inference setup, generate one answer to each evaluation
context and write JSONL rows with `id` and `text`. An optional 512-value `embedding`
plus the manifest's `model_hash` enables semantic checks; the vector must have
been produced by that exact embedding model. The scorer never obtains one itself.

```powershell
python -m leeroy_mind learn score --data-dir F:/private-mind-data --evalset F:/private-learning/eval --candidates F:/private-learning/candidate.jsonl
```

Scoring reads another temporary backup of the same source database. Changed or
missing source lines are refused. The existing guard adapter runs task 12's
repetition, relevance/filler, contradiction and narrow world-evidence checks,
with historical time, channel and audience fences. Results contain IDs, action,
codes, numeric scores and coverage only. No prompts or candidate text are logged.
Without matching vectors, semantic relevance/contradiction coverage is absent;
zero reported hits is not a measured pass for those checks. This harness does
not assess all meaning, factual truth or personality quality.

Compare the unchanged base model and each candidate on the same held-out cases:
detector and guard rates at equal coverage, owner-rated answer quality and voice,
privacy mistakes, contradictions, repeated phrases, missed questions, response
length and inference latency/VRAM. Inspect private/public examples separately.
Do not treat a small win in these proxy counts as permission to change the live
model, dials, real-name screen, age floor or image/content gates.

## A later QLoRA experiment

Choose a compatible open causal model whose license permits the intended use.
Pin the base revision, tokenizer, chat template and training-library versions.
Convert `context` into a system-free conversational prompt and keep the two
completions as `chosen`/`rejected`; TRL's preference format uses those fields with
an explicit prompt. Use the same formatting for training and evaluation.
[TRL preference formats](https://huggingface.co/docs/trl/dataset_formats)

In a separate training environment, load that base with a supported four-bit
configuration, prepare it with `prepare_model_for_kbit_training`, and attach
LoRA adapters. NF4 is an available four-bit choice; select target modules and
precision for the actual architecture/hardware. Save the configuration and
seed alongside the adapter. This repo deliberately installs no training stack.
[PEFT quantization guide](https://huggingface.co/docs/peft/developer_guides/quantization)

Use a small, version-pinned DPO experiment over the training pairs, preserving
the base/reference behavior and holding the evaluation week out. Consult the
installed version's trainer API, rather than assuming one universal script or
hyperparameter recipe. Compare to the unmodified base before expanding a run.
[TRL DPO trainer](https://huggingface.co/docs/trl/dpo_trainer)

After independently validating an export compatible with Ollama (merged
Safetensors weights or a supported, already prepared GGUF), create a new local
candidate name in an isolated Ollama installation. For example, place this in
a `Modelfile`, then run `ollama create leeroy-candidate` in that directory:

```text
FROM /path/to/compatible/merged-safetensors-directory
```

Ollama's supported imports and architecture constraints must be checked for the
chosen base. GGUF quantization happens before import. Keep the candidate separate
from the live model name and run the same evaluation again after conversion.
[Ollama model import](https://docs.ollama.com/import)
