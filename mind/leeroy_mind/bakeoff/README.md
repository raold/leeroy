# The bake-off (`leeroy_mind.bakeoff`)

Which model, thinking setting and prompt make the members read most like a person, and how much
of the gap is the model and how much the live pipeline around it? The bake-off asks other
candidates the guild's own captured turns again, with the mind stopped, and scores every
candidate with the Turing judge (`turing/README.md`) against the owner's own lines, beside three
baselines: what the guild sent, the live model's first draft and the live reply whose words were
sent. It answers "would this do better on *our* turns", not "is this model good".

## 1. Capture (in the live mind)

```toml
[bakeoff]
capture = true   # the default; false: nothing is captured
```

Read once at the mind's start; a missing key means on, an invalid or unknown one means off. The console's Ops
tab lists it in the switchboard. With it on, the talk pipeline stores each **sent** model turn in
`bakeoff_turn` (migration `*_bakeoff.sql`), one row per generation that became a line:

- the call whose words were sent: the exact `messages`, `options` (sampling, stop, `num_predict`,
  `num_ctx`), the `think` flag, the model, the model's reply as it came (before the pipeline's
  repairs) and Ollama's `done_reason`;
- `calls` (how many model calls the generation made) and `attempt` (which of them was sent: 0 is
  the first draft);
