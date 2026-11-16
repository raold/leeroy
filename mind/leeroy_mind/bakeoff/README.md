# The bake-off (`leeroy_mind.bakeoff`)

Which model, thinking setting and prompt make the members read most like a person? The
bake-off asks other candidates the guild's own captured turns again, with the mind stopped,
and scores every candidate with the Turing judge (`turing/README.md`) against the owner's own
lines. It answers "would this model do better on *our* turns", not "is this model good".

## 1. Capture (in the live mind)

```toml
[bakeoff]
capture = true   # default false: nothing is captured
```

Read once at the mind's start; a missing, invalid or unknown key means off. With it on, the
talk pipeline stores each **sent** model turn in `bakeoff_turn` (migration `*_bakeoff.sql`): the
exact `messages`, `options` (sampling, stop, `num_predict`, `num_ctx`), the `think` flag, the
model, the model's reply as it came (before the pipeline's repairs), the turn kind, the talk
channel, the speaker's guid, the line ids it became (`refs`: `c:<chat_line>` or
`d:<discord_message>`, every line of a burst) and the time. A turn keeps each model call it
makes (at most 8); when its line is recorded, the call whose reply the sent words share the most
words with is stored (a draft kept over a worse retry, a guard's preferred draft, the owed
fallback), never a dropped one. A line that shares no word with any reply (written by code: raid
calls, announcements) is not captured, nor is a voice-room turn (spoken lines are outside the
Turing corpus).

Capture never slows or changes a turn: the request is kept on the turn after the model answers
and written after the line is recorded, inside a savepoint of the line's own transaction; any
error is logged as `bakeoff_capture_failed guid= code=` and swallowed, and the line stands.

**Prune.** Rows older than 14 days are deleted at every mind start (whatever the switch says)
and each night by the model-free stage `bakeoff` (after `threads`), which is attached while the
switch is on or captured rows remain. Logs: `bakeoff_capture on= pruned= rows=` at start,
`bakeoff_pruned night= rows=`.

**Privacy.** A captured turn holds the full prompt: the persona card, her memories, the chat
she was shown and his own private lines, explicit DMs included. It stays in `mind.db` on this
machine and is never exported: no console view, no CLI that prints it, no log. Note that
`ac-guild-backup.sh` copies the whole `mind.db` (an SQLite online backup), so captured rows of
the last 14 days ride along in the off-machine guild backup, as do the mind's own snapshots
(`mind-data/snapshots`). Turn capture off and restart the mind before a backup if they must
not; the next start's prune empties them two weeks after the last capture. Size: roughly one
prompt (up to ~8 KB) per sent turn.

## 2. Run (the mind stopped)

```bash
D=F:/azerothcore/mind-data
.venv/Scripts/python -m leeroy_mind bakeoff run --data-dir "$D" --config bakeoff.toml --dry-run
.venv/Scripts/python -m leeroy_mind bakeoff run --data-dir "$D" --config bakeoff.toml \
    [--turns 150] [--windows 8] [--kinds whisper,dm,guild,...] [--out DIR] [--seed bakeoff]
```

- **Refuses** while the mind runs (its instance lock is held for the whole run), while WoW's
  client is open (or the process list cannot be read), and while the console's Free GPU pause
  is set. `--dry-run` takes no lock and touches no model: it prints the plan and the models
  that are not pulled, so it is safe beside a running mind.
- **mind.db is only read** (`mode=ro`). Results go to `--out` (default
  `<data>/scratch/bakeoff-<time>`; a folder inside a git checkout, or reached through a link
  or junction, is refused): `results.sqlite` (the plan, every candidate's reply and typed
  lines, the judge's verdicts), `report.json` and `report.md`. The same `--out` again goes on
  where a run stopped: errors are asked again, the stored plan is kept.
- **The GPU.** It first makes room (every loaded model but the embedding model), runs one model
  at a time (`keep_alive` 5m per call, the model unloaded when the next begins), then the
  reflection model for the judge, and unloads every model it used at the end. Models that are
  not pulled are listed as missing and skipped; the tool never pulls one. Native
  mod-ollama-chat can still load Orion while it runs (bot chatter near a player): for clean
  timings stop the worldserver too.
- **Which turns.** Captured turns of the last 14 days (the 14 before the stored plan's end on a
  resumed run) whose every line is in the Turing corpus (typed, not spoken, not deleted), of
  the chosen `--kinds` (`whisper`, `dm`, `gdm`, `discord`, `guild`, `officer`, `party`, `raid`,
  `say`, `yell`). First the judge's windows (8-12 lines of one conversation, alternating with
  and without his lines) where every member line is a whole captured turn, so all of them can
  be swapped; then single turns, evenly across the families and members, up to `--turns`.
  Seeded. Group DMs count in the Discord family as in the Turing judge; the judge never keeps a
  phrase from them.
- **Asking again.** Each turn goes to the candidate alone with the captured options (same
  sampling, stop, `num_predict`, `num_ctx`), a seed fixed per turn (the same for every
  candidate), the candidate's model and `think` (always sent), and with thinking on
  `num_predict` raised by `think_budget`. A cut Discord reply is trimmed to a sentence end as
  the live pipeline does. The thinking is counted (`thinking_tokens`), never stored.
- **Typing.** Every reply, and the captured live reply (the `live` baseline), takes the live
  pipeline's model-free steps: what it copied from the chat frame goes, the repairs, "(nothing)"
  is silence, then her typing (`style_lines`) as the console's tuning preview types a line:
  sober, her typing profile, her texture with `[talk] texture`, a Discord message's length band.
  One random generator per turn, the same for every candidate.

## 3. What it measures (`report.md`, `report.json`)

