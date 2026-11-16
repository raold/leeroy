# Her day (`leeroy_mind.life`)

A member's day outside the game: a deterministic plan of her local day built from her persona,
where she is at any minute, and how closely she is watching the game and Discord. Section 1 of
the human-likeness design ("Her day: presence and availability").

**`[life] availability`** (0-10, code default 0: always present, today's behaviour; the release
sets 6, the design point) moves Discord presence and the replies on her own time (below,
"Presence on Discord") and who answers him in game and how soon (below, "In game: who answers and
how soon"). **`[life] state`**
(default false; the release sets true) records each member's feeling through the day (below, "Her
feeling right now"); nothing reads it in a prompt yet. There is no migration and no native change.

The day, presence and state models (`day.py`, `presence.py`, `state.py`) are pure: no clock, no
database, no model, no log (`statebook.py` keeps the state in meta). The same persona and date
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
- `free_after(p, her_local, busy=NO_PHOTO)`: her local time at the start of the first minute
  after `her_local` in a block outside `busy` (today's plan, then the next two days'), or None
  when she is not in one of them now. `NO_PHOTO` is asleep and commute: a requested photo
  (`[photos] scene_from_words`, photos/README.md) waits for that time.

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
time (`discord.presence.local_time(clock.local(), region)`), or None at availability 0 or for a
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
