# The conversation lab (`leeroy_mind.lab`)

A nightly loop that measures how real the members' answers to him read, proposes rewordings of
one prompt piece and tests them on the same battery. **It only measures by default**
(`promote = false`): its judge does not yet agree well enough with a strong grader (see "The
judge"), so the changes it reports are indicative and it proposes nothing to the learn bandit.
With `promote = true` the winner goes to the bandit, which promotes it live only through the
weekly cycle's own gate. Spec: human-likeness design section 11
(`docs/superpowers/specs/2026-11-15-human-likeness-design.md`, local).

```toml
[lab]
enabled = true     # default false: the night's three lab stages (read at mind start)
minutes = 40       # the hard budget of one night's lab (5-180)
pairs = 12         # (script, member) pairs a night, each run once per arm (2-60)
judge = 6          # pairs per arm the blind judge reads (0-60, never more than pairs)
variants = 3       # prompt variants proposed a night (0-5; 0 only measures)
grader = "rubric"  # what grades her answers: "tells" (lab/judge.py, live model) or "rubric"
promote = false    # true: a winning variant becomes an arm of the learn bandit
```

An unknown key or a bad value reads the whole table as the defaults with the lab off. Restart the
mind after changing it. `overlays` is one more key the sandbox sets in its own copy (below); the
live mind leaves it off. Code from before `grader` and `promote` reads a table that has them as
off (an unknown key): remove both before rolling back.

## A night

Four stages after `turing` (after `forget` without it), so the runner swaps the models:

1. **`lab_propose`** (reflection model). Unit `plan` makes the night's `lab_run`: tonight's piece,
   round-robin over the registry (`talk/pieces.py`), and the seeded plan. The round-robin passes
   over a piece this mind's settings never render (`pieces.unrendered`: with `[talk] situation`
   on a reply reads `situation_reply_human`, so `reply_human` and `continue` could only end
   `untested`; `[romance] realism` picks one of the two romance pieces), and goes first to a
   piece with a hand-written variant waiting (below). The battery
   (`lab/battery/*.toml`, 30 invented owner-style scripts of 1-2 lines: flirt, logistics, a direct
   question, bored small talk, goodbye, pushback, a photo ask, a long silence then return, a
   name-the-raid question, a request to meet) is paired with up to 20 members drawn across the
   stances (a man's is none), seeded by the night; each pair takes one of its script's channels
   (DM, whisper, guild, party). An earlier night's run still `running` ends `failed` (`unfinished`).
   Unit `propose`: the piece's waiting hand-written variants take the first of the `variants`
   slots; for the slots left, one call writes rewordings of the piece from the repo's own
   wording, what it is for, its placeholders, the phrases it must keep (and why: other code finds
   them by their words), a valid variant of this piece for the form (`propose.EXAMPLES`, never
   tried itself) and the numbers measured lately (the newest control's rubric mean, commonest
   grade codes and rule-flag rates, and earlier variants of this piece with their outcome and
   change); never persona text, his lines or the owner's overlays. The prompt ends with a
   checklist of the exact placeholders and kept phrases. Each passes `propose.screen` or is
   dropped with a code: `placeholders` (its {placeholders} must be exactly the built-in
   wording's, and a pronoun one is never made possessive by hand: "{they}'s" reads "he's"), `kept` (a kept phrase such as "in one short line" or "(nothing)" is not there
   verbatim), `shape` (not one paragraph of text, a `|`, over 1200 characters), `length` (more
   than 20% longer or 40% shorter than the built-in wording, or 30 characters either way for a
   short piece), `names` (a cast, persona or his name the built-in wording lacks, or `@`),
   `explicit` (an explicit or manipulative word it adds: selfies' EXPLICIT_RE, SEXUAL_RE, the
   lessons' MANIPULATION), `meta` (AI, bots, prompts), `same` (the built-in wording, the example
   or a variant already tried). When the screen refused some and slots are left, the model is
   asked once more with what was wrong with each ("it lost the locked phrase ..."). Kept variants
   are `prompt_variant` rows with a word diff.

   The first live night (2026-11-17) kept none of six proposals: each had reworded the kept
   phrase ("in one short line" became "Keep it brief."), the old prompt named it once mid-list
   and then said "keep every part of it, reworded", and the one code `shape` hid which rule it
   was. The same night's piece, `reply_human`, was not rendered either (`[talk] situation` was
   on), so its variants could only have ended `untested`.
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
3. **`lab_score`** (live model; only with `grader = "tells"`). One unit per arm and pair: the
   lab judge's 1-5 score of each of her answers (lab/judge.py, 4 short calls an answer at
   temperature 0, `think` off, a schema each; the same seed for the same pair and turn in every
   arm; her answers of the pair before as context). An answer she never gave is 1 without a
   call. Like the battery it waits while a live lease is held or the GPU is paused. The scores
   and the calls they took go into `lab_score` (`score`, `score_calls`, migration
   `*_lab_judge.sql`). Log: `lab_scored ... units= done= calls= seconds= cut=`.
