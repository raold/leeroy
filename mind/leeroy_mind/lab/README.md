# The conversation lab (`leeroy_mind.lab`)

A nightly loop that measures how real the members' answers to him read, proposes rewordings of
one prompt piece, tests them on the same battery and hands the winner to the learn bandit, which
promotes it live only through the weekly cycle's own gate. Spec: human-likeness design section 11
(`docs/superpowers/specs/2026-11-15-human-likeness-design.md`, local).

```toml
[lab]
enabled = true     # default false: the night's three lab stages (read at mind start)
minutes = 40       # the hard budget of one night's lab (5-180)
pairs = 12         # (script, member) pairs a night, each run once per arm (2-60)
judge = 6          # pairs per arm the blind judge reads (0-60, never more than pairs)
variants = 3       # prompt variants proposed a night (0-5; 0 only measures)
```

An unknown key or a bad value reads the whole table as the defaults with the lab off. Restart the
mind after changing it. `overlays` is a seventh key the sandbox sets in its own copy (below); the
live mind leaves it off.

## A night

Three stages after `turing` (after `forget` without it), so the runner swaps the models:

1. **`lab_propose`** (reflection model). Unit `plan` makes the night's `lab_run`: tonight's piece,
   round-robin over the registry (`talk/pieces.py`), and the seeded plan. The battery
   (`lab/battery/*.toml`, 30 invented owner-style scripts of 1-2 lines: flirt, logistics, a direct
   question, bored small talk, goodbye, pushback, a photo ask, a long silence then return, a
   name-the-raid question, a request to meet) is paired with up to 20 members drawn across the
   stances (a man's is none), seeded by the night; each pair takes one of its script's channels
   (DM, whisper, guild, party). An earlier night's run still `running` ends `failed` (`unfinished`).
   Unit `propose`: one call writes up to `variants` rewordings of the piece from the repo's own
   wording, what it is for, the phrases it must keep and the numbers measured lately (the newest
   control's rubric mean, commonest grade codes and rule-flag rates, and earlier variants of this
   piece with their outcome and change); never persona text, his lines or the owner's overlays.
   Each passes `propose.screen` or is dropped with a code: `shape` (its {placeholders} must be
   exactly the built-in wording's, kept phrases such as "in one short line" or "(nothing)"
   verbatim, one paragraph, no `|`), `length` (within 20% of the built-in wording), `names` (a
   cast, persona or his name the built-in wording lacks, or `@`), `explicit` (an explicit or
   manipulative word it adds: selfies' EXPLICIT_RE, SEXUAL_RE, the lessons' MANIPULATION), `meta`
   (AI, bots, prompts), `same` (the built-in wording or a variant already tried). Kept variants
   are `prompt_variant` rows with a word diff.
2. **`lab`** (live model). Never while a live lease is held or the GPU is paused (`lab_waiting`;
   the run is then ended `unfinished` by the next night). Unit `snapshot` copies the guild into
   `<data dir>/scratch/lab` (the sandbox's copy, `sandbox/README.md`; scratch is never backed
   up) with the sandbox's quiet `mind.toml` plus the lab's own settings: the learn bandit's prompt
   families frozen (a live lab arm never mixes into the control) and `[life] availability = 0`
   (members answer at once: the lab measures words, not timing). A sandbox child
   (`python -m leeroy_mind sandbox --lab --reuse --fast --seed <night>`) then runs every pair,
   the control first; each variant arm restores the same snapshot through `POST /reset` with its
   text as `prompts/lab_<piece>.txt` in the copy (never in the live prompts dir). One unit per
   arm: the transcripts go to `scratch/lab/runs/<run>/<arm>.json` (only the newest run's are
   kept), the rule checks into `lab_score`. A cancel (his login, a lease, Free GPU, the mind
   stopping) kills the child at once.
3. **`lab_grade`** (reflection model). One rubric unit per arm and pair: her answers graded 0-3
   on bake-off 1's scale (0 a bot tell, wrong, empty or out of character; 1 generic or
   sycophantic; 2 specific and in her voice; 3 would pass as her) with a reason code, reading her
   prompt card; an answer she never gave is 0 `empty`. One blind-judge unit for the first `judge`
   pairs of each arm (`turing/judge.py`'s own fixed prompt, names masked; the times are synthetic,
   his reading and her typing at 4 characters a second, since `--fast` skips them). Then
   `finish`: promotion and the run's summary.

**Rule checks** (`lab/rules.py`, model-free, per answer): `silent`; `unanswered` (his line had an
`ask` and her first sentence holds no yes/no, number or time, name, or the asked noun); `wordy`
(more than 3x his words + 15); `repeat` (3+ content words and a Jaccard of 0.35 with an earlier
answer of hers in the conversation, not counting his current line's words); `rewritten` (a guard
rewrote the draft); `echo` (the live echo guard's rule); `stock` (the guards' stock list).