- when a later call was sent (`attempt > 0`: a silence asked again, a retry for its tells, a
  guard's rewrite or fresh answer, the owed fallback), **the generation's first call too**
  (`first_messages`, `first_options`, `first_reply`, ...): the request before any retry hint;
- the turn kind, the talk channel, the speaker's guid, the line ids it became (`refs`:
  `c:<chat_line>` or `d:<discord_message>`, every line of a burst), the shape (lines, a Discord
  turn's length band) and the time.

A turn keeps each model call it makes (the first always, then the latest, at most 8). When its
line is recorded, the call whose reply shares the most words with the sent line is the sent one (a
draft kept over a worse retry, a guard's preferred draft, the owed fallback), never a dropped one.
A line that shares no word with any reply (written by code: raid calls, announcements) is not
captured, nor is a voice-room turn (spoken lines are outside the Turing corpus).

**The beat** (`[talk] think_private`, talk/beat.py): a private answer's beat lines are appended
to its line request. They never reach this table: each request is stored as it was before the
beat's lines (their `"\n" + beat` suffix taken out once from the last user message that holds
it), with only `beat = 1` and its `want` code. So every captured request is the line's request
without the beat; a candidate with `beat = true` asks a beat of its own (below).

Capture never slows or changes a turn: the request is kept on the turn after the model answers
and written after the line is recorded, inside a savepoint of the line's own transaction; any
error is logged as `bakeoff_capture_failed guid= code=` and swallowed, and the line stands (only
SQLite's own rollback of the line's whole transaction is raised again, as it would be anyway).

**Prune.** Rows older than 14 days are deleted at every mind start (whatever the switch says)
and each night by the model-free stage `bakeoff` (after `threads`), which is attached while the
switch is on or captured rows remain. Logs: `bakeoff_capture on= pruned= left=` at start,
`bakeoff_pruned night= rows=`.

**Privacy.** A captured turn holds the full prompt: the persona card, her memories, the chat
she was shown and his own private lines, explicit DMs included. It stays in `mind.db` on this
machine and is never exported: no console view, no CLI that prints it, no log. But:
- `ac-guild-backup.sh` copies the whole `mind.db` (an SQLite online backup), so captured rows of
  the last 14 days ride along in the off-machine guild backup, as do the mind's own snapshots
  (`mind-data/snapshots`). Turn capture off and restart the mind before a backup if they must not.
- A pruned row's bytes can survive in SQLite's free pages and the WAL until they are reused or a
  `VACUUM` runs; the online backup copies whole pages, so they can ride into a backup after the
  prune.
- Size: roughly one prompt (up to ~8 KB, twice that for a retried turn) per sent turn.

## 2. Run (the mind stopped)

```bash
D=F:/azerothcore/mind-data
.venv/Scripts/python -m leeroy_mind bakeoff run --data-dir "$D" --config bakeoff.toml --dry-run
.venv/Scripts/python -m leeroy_mind bakeoff run --data-dir "$D" --config bakeoff.toml \
    [--turns 150] [--windows 8] [--kinds whisper,dm,guild,...] [--from first|sent] [--out DIR] [--seed S]
```

- **Refuses** while the mind runs (its instance lock is held for the whole run), while WoW's
  client is open (or the process list cannot be read), and while the console's Free GPU pause
  is set. `--dry-run` takes no lock and touches no model: it prints the plan (how many turns the
  live pipeline asked more than once, how many had a beat), each candidate's think setting and
  the models that are not pulled, so it is safe beside a running mind.
- **mind.db is only read** (`mode=ro`). Results go to `--out` (default
  `<data>/scratch/bakeoff-<time>`; a folder inside a git checkout, or reached through a link
  or junction, is refused): `results.sqlite` (the plan, every candidate's reply and typed
  lines, the judge's verdicts), `report.json` and `report.md`. The same `--out` again goes on
  where a run stopped: errors are asked again, the stored plan and `--from` are kept, and a
  judge window whose lines changed since (a turn that failed then answers now) is judged again.
  **Nothing prunes these folders**: they hold the members' generated replies and masked context
  past the capture's 14 days; delete a bake-off's folder when it is done with.
- **The GPU.** It first makes room (every loaded model but the embedding model), runs one model
  at a time (`keep_alive` 5m per call, the model unloaded when the next begins), then the
  reflection model for the judge, and unloads every model it used at the end, an interrupted run
  (Ctrl+C) included. Models that are not pulled are listed as missing and skipped; the tool never
  pulls one. Native mod-ollama-chat can still load Orion while it runs (bot chatter near a
  player): for clean timings stop the worldserver too.
- **Which turns.** Captured turns of the last 14 days (the 14 before the stored plan's end on a
  resumed run) whose every line is in the Turing corpus (typed, not spoken, not deleted), of
  the chosen `--kinds` (`whisper`, `dm`, `gdm`, `discord`, `guild`, `officer`, `party`, `raid`,
  `say`, `yell`). First the judge's windows (8-12 lines of one conversation, alternating with
  and without his lines) where every member line is a whole captured turn, so all of them can
  be swapped; then single turns, evenly across the families and members, up to `--turns`.
  Seeded. Group DMs count in the Discord family as in the Turing judge; the judge never keeps a
  phrase from them.
- **Which request.** `--from first` (the default) asks every candidate the generation's first
  request, the one the live model was first asked, before any retry hint or guard rewrite;
  `--from sent` asks the request whose words were sent (on a retried turn, the hinted retry or
  the rewrite, which shows the candidate the live model's rejected draft).
- **Asking again.** Each turn goes to the candidate alone with that request's options (same
  sampling, stop, `num_predict`, `num_ctx`) and a seed fixed per turn (the same for every
  candidate). A cut Discord reply is trimmed to a sentence end as the live pipeline does. The
  thinking is counted (`thinking_tokens`), never stored. Errors keep a short code (`http_400`,
  `timeout`, ...) for the report.
- **Typing.** Every reply, and the captured first draft and sent reply (the `first` and `live`
  baselines, cut back the same way when num_predict cut them), takes the live pipeline's
  model-free steps: what it copied from the chat frame goes, the repairs, "(nothing)" is
  silence, then her typing (`style_lines`) as the console's tuning preview types a line: sober,
  her typing profile, her texture with `[talk] texture`, a Discord message's length band. One
  random generator per turn, the same for every candidate.

## 3. What it measures (`report.md`, `report.json`)

Per candidate, plus three baselines:
- `sent`: what the guild actually sent, with its real typing;
- `first`: the live model's first draft, typed as every candidate's is;
- `live`: the live reply whose words were sent, typed the same way.

So `first` against a candidate asked the first request is the model and sampling; `first`
against `live` is what the pipeline's retries and guards changed; `live` against `sent` is what
came after generation (typing, corrections). **On turns that had a beat live** (`[talk]
think_private`, nearly every answer to his whispers and DMs), `first` and `live` were drafted with
Orion's beat in the prompt, but the stored request has it taken out: there, compare `first` with a
`beat = true` candidate, not a plain one, or the difference is the beat, not the model. Per family (game, private, discord):

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
  embedding model is still loaded at the end (never loaded for this), replies that drift from
  his line.
- **(d)** how often the answer is distinct from what was sent, the median words, silences,
  errors (with their codes), median seconds and thinking tokens.
- **Retried and beat turns apart**: the line stats, the text AUC and two detectors again for the
  turns the live pipeline asked more than once and for those it answered on its first try, and for
  the turns that had a beat live and those that did not.
- The top tells per candidate: the style features in words and the judge's caught tells (its
  phrases only from public channels, as the night keeps them).