Per candidate, plus two baselines: `sent` (what the guild actually sent) and `live` (the live
model's captured reply typed as every candidate's is: the fair comparison), and per family
(game, private, discord):

- **(a) Stylometric AUC against his lines** (`turing/stylo`): the candidate's lines stand in the
  corpus in place of the members' lines of those turns; only they are sampled on the members'
  side (the same features and caps: at least 30 of each, no member above an equal share). The
  headline reads the words alone (`stylo.TEXT_KEYS`); the last table adds the timing features
  (latency, hour, bursts), the night's own measure, which a new model cannot change.
- **(b) The blind judge's balanced accuracy** (`turing/judge`, the reflection model,
  `[models] reflection`): the planned windows with every member line the candidate's, masked
  as the night masks them. 0.50 is chance.
- **(c) Detectors without a model** (`eval/talk`): repeats of her own last 20 lines, phrasing
  three or more members share, stock openings, filler after his question, and, only when the
  embedding model is already loaded (never loaded for this), replies that drift from his line.
- **(d)** how often the answer is distinct from what was sent, the median words, silences,
  errors, median seconds and thinking tokens.
- The top tells per candidate: the style features in words and the judge's caught tells (its
  phrases only from public channels, as the night keeps them).

## 4. The blind sheet

```bash
.venv/Scripts/python -m leeroy_mind bakeoff sheet --out DIR [--n 20]
.venv/Scripts/python -m leeroy_mind bakeoff tally --picks DIR/picks.json [--out DIR]
```

`sheet.md`: n turns that answer his line (whispers and DMs first), each with the conversation
before it (him as `you`, the member as `P1`, every other name masked as the judge masks) and
every candidate's reply, `live` included, shuffled and labelled A, B, C. `key.json` beside it
says which is which. He writes `picks.json` (`{"1": "B", "2": "-"}`; `-` is none) and `tally`
counts the picks per candidate (also `tally.json`). **The sheet quotes his own whispers and DMs,
explicit ones too: local only, never posted, uploaded or committed; delete it after.**

## Candidates

`--config` is a TOML file of `[[candidate]]` tables:

```toml
[[candidate]]
name = "orion"                 # 1-40 letters, digits, . _ -; not sent or live
model = "hf.co/bartowski/TheDrummer_Orion-26B-A4B-v1.1-GGUF:IQ4_XS"
think = false                  # default false
# think_budget = 160           # with think: extra num_predict for the thinking (default 160)
# prompt_variant = "as_is"     # or "situation"
# timeout = 120                # seconds per call, 10-900
```

Prompt variants (`variants.py`): `as_is` sends the captured messages unchanged. `situation`
keeps only the three hard rules of the system prompt's "How you talk here:" list (no talk of AI;
write only your line; no other people's lines) and drops the built-in soft ones (2009 slang,
react to what happened, moody or wrong, no guilt-trip, answer (nothing), the length rule);
everything else stays (the persona card, the flirting rule, his tuning and nudge lines, the
chat, the memories, the turn's instruction). A bullet it does not recognise as built-in (an
owner overlay's wording) stays too. **It is a stand-in for the design's section 9 prompt**,
which will also spend the freed tokens on what she is in the middle of; here they are unused.
To add a variant, add a function to `VARIANTS` and its name to `config.VARIANTS`.

The first run's candidates (design addendum, section 8):

```toml
[[candidate]]
name = "orion"
model = "hf.co/bartowski/TheDrummer_Orion-26B-A4B-v1.1-GGUF:IQ4_XS"

[[candidate]]
name = "orion-think"
model = "hf.co/bartowski/TheDrummer_Orion-26B-A4B-v1.1-GGUF:IQ4_XS"
think = true
think_budget = 160
timeout = 180

[[candidate]]
name = "orion-situation"
model = "hf.co/bartowski/TheDrummer_Orion-26B-A4B-v1.1-GGUF:IQ4_XS"
prompt_variant = "situation"

[[candidate]]
name = "cydonia"
model = "hf.co/bartowski/TheDrummer_Cydonia-24B-v4.3-GGUF:IQ4_XS"   # on disk (the fallback)

[[candidate]]
name = "cydonia-situation"
model = "hf.co/bartowski/TheDrummer_Cydonia-24B-v4.3-GGUF:IQ4_XS"
prompt_variant = "situation"

# Pull first (each ~15-20 GB on F:), then uncomment; a missing one is only listed as missing.
# [[candidate]]
# name = "gemma3-27b"
# model = "gemma3:27b"
# [[candidate]]
# name = "mistral-small-3.2"
# model = "mistral-small3.2:24b"
# [[candidate]]
# name = "qwen3-32b"
# model = "qwen3:32b"
# think = false
```

No cloud candidate: it waits for the owner's explicit yes (privacy; no explicit DMs leave the
machine).

## What it can't tell

- **One model's line in another model's conversation.** Every turn's context (the chat she was
  shown, her own earlier lines) was written by the live model; a candidate answers one turn
  inside it. A model that would steer a whole conversation differently is only seen one line
  at a time.
- **The judge is itself a model** (the reflection model), with its own blind spots; the same
  model judges every candidate, so compare candidates with each other, not with the truth.
- **Small samples.** A few dozen lines a family; a difference under about 0.10 AUC is noise
  (`turing/README.md`, Limits), and his own lines are few in some families (Discord channels).
- **Typing is approximated**: the live turn's mood, drink and time of day are not replayed;
  every candidate and the `live` baseline are typed the same way, `sent` keeps the real typing.
- **Timing is not the model's**: latency and bursts are the pipeline's; the text-only AUC is the
  model comparison, the all-features table the night's view.
- No guard (`[guards]`), retry or owed-answer fallback runs on a candidate's reply: a silence
  is counted as a silence.
