# The Turing judge (`leeroy_mind.turing`)

How distinguishable the guild's members are from a human, with the owner's own lines as the
human baseline. It comes first so the later realism work (presence, intent, emotion, reply
shape) can be measured against a "before". Two parts, one night stage, one CLI, one Lab panel.

## Switch

```toml
[turing]
enabled = true   # the default; false: no stage, no writes
windows = 8      # blind-judge windows a night, 0-60 (0: stylometry only)
days = 14        # the stylometric window, 1-90 days back from the night's end
```

Read once at the mind's start (and by `nightly run`). A missing, invalid or unknown key means
off with the defaults. Restart the mind after changing it. Migration `*_turing.sql` adds
`turing_night` and `turing_judgement`; run `db migrate` with the mind stopped first.

`windows` stays at 8 or less until the judge has run on the real model: health FAILs a stage with
10 failed units, so a model that cannot answer the schema would FAIL every night at 10 or more.

## Channel families

| Family | Lines |
|---|---|
| `game` | guild, officer, party, raid, say and yell chat (`chat_line`); a conversation is a channel within one of his sessions, and for say and yell one place too. Party and raid lines count only as the console's feed shows them: in a session, his own or from a member in the line's audience (his group) |
| `private` | his whispers with a member (`chat_line`) and his Discord DMs (`discord_message`) |
| `discord` | the Discord server's channels and group DMs (`discord_message`) |

His lines are origin `human` from his guid; members' are origin `mind`. Left out: spoken lines
(voice-room lines, whispers the voice service voiced (`voice_line`), his push-to-talk transcribed
into a whisper (`voice_heard`) and Discord voice notes), addon pastes (Recount, RBS, flask checks),
deleted and bot messages, whispers between two members (never his to see) and `chat_line`'s
mirror of Discord rows (the Discord store is the source for those).

**Group DMs** (`gdm:<id>`) count in the `discord` family. He reads every group DM, and nothing
from this package reaches a member: the stylometry keeps numbers and fixed labels only, and the
blind judge keeps no phrase from a group DM (below).

## A. Stylometric distinguishability (no model, deterministic)