## 4. The blind sheet

```bash
.venv/Scripts/python -m leeroy_mind bakeoff sheet --out DIR [--n 20]
.venv/Scripts/python -m leeroy_mind bakeoff tally --picks DIR/picks.json [--out DIR]
```

`sheet.md`: n turns that answer his line (whispers and DMs first), each with the conversation
before it (him as `you`, the member as `P1`, every other name masked as the judge masks) and the
replies of every candidate and the three baselines, `sent` included, shuffled and labelled A, B,
C. Replies that read the same are one option (a turn answered on the first try has `first` and
`live` alike). `key.json` beside it says which is which. He rates every option 0-3 (0 empty,
wrong, a bot tell or out of character; 1 generic or sycophantic; 2 specific and in her voice; 3
would pass as her), as bake-off 1 was judged, and may pick one:
`{"1": {"scores": {"A": 2, "B": 0}, "pick": "A"}}` (`"-"` is none; `{"1": "A"}` alone is a pick
without ratings). `tally` gives per candidate the mean rating, the share rated 2 or more, and the
pick rate (also `tally.json`). **The sheet quotes his own whispers and DMs, explicit ones too:
local only, never posted, uploaded or committed; delete it after.**

## Candidates

`--config` is a TOML file of `[[candidate]]` tables:

```toml
[[candidate]]
name = "orion"                 # 1-40 letters, digits, . _ -; not sent, first or live
model = "hf.co/bartowski/TheDrummer_Orion-26B-A4B-v1.1-GGUF:IQ4_XS"
# think = false                # the default; true, or "default" (see below)
# think_budget = 1400          # a thinking candidate's extra num_predict (default 1400)
# num_ctx = 6144               # default: as captured, or for a thinking one captured + budget, rounded up to 1024
# prompt_variant = "as_is"     # or "situation"
# beat = false                 # true: ask its own beat first (talk/beat.py), as [talk] think_private does
# timeout = 120                # seconds per call, 10-900
```