4. **`lab_grade`** (reflection model). With `grader = "rubric"`, one rubric unit per arm and
   pair: her answers graded 0-3 on bake-off 1's scale (0 a bot tell, wrong, empty or out of character; 1 generic or
   sycophantic; 2 specific and in her voice; 3 would pass as her) with a reason code, reading her
   prompt card; an answer she never gave is 0 `empty`. One blind-judge unit for the first `judge`
   pairs of each arm (`turing/judge.py`'s own fixed prompt, names masked; the times are synthetic,
   his reading and her typing at 4 characters a second, since `--fast` skips them). Then
   `finish`: the comparison and the run's summary. A run keeps the grader and the `promote` it
   was planned with (in its plan), so a resumed run is graded the way it began; a run from
   before this is the rubric's and may promote.

**Rule checks** (`lab/rules.py`, model-free, per answer): `silent`; `unanswered` (his line had an
`ask` and her first sentence holds no yes/no, number or time, name, or the asked noun); `wordy`
(more than 3x his words + 15); `repeat` (3+ content words and a Jaccard of 0.35 with an earlier
answer of hers in the conversation, not counting his current line's words); `rewritten` (a guard
rewrote the draft); `echo` (the live echo guard's rule); `stock` (the guards' stock list).

**The budget** is hard: `minutes` over all three stages, kept in `lab_run.spent` across resumes.
The battery may use 60% of it; a pair is never begun past that, and the stage's work is cancelled
when it is gone (the child killed, the arm in flight kept as far as it got). Grading takes the
rest. `finish` always runs; arms the budget cut short are settled `budget`.

**The comparison and promotion** (`lab/promote.py`): a variant wins when its mean grade beats the
control's by the grader's margin (0.15 of the rubric's 0-3; 0.1 of the judge's score, whose
shrunk weights keep it between about 2.2 and 3.5) over the answers both arms had graded (8 or
more), its piece was in her prompt at least
once, and no rule check regressed (no code has 2 or more, and 10% or more of the paired answers,
more flagged answers than the control's: one more wordy answer in 30 is the unseeded model's
noise). The best winner becomes an **active arm** of the learn bandit's family for its piece,
`prompt.<piece>` (id `lab.<piece>.v<variant>`; its origin column says `coach` because migration
0034's CHECK allows only builtin and coach, and the id and `prompt_variant.arm_id` mark it the
lab's). Every other variant is retired with its outcome: `beaten` (beat the control, not the
best), `lost`, `regressed`, `untested` (never in a prompt), `too_few`, `budget`, `frozen` (a global
learning freeze or a frozen family promotes nothing), and with `promote = false` the variant
that would have been promoted is retired `measure_only`: no arm is made and the weekly gate
never sees it. Every variant keeps its scoresheet (numbers
only). Logs: `lab_plan ... authored=`, `lab_proposed ... kept= authored= asks= refused= codes=`, `lab_arm ... answered= applied=
complete=`, `lab_scored`, `lab_done ... promoted= control= best_delta= cut= grader= promote=`,
`lab_waiting`, `lab_budget`.

