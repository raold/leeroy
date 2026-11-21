# Her day (`leeroy_mind.life`)

A member's day outside the game: a deterministic plan of her local day built from her persona,
where she is at any minute, and how closely she is watching the game and Discord. Section 1 of
the human-likeness design ("Her day: presence and availability").

**`[life] availability`** (0-10, code default 0: always present, today's behaviour; the release
sets 6, the design point) moves Discord presence and the replies on her own time (below,
"Presence on Discord"), who answers him in game and how soon (below, "In game: who answers and
how soon") and her stepping away mid-conversation (below, "Interruptions and coming back").
**`[life] state`**
(default false; the release sets true) records each member's feeling through the day (below, "Her
feeling right now"); nothing reads it in a prompt yet. There is no migration and no native change.

The day, presence, state and interruption models (`day.py`, `presence.py`, `state.py`,
`interrupt.py`) are pure: no clock, no
database, no model, no log (`statebook.py` keeps the state in meta). The same persona and date
always give the same plan. Plans are immutable and cached (`PLAN_CACHE`, 1024: two a member, today's
and yesterday's for a late evening), keyed by the schedule facts (guid, job, rhythm, household,
life thread, night_owl) and the date, so an edited persona gets a fresh plan. Draws use `life.seed` (`unit(*parts)` for one number in [0, 1),
`seeded(what, night, guid, minute, extra)` for a generator of its own), never the talk engine's
shared rng, so a switch that is off draws nothing and moves no other draw.

Guild members can plan shared in-game blocks with `[life] day_plans` and agree on five-member heroics with `[life] dungeons`. Both default off. The guild-life controller coordinates the party, GPU lease and native receipts. His validated request for a participant takes priority. [In-game activities and members’ heroics](../godmode/README.md)

## Her day plan (`life/day.py`)

`day_plan(p, day, *, event=None) -> DayPlan`: every minute of her local date (0-1439) in exactly
one block: `asleep`, `work`, `commute`, `home_duty`, `gym`, `out`, `free`, `gaming`.
`DayPlan.spans` are contiguous `Span(start, end, block)` from 0 to 1440 (end exclusive);
`DayPlan.block_at(minute)`, `DayPlan.workday`. `block_at(p, her_local)` gives the block and the
day's event at a local wall-clock time. Her local time is her home's (`discord.presence.her_local`,
geography/README.md: her state's zone, UK time for a UK member) while `[geography]` is on; a US
member without a home keeps US Eastern, as Discord presence has it (`local_time`).

### Job shape

From `job + " " + rhythm`, case-folded. With `[talk] coherence` on (default off; read at the
mind's start and at his login; off, a card reads exactly as the table reads it and the rows
marked "coherence" below do not exist) three readings come around the table (2026-11-17, the
self-play audit: a bartender, a trainer with early clients, a half-day worker and a paralegal on
maternity leave all had the `day` plan). They read only **clauses about work**: the text is
split at commas, full stops, brackets, "and", "then", "but", and a clause about raiding,
playing, the game, the guild, being online, the gym, sleep or "after work" is left out, so
"raids 8pm to 11pm", "early mornings at the gym" and "opens the guild bank" shape nothing.

1. **Leave is `home`**: maternity, parental, paternity, medical, sick or long-term leave, "on
   leave", signed off, sabbatical, furloughed, laid off, out of work; not leave in the past
   ("sick leave last year"). She has the job and is not at it.
2. **The working hours her card states outright** (`_clock_shape`): a range such as "4am to
   1pm", "2pm-10pm" or "22:00 to 06:00" in a work clause that has a word for working in it
   (works, shift, on call, opens, teaches, clients, patients, at the office/plant/bar...), 2 to
   14 hours long. By its start: 03:00-07:29 `early`, 07:30-10:59 `day` (five hours or less:
   `part`), 11:00-15:59 `late`, 16:00-18:59 `evening`, else `nights`. Hours with am, pm or
   minutes come before the table: a card whose job says "third shift" and whose rhythm says "4am
   to 1pm work" has one clock, the stated one. Bare hours ("works 9 to 5") are read only after
   the table has said nothing, so "night shifts, 7 to 7" stays nights.
3. **After the table**, on work clauses only, the "coherence" rows.

**She works weekends** ("works weekends", "weekend shifts", "evenings and weekends"): her
workdays are Wednesday to Sunday for every shape but `nights` and `rotating`.

The table, first word-bounded match wins, else `day`:

| Shape | Pattern |
|---|---|
| `nights` | `night[- ]?shifts?\|third[- ]shifts?\|work(?:s\|ing)?(?: on)? nights\|(?:i'm\|she's\|is\|was\|been) on nights\|overnights?\|graveyard` ("raid nights", "late nights" and "plays on nights" are no night shift) |
| `rotating` | `\d+\s*on\s*,?\s*\d+\s*off\|\d+\s*(?:days\|weeks)\s*on\|rotating\|rota\|12[- ]hour\|on call` |
| `early` | `early shifts?\|early starts?\|[4-6]\s*a\.?m\.?\s*starts?\|starts? at [4-6]` |
| `late` | `late shifts?\|evening shifts?\|swing shifts?\|afternoon shifts?` |
| `student` | `student(?! loans?)\|studying\|(?:attends?\|attending\|has\|skips?\|skipping\|goes to\|going to) lectures?\|(?:takes?\|taking\|attends?\|attending\|night\|evening) classes\|(?:back\|off\|goes\|going\|still\|is\|she's\|been\|starts?\|starting) (?:to\|at\|in) (?:uni\|university\|college)\|(?:uni\|college) (?:classes\|courses?\|lectures?\|coursework\|assignments?)\|doing an? (?:[\w-]+ ){0,2}degree`, unless the text also says `teach\w*\|tutor\w*\|lecturer\|professor\|instructor\|gives? lectures?\|works? (?:at\|for) (?:the \|a )?(?:uni\|university\|college)`: only what she does, so an employer named "... College", a degree she holds, a job at a university or a class she teaches is no study |
| `remote` | `wfh\|works? from home\|from home\|freelanc\w*\|self[- ]employed` |
| `home` | `stay[- ]at[- ]home\|unemployed\|between jobs\|retired` |
| `nights` (coherence) | "nurse/ward/hospital/security/warehouse ... on nights", "nights at the hospital/ward/warehouse/depot/plant" |
| `early` (coherence) | early mornings, opening or morning shifts, "opens up/the", up at or before dawn or 4-6, baker, bakery, milk round, postie |
| `evening` (coherence) | bar, pub, club and kitchen work: bartend*, barmaid, barman, bar staff/work/manager, behind/tends/runs a bar, works at or in a bar/pub/club/restaurant, pub landlady/landlord, "evenings and weekends", closing shifts, works evenings, chef, line cook, waits tables, bouncer, door staff |
| `late` (coherence) | second shift, "works lates" |
| `home` (coherence) | full-time mum/dad/parent/carer, housewife, homemaker |
| `part` (coherence) | `part[- ]time\|half[- ]days?\|mornings only\|school hours\|term[- ]time`, two or three days or mornings a week |

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
| `evening` | Wed-Sun (her weekend is Monday and Tuesday) | 16:00-23:30 | 15:30-16:00, 23:30-24:00 | 10:00 |
| `part` | Mon-Fri | 09:00-13:00 | 08:30-09:00, 13:00-13:30 | 07:30 |
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
- `free_after(p, her_local, busy=NO_PHOTO)`: her local time at the start of the first minute
  after `her_local` in a block outside `busy` (today's plan, then the next two days'), or None
  when she is not in one of them now. `NO_PHOTO` is asleep and commute: a requested photo
  (`[photos] scene_from_words`, photos/README.md) waits for that time.

### Her evening under a job shape (`[talk] coherence`)

With the switch on no job shape alone has her asleep before 23:30 (`day.BED_FLOOR`): the early
plan sleeps from 21:00-22:00, the hours he plays, and five of six live cards read as early
shifts say they play late. A card that states late play (the night-owl field, "late nights",
"up late", "raids evenings", "raid nights", "plays until 11pm", "nights are for the game") is
awake until midnight; a card that states an early bedtime ("in bed by nine", "early nights")
keeps its shape's. Her work hours and waking time are the shape's as before. The `life day|now`
commands read the switch from `mind.toml` as the running mind does. Weekend work is also read
across an "and" ("works saturdays and sundays").

### One "right now" per conversation (`[talk] coherence`)

With `[talk] coherence` on the "Right now" line agrees with the conversation
(`stated_where`, `settled_where`, `playing_text`, `away_note`; `talk/README.md` "Conversations
that hold together"). `stated_where` reads only a sentence of hers that opens with the place,
about herself, now ("at work, ...", "im still in bed", "ok im home now"); never "she's at work",
"im not at work", "are you at work?", "i was at work" or "ill be at work by nine". The one
reader is `talk/sofar.py`'s `her_place` and `her_state`: `stated_where` leaves out sentences
that are questions, negations, past, future or about someone else, hands the rest to it and
names the kind of the words it returns. That reader wants "i'm", "still", "back" or "just got"
before the place, so a bare "at work, ..." is not read. Where
she said she is in the last 30 minutes holds whatever block her plan
has moved to ("You're still at work, as you told him a little while ago."); joining his group
after saying so reads "You told him a little while ago you were at work: you've just got in from
work and logged on."; a member in his group while he plays reads "You're at your PC, playing:
you're in <his name>'s raid with him right now, in Naxxramas." (the instance by her map, when it
is a raid or a 5-man); and for 30 minutes after she is back from a brb the line adds why she
stepped away ("someone was at the door").

## Settings (`[life]`, `life/config.py`, `life/service.py`)

```toml
[life]
availability = 0   # 0-10: how often members are away, busy or slow to answer (0: always there)
state = false      # her feeling through the day (below, "Her feeling right now")
wow_vs_life = 5    # 0-10: the night's WoW-vs-life theme (nightly.settings.load_life)
```

`config.LifeCfg(availability=0, state=False, wow_vs_life=5)` is `MindConfig.life`. An invalid
`[life]` key never blocks the rest of mind.toml (a login still applies his other edits): an
`availability` that is not an integer 0-10 (a bool is not) reads as 0, a `state` that is not a
boolean as false and a `wow_vs_life` that is not an integer 0-10 as 5, each logged once per value
(`life_<key>_invalid key= want= default=`, never the value); a `[life]` that is not a table reads
as empty (`life_invalid`). The night still refuses an invalid `wow_vs_life` as before
(`nightly.settings.load_life` raises `ConfigError`, logged `nightly_config_error`). `availability` is also a world dial
(console Tuning, `tuning.dials.WORLD_DIALS["availability"]`): an override in mind.db replaces
mind.toml's value without writing it.

The talk engine owns `LifeService` (`TalkEngine.life`). It is configured at mind start, at each
login (mind.toml read again), when a world dial moves and when the night reloads its settings:
`configure(cfg)` pins the values for the whole process (`life.config.pin(availability, state)`;
`availability()` and `state_on()` read them, 0 and false until pinned) and writes the
availability in force to meta `life:availability` when it changed (a missing value counts as 0).
`LifeService.presence(guid, *, medium, in_his_group=False)` is her `LifePresence` at her local
time (`discord.presence.her_local(clock.local(), p)`), or None at availability 0 or for a
member without a persona.

## Presence on Discord

With availability above 0, `discord.presence.presence_of` (unchanged signature, so every caller
follows: the Discord talk, the server engine and its seats, DMs and reactions, banter, decisions
and the console) reads her day:

- **State:** `discord_state(presence_at(p, local, medium="discord"), dnd_day=...)` with the same
  dnd day as today (one weekday in seven): asleep offline; at work idle, or dnd on her dnd day;
  driving, at the gym or out idle; home duty, free and gaming online. A member in his group is
  online and playing, as today.
- **Playing:** online, her game body online and a gaming block (or in his group).
- **Status line** (`status_text`, `STATUS_SHARE` unchanged): work `at work` / `in meetings` /
  `working` (and `lunch break` 12:00-13:00), commute `commuting` / `on my way home`, home duty
  `kids' bedtime` / `family time` / `making dinner`, gym `at the gym`, out `out tonight`; free and
  gaming (or playing) take today's clock evening and late phrases, never the clock's work or
  commute.

**Replies on her own time.** `DiscordTalk.own_time()` is `[talk] dm_own_time` or availability
above 0. So with availability above 0 his DM (and his group DM, per member) is answered at once
while she is online by her day (free, gaming, home duty), talking with him, or in his WoW group
while he plays (`owntime.with_him`: Discord shows her online and playing then), and waits while
she is away by her schedule
(asleep, at work, driving, at the gym, out): `discord_dm_later`, meta
`discord:dm:deferred:<guid>`, then `discord_dm_answered_later` (urgent words and three posts in a
row still cut the wait). The waits (`owntime.PHONE_S` for idle and dnd, `BACK_S` after she is
back) are scaled by `max(availability, 1) / 6`, never under 30 s (`LIFE_FLOOR_S`): 6 keeps
today's bounds. A late answer says where she was from her day (`life.presence.away_words(...,
"discord")`: asleep, at work, driving, at the gym, out, with the kids, away from your phone).
`[talk] dm_own_time` keeps its meaning and stays the owner's.

**The console** has no mind.toml of the mind's: `console/discordview._members` reads meta
`life:availability` (an integer string 0-10; anything else is 0) and pins it before computing
presence, so the Discord tab shows the members away that the mind has away.

Availability 0 is exactly today's presence, status line and DM timing (pinned by a hash of a
week of states in `tests/life/test_discord_presence_life.py`).

## In game: who answers and how soon

With availability above 0 (at 0 every line below is skipped and the talk engine draws exactly
today's numbers from its own generator):

- **Who.** Every speaker weight in game (`talk.pipeline._weigh`: answers to his lines, greeters,
  openers, whispers, raid reactions, the owed watch's rescues) is multiplied by
  `LifeService.weight(guid)`: `attention_weight` of her attention in game, times `1 + social / 2`
  while `[life] state` is on. A member in his group, or in the voice room he speaks in
  (`with_him`), always gives full attention. 1.0 at 0.
- **How soon.** The answers `on_human_chat` plans are ordered by attention (most attentive first,
  the plan's order otherwise), and each draws `LifeService.answer_delay` from `seeded("delay",
  night, speaker, local minute of the day, his line id)`: `latency` with her energy and stress. An
  answer he is owed (the first in that order, a whisper's, a member he named, his pushback's
  target) waits at most `game_cap_s` (384 s at 6, 600 s at 10). Any other answer drawn past
  `NON_OWED_MAX_S` (240 s) is skipped: she missed it (`reply_skipped: line= reason=away guid=`).
  The rest go out in order of their delay: the first waits its own draw, each later one
  `max(uniform(2, 6), its draw - the one before)` (one job says them one after another), and the
  first carries the owed answer. Logged `reply_latency: line= guid= att= delay_s= owed=0|1`. A line
  he spoke in a voice room is answered on today's timing (the members there are in the room with
  him). The altered states' delay multiplier is not applied to these draws, so the cap holds.
- **Waiting answers.** An answer in its delay can be hurried (`Pipeline.hurry`: `TurnMeta.wake`,
  `Pipeline._delay`). His whisper to a member whose answer to him is still waiting plans nothing
  new (`reply_coalesced: line= guid=`): that answer answers his newest line. Urgent words
  (`discord.owntime.URGENT_RE`: "are you up", "need help", ...), in his first whisper or a later
  one, or his third whisper since her last within the last 10 minutes (`owntime.TALKING_S`), hurry
  it; his goodbye hurries every game answer to him still waiting, and its own first answer.
- **His logout.** A whisper to him is never sent while he is offline (`_blocked`: `human_offline`,
  his whisper's own answer included). When the session ends with him offline (his logout, or a
  hello saying he left: `TalkEngine.end_session`), `LifeService.on_logout` drops each answer to his
  whisper not yet said (in its delay, composing or typing; never one whose line is at the bridge)
  with reason `carried` (`Pipeline.withdraw`) and queues, per member, her answer in her Discord DM
  instead (`carry_turn`): a REPLY_HUMAN turn on `dm:<guid>` to him with `CARRY_TEXT` ("He
  whispered you in game and logged off before you saw it; answer it here."), owed, his whisper as
  recall's query and in the prompt's "elsewhere" block, after what was left of her delay (none
  once it was being composed: his "gn all" then /logout still reaches him). Neither this session's
  end nor a later logout cancels it (`carried_jobs`). Logged `life_carried: guid= line=`; the owed
  watch logs `owed_carried: line= guid=` and keeps the stretch until her DM answer lands (it
  closes it); if that answer never comes, the whisper waits for his next login, as before.
- **A mind stop** (`MindApp._shutdown` sets `TalkEngine.stopping`; a clean stop, or one after the
  bridge dropped) carries nothing into the shutdown: the owed watch keeps every whisper of his
  still owed (an answer waiting, composing, held, or carried to her DM) in meta `life:carried`, a
  JSON list of `{"line", "guid", "dm"}` (his line id, the member, carried; never a text;
  `owed_persisted: whispers=`). The next start reads and clears it (`owed_restored: line= guid=
  dm=`) and, once its start grace has passed, answers each again: in her DM while he is offline
  (`owed_rescue ... channel=dm:<guid> ... reason=restored`, then `owed_carried`), else by whisper.
  A crash writes nothing (as before).
- **Health.** `owner-replies` reads `life:availability` from meta: above 0 a game line is answered
  in time within `game_cap_s` plus 2 minutes, a DM or group DM by when her own time answers it
  (`owntime.due_at`, the latest she would: never urgent; a group DM's slowest member) plus 2
  minutes, and her DM line answers his whisper too. At 0 it is today's 5 minutes.
- **Her state** is read once per member per `STATE_SNAPSHOT_S` (30 s; `StateBook.get` costs about
  three queries).
- **Discord.** His group DM drawn to one member (no one named) goes to its members online by their
  day when there are any, never first to one idle at work while another is online (review-335
  P3-E).

Unverified without a live model: how her late answers read (the late excuse is Task 9's shape).

## Interruptions and coming back (`life/interrupt.py`, core plan Task 10)

With availability above 0 (no draw at 0, so nothing moves), after a line of hers to him in their
whisper or her Discord DM her day can call her away mid-conversation (spec section 1: "brb",
"kid's up", "boss walking by"). In memory only: a mind restart forgets every away.

- **When it is drawn** (`LifeService.after_line`, from the talk's `_after_sent`, so only after a
  line was said: a turn left on read, reacted to or dropped draws nothing): her line to him in
  `whisper` or `dm:<her guid>` (never a channel, a group DM, officer chat or a voice room), his
  last line to her (his whisper, or his post in her DM: one conversation) under 300 s old, never
  after a template's line (her own brb and back), the owed watch's rescue or a carried DM, at
  most once per member per 1800 s (`INTERRUPT_GAP_S`, counted from an interruption that
  happened). Never while she owes him an answer (`_owes_him`): one of hers to him still on its
  way, a DM post of his waiting for her own time, a DM post of his of the last day that no
  message of hers replies to yet (its answer was dropped and waits for the owed watch's rescue),
  or a stretch of his the owed watch still holds for her (`OwedWatch.open_for`). Never in a quiet
  (`_quiet`, as nothing starts a conversation with him then): his raid's fight, wipe silence or
  debrief, a boss fight of his group, his AFK, the quiet after his login.
- **The draw** (`draw_interruption`, seeded `("interrupt", night, her, minute, her line id)`):
  her block as her presence on that medium says it (her own game's while she plays in his WoW
  group: PM log D15). `p = BASE[block] * availability / 6 * (1 + max(0, stress))`:

  | Block | BASE | Median away at 6 | Reason, her line |
  |---|---|---|---|
  | work | .06 | 600 s | `work`: "brb boss", "sec, work", "gotta do a work thing brb"; her own boss: "sec, client", "sec, work", "gotta do a work thing brb" |
  | home duty | .06 | 480 s | `kids` (kids at home): "brb kid's up", "sec, kids", "brb the kids"; else `home`: "brb", "sec", "one sec, brb" |
  | commute | .08 | 900 s | `commute`: "gotta drive, brb", "driving, brb", "sec, driving"; with no car: "brb my stop", "sec, getting off", "gotta get off here brb" |
  | free time | .015 | 300 s | `door` "brb door", `phone` "sec, phone", `food` "brb food" (one drawn) |
  | gaming | .01 | 240 s | as free time |
  | asleep, gym, out | 0 | - | - |

  Her own boss (`own_boss`: her job or days say self-employed, freelance, she owns or runs her
  own business) and no car (`drives` false: her days say she takes the bus, the metro or the
  train, cycles or walks to work, or does not drive) are read from her persona's day facts.
  The away is lognormal (sigma 0.5) around the median times availability / 6, under 0.9 x
  `game_cap_s` in a whisper (345.6 s at 6) and 2700 s in her DM: a length drawn past the cap is
  drawn again at 40-100% of it (`UNDER_CAP`), so aways do not pile up at exactly the cap. The
  same stream draws her line's delay (2-6 s) and what she says when she is back (`draw_back`:
  one of "back", "back, sorry", "ok back" half the time, else nothing).
- **Her line** (`_step_away`) goes out through `Pipeline.say_as` as a whisper (game) or her DM
  message (`REPLY_HUMAN`), a pair line (`TurnMeta.pair_line`: read by no one else, so the guild's
  pace never holds it back), one line in her own typing (`Pipeline._style`: her texture and
  habits, as her other lines). Its barrier, just before it goes out, withholds it when she stays
  after all (`_stays`): he wrote to her since her line (she saw it: she stays and answers him; a
  brb after his line would read as her answer to it), she owes him an answer (`_owes_him`), a
  quiet began, or availability went to 0 (`life_interrupt_dropped: guid=
  reason=his_line|answering|raid|boss|afk|login|off`; she is not away). Otherwise she is away
  from then, said or not: refused by the guards, or a whisper not sent because he has left the
  game (never a whisper to an offline player), she is still away without a line, and so says no
  back line after it either (`life_interrupt: guid= reason= away_s= said=0|1`; codes only, never
  her line).
- **Her brb and back answer nothing.** In her DM a pair line is stored with its turn starting
  `pair_line:` (`discord.transport.PAIR_LINE_TURN`), and every answered check leaves it out: the
  owed watch (`_discord_answered`), health's count (`count_unanswered`) and her own time
  (`DiscordTalk._owed`). A whisper brb or back always follows her answer and never a line of his
  she has not answered (the barrier), so the whisper checks need no mark.
- **While she is away** nothing of hers he could see goes out but her answers to him
  (`Pipeline._blocked` drops the rest `stepped_away`): no guild or party chatter, no opener, no
  banter, no server conversation or DM opener (each also leaves her out of its pool:
  `Pipeline._candidates`, `Banter.candidates`, the server engine's `_valid`, `DmOpeners._can_open`).
  Her answers to him wait:
  - In game `LifeService.answer_delay` is at least what is left of it (an answer he is owed still
    at most `game_cap_s`; any other drawn past `NON_OWED_MAX_S` is missed, as before), through the
    turn's delay with its wake: his goodbye, his urgent words and his third line still hurry it,
    his next whisper still joins it, and his logout still carries it to her DM (`CARRY_TEXT` is
    then its one excuse). Of the members planned for a line of his, one who is away is never the
    one he is owed while another is there (`_life_delays`), and she is a tenth as likely to be
    picked to answer (`weight`, `AWAY_WEIGHT`). Her emote back at his emote waits for her too, at
    most `game_cap_s`.
  - In her DM his post is deferred (`DiscordTalk._due_of`): no sooner than she is back plus
    30-120 s (`BACK_FIND_S`, `seeded("back", the away's night, her, its minute, his post)`; meta
    `discord:dm:deferred:<guid>`, the owed watch holding it `own_time`), unless it has urgent words
    or is his third post in a row, which reach her wherever she is, as in game.
  - In a Discord channel or a group DM his post goes to a member who is there
    (`DiscordTalk._draw` takes her only when nobody else is); when he named her or replied to her
    she answers when she is back plus 30-120 s, and a second answer from her is left out
    (`discord_answer_skipped: message= guid= reason=stepped_away`).
  - A line of hers to him in that whisper or DM, or an answer he is owed anywhere, before her away
    ends (a hurried answer, or an owed answer at `game_cap_s` while an away from her DM runs
    longer) ends it: she is back, says no back line, and his next line is answered at her usual
    pace without the away's excuse (`life_back: guid= said=0`).
- **Her late answer says so once.** With `[talk] shape` on, an answer to him that waited out her
  away (`Pipeline._waited_away`: he wrote while she was away, at least 20 s before she was back,
  `EXCUSE_MIN_S`; never a carried DM) is LATE, in game or her DM, its excuse "away": "You're only
  now seeing this (you were away): say so in a few words, then answer." (a DM keeps its DM
  wording and length first), and her day's own excuse is left out. In her DM the own-time late
  note is then left out too; without a shape (shape off, `dm_mirror` 0, a photo's turn) the late
  note says "while you were away" (from 10 minutes late, as before).
- **Coming back** (`LifeService.tick`, every second from the app's ticker): once an away has
  ended, to a conversation he left quiet since her brb she says her drawn back line the same way
  (a whisper only while he is in game), else nothing. Withheld (`_back_held`) if he writes first,
  she owes him an answer anywhere (its excuse says she was away: one excuse), or a quiet began.
  Logged `life_back: guid= said=0|1`.
- **Health.** Each away is also in meta `life:away:<guid>` = `<since>,<until>` (unix seconds,
  numbers only, overwritten by her next; `LifeService._mark`; dropped a day after it ended). The
  mind never reads it back (a restart forgets every away); health's `owner-replies` bound for his
  DM or group-DM post made during an away is no sooner than her return plus 120 s, plus its
  margin. A channel answer that waited for her named return can still count as late there.

## Her feeling right now (`life/state.py`, `life/statebook.py`)

Section 3 of the human-likeness design. **`[life] state`** (default false; the release sets
true): while it is on, `LifeService.state` is a `StateBook` and each member carries five numbers,
each in [-1, 1], that move through her day. While it is off, `LifeService.state` is None and
nothing is read or written. Game latency and who answers read her energy, stress and social (above,
"In game"); no prompt, intent or shape reads it yet (later tasks: her mood line, "It lands", and
the draws). Only cast members move: a guild
member at level 80 or more with a persona, never him.

| Axis | Baseline (where it rests) |
|---|---|
| valence | the night's PAD pleasure (`store.get_state(guid, lens=True).mood`, his godmode mood edits folded in); bad day -0.25, night out +0.10, sick kid -0.10 |
| energy | half the night's PAD arousal (`pad_mood.a`, 0 when none); -0.4 in the hour after she wakes, -0.3 in the 90 minutes before bed, -0.15 in the hour after a shift ends, -0.15 all day on a late shift |
| stress | (neuroticism - 5.5) / 9; +0.15 at work; sick kid +0.25, bad day +0.20 |
| social | (extraversion - 5.5) / 9 |
| toward_him | last night's feelings of hers about him: 0.1 x the sum of sign x intensity / 3 over her `emotion` rows with him as target (joy, pride, admiration, gratitude, hope, happy_for +; distress, shame, reproach, anger, fear, resentment -), within +-0.3; the night is the latest of tonight's and last night's that has rows |

Wake, bedtime and shift ends come from her day plans for yesterday, today and tomorrow, so a
shift or a sleep across midnight counts once. A stored state eases back to the baseline with a
half-life of 3 hours (6 for toward_him).

What moves it (`Impulse`, deltas in `state.IMPULSES`):

| Impulse | When | Deltas |
|---|---|---|
| his_warm / his_harsh / his_plain | a line of his to her, by `state.tone` | toward_him +0.15 / -0.25 / +0.04, valence +0.08 / -0.12, social +0.05 / +0.02, stress +0.10 (harsh) |
| ignored | once per burst of hers to him (below) | toward_him -0.08, valence -0.04, social -0.03 |
| talked | each line of hers, except to him or answering him (below) | social -0.04, never past 0.4 below her baseline |
| kill / wipe | a boss killed or wiped, its members | valence +0.15 / -0.06, energy +0.10 / -0.04, stress -0.08 / +0.08 |
| loot_won / loot_lost | she won an item; she lost one to his claim (guild ports' `loot_lost`) | valence +0.20 / -0.12, stress +0.04 (lost) |

`tone(text)`: harsh when `talk.select.pushback_kind` says anger or the line has a cutting word
(stupid, idiot, useless, pathetic, shut up, piss off, ...); else warm when `talk.filters.flirty`
or thanks and praise (thanks, ty, love you, miss you, you're the best, good job, gj, well
done, ...); else plain. Phone apostrophes read as typed ones.

**Whom his line reaches.** A game whisper: the member he whispered. Any other game line: only the
members it names (`talk.select.mentioned`) among those who could read it (the row's audience), so
a guild or party line that names nobody moves nobody and a name in officer chat reaches only an
officer. A Discord DM: its member. A group DM: the members he @-named or replied to among the
post's captured readers. A channel post: the members he @-named or replied to among the
channel's audience (an officers' channel: its officers).

**Talking tires her, him never.** Each line of hers drains social by 0.04, but talking alone
never takes it more than 0.4 below her baseline (`TALK_DRAIN_MAX`; other impulses can still move
it). A line to him (a whisper to him, her DM with him) or answering him (a reply to his Discord
post, or a line in a channel he wrote in within 120 s before it, `ANSWER_S`) drains nothing:
engagement with him is not fatigue.

**Ignored, once per burst.** Her lines to him (whispers and DMs are one conversation) at most 90 s
apart (`BURST_S`) with no answer of his between are one burst. The burst counts IGNORED once when
he wrote nothing to her in that conversation from its first line until 1200 s after its last,
while he wrote somewhere else in that window (he was around and did not answer). Bursts are kept
in memory: a restart forgets the open ones.

**Contagion**, damped: a member's line in guild, party or raid pulls each other reader (the row's
audience, online now) 3% of the way from her valence to the speaker's; a member's post in a public
Discord channel does the same for the members of its audience whose Discord presence is online.
At most one pull per member a tick, and at most 0.3 of valence moved by contagion per member in
any hour (`CONTAGION_PER_HOUR`, kept in memory), so a busy night still leaves members different.
Never from a whisper, a DM, a group DM, officer chat or an officers' or unknown Discord channel.

**The tick** (`LifeService.tick`, from the app's ticker; a pass every 30 s) reads `chat_line` for
the game (the guild Discord's lines there are skipped) and `discord_message` for Discord, from
read marks that start at the newest rows on its first pass and advance row by row, so no history
is replayed after a start, a restart or turning it on; at most 1000 rows a table a pass. A row
that cannot be read is skipped and logged once (`life_state_row_skipped table= id= reason=`).
Boss and loot events come through the app's `boss` and `loot_won` handlers. A row or event more
than 10 minutes old when it is read (`STALE_S`) moves nothing, so a bridge replay after a restart
applies no old tone. `LifeService.tick`, `on_event` and `loot_lost` keep any failure inside
(`life_state_failed op= reason=<class>`): the app's ticker, its handlers and the guild's loot go
on.

**Stored** in meta `life:state:<guid>` as `{"v","e","s","o","h","ts"}` (valence, energy, stress,
social, toward him, written at), numbers only; no migration. `StateBook.get(guid)` is the stored
state eased to now (her baseline when none is stored; neutral without a persona); a malformed
value reads as none. Turning the switch off leaves the keys, unread; to forget them, delete the
`life:state:%` meta rows with the mind stopped.

**Words** (for her prompt, later): `words(state, to_him=, forms=)` is one line of at most three
parts ("You're tired and a bit short today.", "You're in a great mood and not feeling chatty
today."); her feeling about him ("happy with him right now", "a bit annoyed with him right now")
only when `to_him`, never on another member's turn.
`lands(state, tone, agreeableness=, forms=)` is how his line lands on her now ("it stings", "it
annoys you", "nice, but you're still a bit sore with him", "it makes you smile", "one more thing
on a busy day", "it barely registers", "you're not feeling it right now", or ""): a feeling, never
how much she says (review-340: the reply's length is the shape's).

**Logs**: `life_state_tick impulses=<n> members=<n>`, at most once a minute when something moved;
`life_state_row_skipped` and `life_state_failed` as above. Ids, counts and codes only: no text,
no names.

## Command line

Read-only, on `mind.db` opened `mode=ro` (the cast's stored personas); prints guids, codes,
times and counts, never persona text or names:

```bash
python -m leeroy_mind life day --data-dir "$D" [--guid N] [--date YYYY-MM-DD]
python -m leeroy_mind life now --data-dir "$D" [--at YYYY-MM-DDTHH:MM]
python -m leeroy_mind life intents --data-dir "$D" --guid N [--kind reply_human] [--family whisper] [--at ...]
python -m leeroy_mind life state --data-dir "$D" [--guid N]
```

`day` prints `<guid> job=<shape> event=<code>` and one `HH:MM-HH:MM <block>` line per span (the
date defaults to today on this PC). `now` counts the members per where, per attention in game
and on Discord, and per event, at this PC's time (or `--at`). `intents` prints each want's share
on a turn from her persona and her presence then (talk/README.md, "Wants"). `state` prints, per
member, `<guid> now valence= energy= stress= social= toward_him= stored=<age>|none` (her
feeling now, two decimals) and `<guid> baseline ...`; it never writes, even while `[life]
state` is off. Exit 0 done, 1 unknown guid or a database error, 2 no `mind.db`.

## His DMs are answered promptly (2026-11-17)

`[life] dm_wait_cap_s` (default 75; 0 = no cap; an invalid value reads 75) is the longest his
Discord DM, or a group DM line that names her, waits for her answer, whatever `availability`
says: idle and do-not-disturb alike, twice the cap for a member asleep or not about, and for a
post made while she stepped away. The cap is applied in `discord/owntime.due_at` after the
availability scaling; a capped wait is drawn in the cap's upper half. An answer that waited 45 s
or more says where she was (the LATE shape with `[talk] shape`, else the late note). The mind
writes the cap in force to meta `life:dm_wait_cap_s`; health's `owner-replies` bound follows it.

Interruptions (`interrupt.py`, `LifeService._after_line`) never start within 60 s of his message,
after his bad news (`talk/hisline.py`) or in an explicit scene he is turning up, and a work line
needs a workplace (`interrupt.workplace`): a member on leave, at home or out of work gets the
home lines, one who works from home has no boss walking by.