Per line (`features.py`): words, apostrophes in contractions (`i'm`, `don't`) and contractions
without one (`im`, `dont`), all-lowercase, a leading capital, end punctuation (full stop,
question mark, `!`, ellipsis), full stops inside the line (fragment chains), commas, chat
shorthand (`u`, `idk`, `rn`), laughter, emoji, emoticons, questions, lines about "you", lines
naming someone, seconds since the line before in the conversation, hour of day (as sine and
cosine), and burst position (follows the speaker's own line; part of a burst of two or more).

Per family (`stylo.py`): his lines against an equal, seeded sample of the members' (at most
1,500 each; at least 30 of each or the family reads `too_few`). No member is more than 5% of the
members' sample, or more than an equal share when fewer than 20 members spoke, so one chatty
member cannot set the tells; when the capped lines fall short, both sides shrink to fit. Then an
L2 logistic regression in
plain Python (`classify.py`: the mind's venv has no numpy), grouped 5-fold cross-validation
repeated over 3 fold draws (a conversation stretch, cut at 30 minutes of silence, never sits in
two folds; folds keep both classes evenly spread), the folds' held-out AUCs averaged, and an
approximate interval from a cluster bootstrap over those stretches (see Limits). AUC 0.5 means the members read like him; 1.0
means they are always told apart. The folds' AUCs are averaged rather than their scores pooled:
pooling carries each fold's intercept, which leans against that fold's own class balance and
drags a no-signal AUC under 0.5. The top features are the ones
whose single-feature AUC is furthest from 0.5, shown in words with his value and the members':
"contractions with an apostrophe (i'm, don't): him 6%, members 51%".

## B. The blind judge (night, reflection model)

`judge.py`: windows of 8-12 consecutive lines from one conversation (no 15-minute gap inside),
alternating windows with his lines and windows with members' only, each from a family drawn
evenly among those that have one, the newest 3 days first. A line shown to the judge once is
never shown again. Every name is masked, at any length and with its ending as typed ("arkons",
"Arkon's"): speakers as A, B, C, anyone else as `[name]`. That covers character names, his
nickname, character name and real name, and the personas' own real and first names. Times are
shown as minutes:seconds from the first line. The reflection model answers a fixed JSON schema:
per speaker, human or AI, and up to three tells, each a code (`TELLS`) and a phrase of its own
(at most 100 characters). Tells are counted by code: `caught` (a member it rightly called AI),
`missed` (a member it called human) and `him`.

The score is **balanced accuracy**: the mean of the share of members it called AI and the share
of times it called him human, so 0.50 is chance. Members are most of the speakers (at least
three in four), so a judge that says "AI" to everyone would score 0.75 or more in raw accuracy;
raw accuracy is kept in the data and shown only beside that comparison.

A phrase is kept only when every line of the window is on a public channel; never from a
whisper, a DM, a group DM, officer chat or an officers' Discord room. It is cut to 80
characters, with names masked again by the same pattern as the window. The judge's prompt is fixed here (no overlay, no persona,
none of the night's generating invariants, which forbid talk of bots); nothing it says reaches a
member's prompt.

## The night stage

`turing`, reflection model, after `forget` (the last base reflection stage, so no model swap).
Units, each one transaction: `stylo` (part A, read on a read-only connection in a worker
thread), `plan` (the windows, saved in meta `turing:plan:<night>` so a resumed run judges the
same ones), then `w1`..`wN` (one JSON call each). The night's budget counts one JSON call per
window left and may lower `windows` (degrade step `turing`, after `snaps`, never below 4). A
failed judge call is that unit's failure and is tried again on a resumed run; the owner's GPU
pause stops the night like any stage. Logs: `turing_attached`, `turing_stylo` (family, status,
AUC, counts), `turing_plan` (windows), `turing_judged` (unit, family, status, counts); never
text.

## Reading it

- `python -m leeroy_mind turing report [--days 14] [--json] --data-dir D`: the stored nights per
  family (AUC with its approximate interval, the judge's balanced accuracy), the latest style
  tells in words, and the
  judge's top caught tells with up to three phrases each. Read only; an older database reads
  "Not set up yet".
- `python -m leeroy_mind turing run [--days 14] [--json] --data-dir D`: part A now, on mind.db
  opened read-only. No model, nothing written. Safe while the mind runs.
- `python -m leeroy_mind turing texture [--until YYYY-MM-DD] [--days 90] [--json] --data-dir D`
  (`texture.py`): the typing texture (`talk/style.py`, `[talk] texture`) replayed on the stored
  game and private lines. Members' lines are typed again with the current `style.Calibration`
  (their own abbreviations put back into words first); his are left alone. Per family it prints
  the AUC over the words alone and each mechanical feature's rate (apostrophes, commas, fragment
  chains, last full stops, shorthand), his beside the members' as stored and as typed again, on
  all days and on two halves split by day (fit on one, confirm on the other), and how the habits
  spread over the members. `--until` is the day the texture went on (2026-11-15 on the live
  install): a line already typed with it would get it twice. It cannot replay case, or how the
  model's drafts change once textured lines are its chat history. Read only, numbers only.
- The console's Lab tab, panel **Turing**: per family a trend chart (solid: style AUC; dotted:
  the judge's balanced accuracy; dashed: 0.50, chance for both), the latest headline, the style
  tells, what the judge caught and the nights.

## Limits

- His lines are few in some families (Discord channels: a handful), which reads `too_few` until
  he writes there. The game family depends on how much he played inside the window.
- Lines inside a conversation are not independent; the bootstrap resamples whole conversation
  stretches. It resamples the held-out lines over the fitted folds and does not refit, so the
  printed interval is approximate and too narrow: near 0.5 it covers about 85%, not 95%. On
  invented data with no signal and 120-170 lines a side, one measurement's AUC spreads about 0.03
  to 0.05 (one standard deviation) around 0.50, and the difference between two measurements of
  separate windows spreads more. **A change under about 0.10 between two nights or windows is
  within noise.**
- The window is `days` long (14), so a change to how members type shows only as fast as new
  lines replace old ones in a family: after the typing texture went on (2026-11-15) the next
  night's game and private windows held about 1% textured member lines, because he had not
  played since. Compare windows that lie wholly after a change, or replay it (`turing texture`).
- Latency and bursts are partly properties of the talk pipeline's timing, not the model's
  writing; they are real tells all the same, since a human reader sees them.
- The judge is the same reflection model every night and sees a small sample; read its accuracy
  as a trend over a week or more, not night to night.
- An optional reply scorer exists and is not part of a night: `lab.judge.score_lines(reasoner,
  lines)` scores the replies of a window of `corpus.Line` 1-5 on the live model (lab/README.md,
  "The judge"). It is below the lab's bar for agreement with a strong grader, so nothing here
  reads it yet.