## The judge (`lab/judge.py`, `lab/features.py`, `lab/gold.py`, `lab/judge_eval.py`)

Measured on 2026-11-17 against a strong grader's 1-5 grades ("would a real person have texted
this", with defect letters) of 390 replies in 21 self-play conversations, split once by
conversation with a fixed seed (`gold.SPLIT_SEED`) into train 234, dev 74 and test 82. The gold
is private and local; nothing of it is in the repo. `lab judge-eval --gold DIR` recomputes every
number below on any directory of gold conversations (format: `lab/gold.py`).

**What the old rubric measures.** Asked of the live model exactly as the lab asks it, over all
390 replies: Spearman +0.11 (95% interval +0.01..+0.21) with the gold; by part +0.03, +0.19,
+0.25, and three more runs on the test part gave +0.17, +0.17 and +0.27 (it is unseeded at
temperature 0.2, so its number moves by 0.1 from run to run). 61% of replies get a 3 and 28% a
2. Its means by her length rise where the gold's fall (10 words or fewer 2.31, 11-25 2.52,
26-50 2.44, over 50 2.57; the gold 3.41, 3.39, 3.00, 3.29: rho with her word count +0.16
against the gold's -0.14). It is blind to every defect the grader marks: its rho with each
defect letter is within 0.1 of zero but for stock phrases (-0.17, 13 replies). Its only codes
with signal are `out_of_character` (17 replies, gold mean 2.47) and `wrong` (4, 2.50);
`in_voice`, `passes`, `specific` and `generic` sit at the gold's overall mean (3.1-3.4).

**What the gold grades.** A linear model of the grader's own defect letters reproduces her
score at rho 0.86 (train) and 0.79 (dev). The heavy ones are the conversation got wrong (-1.15
a reply, 92 replies), an invented fact about the game, him or their past (-1.18, 41), an oddity
of shape (-0.85), a claim to have met (-0.76) and a contradiction of who she is (-0.64). The
surface tells are the light ones: repeating herself -0.44, a dodged question -0.34, echo -0.32,
length -0.18.

**The designs, on the test part** (82 replies of 6 conversations, the shipped command, the live
model at temperature 0 with `think` off; arm agreement: which of two resampled arms of 24
replies is better, over the draws whose gold means differ by 0.15 or more):

| judge | rho | 95% interval | arm agreement | calls a reply | model seconds a reply |
|---|---|---|---|---|---|
| rubric (old; three runs) | +0.17, +0.17, +0.27 | -0.04..+0.45 | 75%, 75%, 77% | 0.5 | 1.4 |
| features (no model) | +0.16 | -0.05..+0.37 | 64% | 0 | 0 |
| fewshot (1-5 with the word counts shown) | +0.21 | +0.00..+0.41 | 59% | 1 | 1.1 |
| pairwise (against six invented anchors) | +0.03 | -0.19..+0.24 | 41% | 12 | 11.7 |
| checklist (8 yes/no tells + length) | +0.29 | +0.08..+0.49 | 72% | 7.9 | 8.0 |
| **tells** (the combination) | **+0.31** | +0.11..+0.50 | 71% | 4 | 4.8 |

Over all 390 replies (train and dev chose the design and fit its weights, so only the rubric's
row is out of sample there): rubric +0.11 and 55%, features +0.14 and 57%, fewshot +0.23 and
59%, checklist +0.33 and 66%, tells +0.33 (+0.24..+0.42) and 68%; tells by part +0.33, +0.29,
+0.35. A second run of the same test replies gave tells +0.35 and checklist +0.35: Ollama's
parallel decoding is not bit-exact even at temperature 0 with a seed, so a rho here is good to
about 0.03, and the test part is small (the intervals are wide).