**The budget** is hard: `minutes` over all three stages, kept in `lab_run.spent` across resumes.
The battery may use 60% of it; a pair is never begun past that, and the stage's work is cancelled
when it is gone (the child killed, the arm in flight kept as far as it got). Grading takes the
rest. `finish` always runs; arms the budget cut short are settled `budget`.

**Promotion** (`lab/promote.py`): a variant wins when its rubric mean beats the control's by 0.15
or more over the answers both arms had graded (8 or more), its piece was in her prompt at least
once, and no rule check regressed (no code has 2 or more, and 10% or more of the paired answers,
more flagged answers than the control's: one more wordy answer in 30 is the unseeded model's
noise). The best winner becomes an **active arm** of the learn bandit's family for its piece,
`prompt.<piece>` (id `lab.<piece>.v<variant>`; its origin column says `coach` because migration
0034's CHECK allows only builtin and coach, and the id and `prompt_variant.arm_id` mark it the
lab's). Every other variant is retired with its outcome: `beaten` (beat the control, not the
best), `lost`, `regressed`, `untested` (never in a prompt), `too_few`, `budget`, `frozen` (a global
learning freeze or a frozen family promotes nothing). Every variant keeps its scoresheet (numbers
only). Logs: `lab_plan`, `lab_proposed ... kept= refused= codes=`, `lab_arm ... answered= applied=
complete=`, `lab_done ... promoted= control= best_delta= cut=`, `lab_waiting`, `lab_budget`.

## Live: a promoted arm in the learn bandit

`learn.arms`: one family per piece, `prompt.<piece>`, with params `{"piece", "variant"}`. These
families are never in `arms.controls()`: a turn draws one only when it may render the piece and a
lab arm for it is active (`lab/live.py`, `bandit.choose_pieces`), before its prompt is composed
(Thompson sampling with the same maturity rule and the same per-member exploration budget as the
other families); `learn.apply` merges the draw into the turn's selection. When the composition is
done, a family stays in the recipe (and so in its reward credit) only when its piece was
rendered. Without lab arms every recipe, credit and choice is byte-for-byte as before. Promotion
to the cycle's `promoted` status is the weekly cycle's gate (P(beats control) > 0.9 on 30+ rewarded
uses, no guard regression, no veto); its retirement rule applies too. The Lab's arm buttons
(accept, reject, retire) and freezes (`frozen_families` takes `prompt.<piece>`) work on them, and
`learn rollback` restores their statuses like any arm's. Log: `lab_piece: guid= piece= source=arm`.

## The pieces (`talk/pieces.py`)

`reply_human`, `continue`, `goodnight` (INSTRUCTIONS), `situation_reply_human`
(SITUATION_INSTRUCTIONS), `discord_dm` (DISCORD_DM), `romance_hook` (the romance lead's closing
sentence, SPECIFIC: the attitude sentences differ by stance and forwardness, so the hook is the one
sentence every romance lead shares), `beat_ask` (the beat's first sentence, MARK). A variant reaches
a prompt only at render time, by name, inside `pieces.using` while a turn composes; outside it the
built-in wording is used and nothing is marked, so the golden prompts and template traces are
unchanged. A piece whose `lab_<piece>.txt` name exists in the live prompts dir is passed over by
the round-robin (its file is never read).

## Commands

From `mind/` with `--data-dir "$D"`:

```bash
python -m leeroy_mind lab report [--night N] [--runs K] [--json]   # read only
python -m leeroy_mind lab run --dry-run [--pairs N] [--scripts a,b] [--members g,h]
python -m leeroy_mind lab run [--grade-model reflection|live] [--pairs N] [--variants K]
    [--minutes M] [--scripts ...] [--members ...]
```

`report` and the dry run never write or migrate. `run` holds `mind.lock` (the mind stopped) and
runs the loop once as a `cli` run: it swaps the reflection and live models as the night does;
`--grade-model live` proposes and grades on the live model and swaps nothing, for a run on a copy
of the data dir beside a running live mind. The console's Lab tab has a "Conversation lab" panel
(the nights' control means by kind, rule-flag rates, the judge, and the variants tried and
promoted with their word diffs). Health notes a night run that has not finished in 3 nights with
the switch on (`conversation-lab`).

## Privacy

The battery is invented wording; no line of his and no persona text is in the repo. The
transcripts live only in `scratch/lab/runs` (newest run only); `lab_score` keeps no words;
`prompt_variant` keeps prompt wording only. The owner's overlay files are copied into the
sandbox (as the sandbox always does) and never read as variants or written.

## Not covered

- A long silence cannot be waited out: those scripts say it in his words.
- The live model's sampling is not seeded, so arms differ by chance too; a 0.15 rubric margin on
  8+ paired answers and the weekly cycle's own gate stand between a lucky night and live use.
- With `[photos]` off in the sandbox, a photo ask is answered in words only.
