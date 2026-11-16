# Her day (`leeroy_mind.life`)

A member's day outside the game: a deterministic plan of her local day built from her persona,
where she is at any minute, and how closely she is watching the game and Discord. Section 1 of
the human-likeness design ("Her day: presence and availability").

**Inert for now.** Nothing in talk, Discord or the night reads this package yet; later tasks wire
it in behind `[life] availability` (0-10, default 0: always present, today's behaviour). There is
no switch to set, no migration and no native change.

Everything here is pure: no clock, no database, no model, no log. The same persona and date
always give the same plan. Plans are immutable and cached (`PLAN_CACHE`, 1024: two a member, today's
and yesterday's for a late evening), keyed by the schedule facts (guid, job, rhythm, household,
life thread, night_owl) and the date, so an edited persona gets a fresh plan. Draws use `life.seed` (`unit(*parts)` for one number in [0, 1),
`seeded(what, night, guid, minute, extra)` for a generator of its own), never the talk engine's
shared rng, so a switch that is off draws nothing and moves no other draw.

## Her day plan (`life/day.py`)

`day_plan(p, day, *, event=None) -> DayPlan`: every minute of her local date (0-1439) in exactly
one block: `asleep`, `work`, `commute`, `home_duty`, `gym`, `out`, `free`, `gaming`.
`DayPlan.spans` are contiguous `Span(start, end, block)` from 0 to 1440 (end exclusive);
`DayPlan.block_at(minute)`, `DayPlan.workday`. `block_at(p, her_local)` gives the block and the
day's event at a local wall-clock time. US members' local time is US Eastern and UK members'
is UK time, as Discord presence has it (`discord.presence.local_time`).

### Job shape

From `job + " " + rhythm`, case-folded; the first word-bounded match wins, else `day`:

| Shape | Pattern |
|---|---|
| `nights` | `night[- ]?shifts?\|third[- ]shifts?\|work(?:s\|ing)?(?: on)? nights\|(?:i'm\|she's\|is\|was\|been) on nights\|overnights?\|graveyard` ("raid nights", "late nights" and "plays on nights" are no night shift) |
| `rotating` | `\d+\s*on\s*,?\s*\d+\s*off\|\d+\s*(?:days\|weeks)\s*on\|rotating\|rota\|12[- ]hour\|on call` |
| `early` | `early shifts?\|early starts?\|[4-6]\s*a\.?m\.?\s*starts?\|starts? at [4-6]` |
| `late` | `late shifts?\|evening shifts?\|swing shifts?\|afternoon shifts?` |
| `student` | `student(?! loans?)\|studying\|(?:attends?\|attending\|has\|skips?\|skipping\|goes to\|going to) lectures?\|(?:takes?\|taking\|attends?\|attending\|night\|evening) classes\|(?:back\|off\|goes\|going\|still\|is\|she's\|been\|starts?\|starting) (?:to\|at\|in) (?:uni\|university\|college)\|(?:uni\|college) (?:classes\|courses?\|lectures?\|coursework\|assignments?)\|doing an? (?:[\w-]+ ){0,2}degree`, unless the text also says `teach\w*\|tutor\w*\|lecturer\|professor\|instructor\|gives? lectures?\|works? (?:at\|for) (?:the \|a )?(?:uni\|university\|college)`: only what she does, so an employer named "... College", a degree she holds, a job at a university or a class she teaches is no study |
| `remote` | `wfh\|works? from home\|from home\|freelanc\w*\|self[- ]employed` |
| `home` | `stay[- ]at[- ]home\|unemployed\|between jobs\|retired` |

**Kids** (home duty) means a dependent child at home. A household that says "no kids", "no
children", child-free, "empty nest", doesn't/does not/don't/do not/never/without (up to two words)
kids or children, or that she lives alone (unless "alone with her/his/their ...") has none.
Otherwise `household` is split into clauses (at `, ; . /` and "and"; brackets stay inside their
clause); a clause after a comma that starts with a number or one of the "gone" words below belongs
to the child before it ("a son, 19, at college"). One clause must name kids, sons, daughters, a
baby, toddlers, children, twins or stepkids without putting them elsewhere or grown up (grown,
grown-up, adult, away, at or in college/uni/university, with (the/their/her/his)
dad/ex/mum/mom/father/mother, visits, visiting, moved out) or not born yet (on the way,
expecting, pregnant, trying for: only in the child's own clause, so "three kids, expecting a
fourth" still has kids).

### Hours (her local time)

| Shape | Workdays | Work | Commute | Wake on a workday |
|---|---|---|---|---|
| `day` | Mon-Fri | 09:00-17:00 | 08:15-09:00, 17:00-17:45 | 07:30 |
| `early` | Mon-Fri | 06:00-14:30 | 05:30-06:00, 14:30-15:00 | 04:45 |
| `late` | Mon-Fri | 14:00-22:30 | 13:30-14:00, 22:30-23:00 | 10:00 |
| `nights` | shifts start on weekdays `(k + guid) % 7`, k = 0-3 | 22:00-06:00 next day | 21:30-22:00, 06:00-06:30 | asleep 07:00-14:30 after a shift |
| `rotating` | `(date.toordinal() + guid) % 8 < 4` | 07:00-19:00 | 06:30-07:00, 19:00-19:30 | 05:45 |
| `student` | Mon-Fri | 10:00-15:00 | none | 09:00 |
| `remote` | Mon-Fri | 09:30-17:00 | none | 08:30 |
| `home` | none | none | none | the day-off rule |

**Sleep.** Before a workday she sleeps 7.5 h (6.5 h as a night owl) ending at the wake time;
the part before midnight is in the previous date's plan. A day off: a night owl sleeps
03:00-11:00, anyone else 00:00-08:30; with kids the day-off wake is 07:00 (owl 09:30). A night
shift worker sleeps 07:00-14:30 the morning after a shift and uses the day-off rule otherwise.

**Other blocks.** Kids: home duty 07:00-08:15 on weekdays and 17:45-20:00 every day. Gym: 60
minutes on Mon/Wed/Fri from max(18:00, the end of the day's last commute) when job, rhythm or
life thread has a word starting gym, climbing, lifting, crossfit, yoga, swimming, cycling,
marathon or 5k. Gaming: the free minutes from 18:00 to her bedtime when
`unit("game", guid, date) < 0.75` (an evening that games up to midnight carries on to a bedtime
after it), and on Sat/Sun also 13:00-18:00 when `unit("game_day", guid, date) < 0.5`. Every
other minute is free.

**Paint order** (later wins): free, gaming, home duty, gym, commute, work, asleep, then a shift
that runs past midnight again (every night shift; the tail of yesterday's late shift), then out.

### The day's event

One a day, `u = unit("event", guid, date)`:

| u | Event | Effect |
|---|---|---|
| [0, .04) | `late_shift` | the shift that ends today ends 120 min later, its commute home too (none: no shift ends today); a late shape's late shift runs to 00:30 and home by 01:00 |
| [.04, .07) | `sick_kid` | the shift that starts today (and its commutes) becomes home duty, including its hours after midnight (none without kids) |
| [.07, .11) | `night_out` | out 19:30-23:30 |
| [.11, .17) | `bad_day` | no block changes (the intraday state reads it) |
| else | `none` | |

The part of a shift after midnight sits in the next date's plan and follows the drawn event of
the day the shift started. Passing `event=` forces one for that date only (tests, the CLI); an
event the day cannot have reads as `none`.

## Presence (`life/presence.py`)

`presence_at(p, her_local, *, medium="game"|"discord", in_his_group=False) -> LifePresence`
(block, where, attention, event, kids):

| Block | Game | Discord | Where |
|---|---|---|---|
| asleep | none | none | asleep |
| work | glancing | glancing | at_work |
| commute | none | glancing | busy |
| home duty | glancing | half | busy |
| gym | none | glancing | busy |
| out | none | glancing | busy |
| free | half | full | free |
| gaming | full | half | gaming |

In game, a member in his group always gives full attention.

**Availability** (`[life] availability`, 0-10; 6 is the design point; 0 means always present and
the callers keep today's behaviour):

- `attention_weight(att, availability)` = `BASE_W[att] ** (availability / 6)` with full 1.0,
  half 0.6, glancing 0.2, none 0.05; 1.0 at availability 0.
- `latency(att, availability, rng, *, medium, energy=0, stress=0)`: lognormal seconds with
  medians full 3 (sigma 0.5), half 25 (0.8), glancing 150 (0.9), none 300 (0.7); every median but
  full's scaled by `availability / 6`; x1.3 when energy < -0.3, x1.2 when stress > 0.3; capped at
  `game_cap_s(availability)` = 60 + 54 x availability in game (384 s at 6) and 2700 s on Discord.
  Availability 0 raises `ValueError` (no draw).
- `discord_state(lp, *, dnd_day)`: none offline; glancing dnd at work on a dnd day, else idle;
  half and full online.
- `away_words(lp, medium)`: asleep, at work, driving, with the kids / busy at home, at the gym,
  out, away from the keyboard (game) / away from your phone (Discord), in the middle of
  something in game.

## Command line

Read-only, on `mind.db` opened `mode=ro` (the cast's stored personas); prints guids, codes,
times and counts, never persona text or names:

```bash
python -m leeroy_mind life day --data-dir "$D" [--guid N] [--date YYYY-MM-DD]
python -m leeroy_mind life now --data-dir "$D" [--at YYYY-MM-DDTHH:MM]
```

`day` prints `<guid> job=<shape> event=<code>` and one `HH:MM-HH:MM <block>` line per span (the
date defaults to today on this PC). `now` counts the members per where, per attention in game
and on Discord, and per event, at this PC's time (or `--at`). Exit 0 done, 1 unknown guid or a
database error, 2 no `mind.db`.