**The bar is not met.** A judge becomes the default and the lab may promote at rho 0.35 or
more and arm agreement 70% or more on the test part (`judge_eval.PASS_RHO`, `PASS_ARM`). The
combination reaches +0.31 and 71%: steadier than the rubric across the parts and with the
right sign for length, but not clear of the bar and not clear of the rubric on the test part.
So `grader` stays `"rubric"` by default, `promote` is false, and every delta the lab shows is
indicative. Setting `grader = "tells"` gives the steadier numbers (and the same-line
preference below) at 4 calls an answer on the live model.

**Why.** The local model does see the surface tells it is asked about (its `echo` tell against
the grader's echo letter +0.38, `repeats` +0.36, `met` +0.25; features' dodged question +0.27,
length +0.38), but those are the light part of the grade. Of the heavy part it sees little: its
`context` tell reaches +0.07 with the grader's context letter, `drifts` +0.14, `invented` +0.14
with the invented-fact letter, and `persona` (reading her card) +0.04 with the persona letter
(it fires on 3% of replies). Those need what the strong grader had: the game's facts, her whole
card, the whole conversation.

**The tells mode** (`judge.LAB_MODE`, what `grader = "tells"` scores with; 4 calls a reply):
one structured grade (the main problem from a fixed list, then 1-5, reading the six turns
before and her last five messages, with both word counts shown) and three yes/no tells, each
with the words that show it: the conversation got wrong (reading eight turns), a jump off the
subject, a claim to have met. The score is a ridge-shrunk linear model of the four
(`judge_weights.json`: numbers only, versioned, fit on train and dev), so it runs between about
2.2 and 3.5; a reply she never gave is 1 without a call. Every call is schema-constrained,
`think` off, temperature 0 with a seed, 96 tokens at most; nothing of the chat or a card is
logged. The other modes (`features`, `checklist`, `fewshot`, `pairwise`) and the four tells
that did not earn a place (`invented`, `odd`, `persona`, and the checklist's own) stay for
`judge-eval` and a refit (`judge-eval --fit` prints new weights from the train and dev parts).

**Same-line preference** (`judge.prefer`). With the tells grader the score stage also shows the
judge each variant's answer and the control's answer to the same line, both ways round (A/B and
B/A; a judge that follows the position counts as no preference), 2 calls a line, and records
the variant's share in its scoresheet (`prefer`). On a second gold set (the same scripted lines
answered under two code versions, 39 pairs whose defect counts differ) it picked the answer
with fewer defects in 72% of the pairs it decided (23 of 32; 7 ties), the tells score in 62%,
and it had the better arm right in both arm comparisons; "the shorter answer" alone was right
in 77% of those pairs. Between two replies to different lines it is right 60% of the time (64%
when the gold differs by two grades), and scored against fixed anchors it is the worst design
above. It is reported, never decided on.

`judge.score_lines(reasoner, lines)` scores a window of `turing.corpus.Line` (the type the
turing judge and the bake-off read) reply by reply: an optional scorer for both, called by
neither's runs while the judge is below the bar.

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

## Hand-written variants (`lab/authored.py`)

The reflection model's rewordings are mostly a synonym swapped. A variant written by someone who
has read the replies can aim at one weakness (echoing his line, answering longer than he wrote,
a polished tone, nothing of her own in it), and the lab tests it like its own:

```bash
python -m leeroy_mind lab add-variant --piece situation_reply_human --file my-variant.txt [--name N]
python -m leeroy_mind lab variants        # each file: waiting, tried, or refused with its code
```