**`beat = true`** (bakeoff/beat.py): the candidate first asks a beat on the turn's request through
talk/beat.py, as the live pipeline does for his whispers and DMs (the beat's instruction after the
request, `format` = the beat's schema, `think` false, the BEAT preset's sampling and num_predict
the data dir's `[talk] think_budget`, 240 if unset, a seed per turn and candidate), then asks its
line with the beat's lines after the instruction. A beat that fails the
schema, does not fit or errs is skipped and the line goes without it. What it may bring in is
read back from the request (the memories under "You remember:", the earlier chat lines that are
not hers, never the line she answers), since the composed prompt's own list is not stored. The
result keeps the beat's want code or why it was skipped, never its words; the report gives how
many turns used one and the wants. It combines with `prompt_variant` (bake-off 1's best was
Orion + beat + situation on public channels). The beat is asked on every captured turn, game
channels too, which the live pipeline does not do.

**`think` has three settings.** `false` (the default when the key is left out, as the live
pipeline sends it) and `true` are sent as the request's flag; **`think = "default"` sends no flag,
so the model does what it does by default**: Orion thinks only then (it answers `think: true` with
HTTP 400), Qwen3 thinks by default too. A candidate whose `think` is not `false` is a thinking
one: the captured `stop` is dropped (the live blank-line stop would
fire inside the thinking; the content is cut at its first blank line afterwards), `num_predict`
is raised by `think_budget` and `num_ctx` raised to fit (4096 + 1400 becomes 6144).
**Bake-off 1 (2026-11-16) found native thinking worse on Orion** (5-7 s, 400-550 words of prompt
recap, 15-30% empty answers) and Qwen3 32B thinking empty at a small budget; the setting exists
for other models, not to retry Orion's.

Prompt variants (`variants.py`): `as_is` sends the captured messages unchanged. `situation` is
bake-off 1's situation variant: of the system prompt's "How you talk here:" list it keeps the
three hard rules (no talk of AI; write only your line; no other people's lines), the rule against
invented shared history ("React only to the chat shown...", whose absence bake-off 1 blamed for
the dense models' invented events) and the (nothing) rule, and drops the built-in soft ones (2009
slang, react to what happened, moody or wrong, no guilt-trip, the length rule); everything else
stays (the persona card, the flirting rule, his tuning and nudge lines, the chat, the memories,
the turn's instruction). A bullet it does not recognise as built-in (an owner overlay's wording)
stays too. **It is a stand-in for the design's section 9 prompt**, which will also spend the freed
tokens on what she is in the middle of. To add a variant, add a function to `VARIANTS` and its
name to `config.VARIANTS`.

A first bake-off 2 (design addendum, section 8; native thinking left out after bake-off 1):

```toml
[[candidate]]
name = "orion"
model = "hf.co/bartowski/TheDrummer_Orion-26B-A4B-v1.1-GGUF:IQ4_XS"

[[candidate]]
name = "orion-situation"
model = "hf.co/bartowski/TheDrummer_Orion-26B-A4B-v1.1-GGUF:IQ4_XS"
prompt_variant = "situation"

[[candidate]]
name = "orion-beat"
model = "hf.co/bartowski/TheDrummer_Orion-26B-A4B-v1.1-GGUF:IQ4_XS"
beat = true

[[candidate]]
name = "orion-beat-situation"
model = "hf.co/bartowski/TheDrummer_Orion-26B-A4B-v1.1-GGUF:IQ4_XS"
beat = true
prompt_variant = "situation"

[[candidate]]
name = "cydonia"
model = "hf.co/bartowski/TheDrummer_Cydonia-24B-v4.3-GGUF:IQ4_XS"   # on disk (the fallback)

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
# [[candidate]]
# name = "qwen3-32b-thinking"     # thinking on another model: no flag, its own default
# model = "qwen3:32b"
# think = "default"
# think_budget = 1400
# timeout = 300
```

No cloud candidate: it waits for the owner's explicit yes (privacy; no explicit DMs leave the
machine).

## What it can't tell

- **Turns the live pipeline dropped are not here.** Capture keeps only turns that became a line;
  a draft dropped for good (a silence it accepted, a guard's drop, a `DROP_ON_RETRY` tell) left
  nothing to replay, so the baselines and candidates are compared on the turns that survived.
- **One model's line in another model's conversation.** Every turn's context (the chat she was
  shown, her own earlier lines) was written by the live model; a candidate answers one turn
  inside it. A model that would steer a whole conversation differently is only seen one line
  at a time.
- **The judge is itself a model** (the reflection model), with its own blind spots; the same
  model judges every candidate, so compare candidates with each other, not with the truth.
- **Small samples.** A few dozen lines a family; a difference under about 0.10 AUC is noise
  (`turing/README.md`, Limits), and his own lines are few in some families (Discord channels).
  The retried subset is smaller still and often reads `too_few`.
- **Typing is approximated**: the live turn's mood, drink and time of day are not replayed;
  every candidate and the `first`/`live` baselines are typed the same way, `sent` keeps the real
  typing.
- **Timing is not the model's**: latency and bursts are the pipeline's; the text-only AUC is the
  model comparison, the all-features table the night's view.
- No guard (`[guards]`), retry or owed-answer fallback runs on a candidate's reply: a silence
  is counted as a silence.
- The live beat is not replayed: its lines are never stored, so a candidate answers without
  one unless it asks its own (`beat = true`), whose items are read back from the request.
- An optional reply scorer exists and is not called by a run: `lab.judge.score_lines(reasoner,
  lines)` scores a window's replies 1-5 on the live model (lab/README.md, "The judge"). It
  agrees with a strong grader at rho 0.31 on its test set, below the lab's own bar, so the
  bake-off's tables do not use it.