`add-variant` screens the text and copies it to `<data dir>/lab/variants/<piece>/<name>.txt`
(one variant a file, folded to one paragraph); it writes that one file, so it works beside a
running mind, and the folder may also be filled by hand. The screen is the proposer's
(`propose.screen`): the same placeholders, kept phrases, names, explicit and meta checks. Only
the length band is its own: any length up to half as long again as the built-in wording, or 120
characters longer for a short piece. A night's plan goes first to a piece with a file waiting
(among several, round-robin), and its propose unit takes the waiting files in name order before
it asks the model for the slots left, so with `variants = 3` and four files the fourth waits for
that piece's next night. A file whose text is a `prompt_variant` row is done (`tried`); the
files stay where they are. A hand-written variant is marked by meta
`lab:authored:<variant id>` (its file name) and `lab report` says so; from there it is promoted,
retired and credited exactly like the model's. A piece the settings never render is not picked
for its files (`lab variants` and `add-variant` say so); `lab run --piece` names it anyway.

The files are prompt wording only: no persona text, none of his lines, no names. The folder is
in the data dir, so it is in the guild backup and never in the repo.

## The pieces (`talk/pieces.py`)

`reply_human`, `continue`, `goodnight` (INSTRUCTIONS), `situation_reply_human`
(SITUATION_INSTRUCTIONS), `discord_dm` (DISCORD_DM), `romance_hook` (the romance lead's closing
sentence, SPECIFIC, with `[romance] realism` off), `romance_lead` (REALISM_LEAD, its place with
`[romance] realism` on; the attitude sentences differ by stance and forwardness, so the lead's
last sentence is the one every romance text shares), `beat_ask` (the beat's first sentence, MARK).
A piece the night's turns never render (the other romance one, situation mode off) ends its
variants `untested`. A variant reaches
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
    [--minutes M] [--scripts ...] [--members ...] [--piece P]
python -m leeroy_mind lab variants
python -m leeroy_mind lab add-variant --piece P --file F [--name N]
python -m leeroy_mind lab judge-eval --gold DIR [--part test|dev|train|all] [--judges a,b]
    [--seed N] [--parallel 1-4] [--json] [--fit]
```

`report`, `variants`, `judge-eval` and the dry run never write or migrate; `add-variant` writes its one file.
`--piece` names the piece in place of the round-robin (a piece the settings never render ends
its variants `untested`). `run` holds `mind.lock` (the mind stopped) and
runs the loop once as a `cli` run: it swaps the reflection and live models as the night does;
`--grade-model live` proposes and grades on the live model and swaps nothing, for a run on a copy
of the data dir beside a running live mind. The console's Lab tab has a "Conversation lab" panel
(the nights' control means by kind, rule-flag rates, the judge, and the variants tried and
promoted with their word diffs; a night that only measured says "measure only", a variant ahead
of its control reads "ahead of the control (indicative; not proposed)", and the panel says the
changes are indicative). Health notes a night run that has not finished in 3 nights with
the switch on (`conversation-lab`).

## Privacy

The battery is invented wording; no line of his and no persona text is in the repo. The judge's
few-shot examples and anchors are invented too, its weights are numbers, and gold conversations
stay wherever the operator keeps them: `judge-eval` reads them and prints numbers only. The
transcripts live only in `scratch/lab/runs` (newest run only); `lab_score` keeps no words;
`prompt_variant` keeps prompt wording only. The owner's overlay files are copied into the
sandbox (as the sandbox always does) and never read as variants or written.

## Not covered

- With `[talk] shape` on, a DM renders `discord_dm` only for a drawn length over 45 words (the
  shape's own lines word the shorter ones and are not pieces), and for a woman with a romance
  stance the owner's whisper overlay follows the reply instruction, so it, not the piece, is
  the last thing she reads.

- A long silence cannot be waited out: those scripts say it in his words.
- The live model's sampling is not seeded, so arms differ by chance too; the margin on 8+ paired
  answers and the weekly cycle's own gate stand between a lucky night and live use, and with
  `promote = false` nothing reaches live use at all.
- The judge cannot see what the strong grader marks down hardest: a wrong game fact, a detail
  that contradicts who she is, who said what many turns ago ("The judge").
- With `[photos]` off in the sandbox, a photo ask is answered in words only.
