# Member drama

Romances between members, the owner drawing a partner away, and jealousy that boils over.
The approved design is the owner's 2026-11-15 member-romance spec (local docs only).

## Operator notes

Enable with the following in `mind.toml`, then restart the mind and console:

```toml
[drama]
member_romance = true
```

The default is off. An invalid table (an unknown key, a value that is not a boolean, an
unreadable file) turns it off and logs `drama_config_invalid key=<code>` once. The switch is read
when the mind starts and pinned until it restarts: every night keeps the startup value (as
`[learn] enabled` does, `nightly/wiring.py`), the `couples` stage is attached only when it was on
and itself does nothing on a night whose switch is off, and live talk's cards read the same
pinned value (`drama.config.pinned`). Editing `mind.toml` without a restart changes nothing.
The explicit `nightly run` command attaches the same couples stage and pins its configured
value for that command's context, then restores the prior process value, including on failure.
Each draw-away and spark commits separately. A bad pair does not roll back other pairs;
resuming does not repeat erosion or exceed the two-spark nightly cap.
Off, no night stage runs, the storyteller forms no `flirt` or jealousy confrontation, encounter
prompts keep "no romance or flirting between them", jealousy writes no resentment or envy words,
and cards never mention a member romance.

Migration `0049_member_romance.sql` adds `breakup`. Code from before it refuses a database it has
migrated, so rolling back means restoring the `pre-release-*.db` snapshot, which also loses
everything written since the release. Turning the switch off instead leaves existing couples'
stages on their relationship rows (the Social tab and the server's ties still see them), but
nothing moves or shows them in talk.

Four changes ship without the switch:

- **Scenes are judged for contradiction only** (`nightly/ground.py` `CONTRADICT_ONLY`). A scene
  and its two memories are fiction, so their batch has its own system line, question and schema
  (`FICTION_SYSTEM`, `FICTION_ASK`, `FICTION_SCHEMA`, overlay `prompts/nightly_judge_scene.txt`):
  "is it contradicted by its evidence?", `contradicted: true` rejects, and so does a broken rule.
  Invented detail the evidence does not mention passes. A person offered as "might be around"
  grounds a new name only if the scene also reports a valid observation for them; that person
  then receives the corresponding knowledge. The `around:<encounter>` grounding item never
  enters memory evidence. Offscreen scene memories do not become relationship-note evidence
  or third-party gossip (including gossip through a derived memory). Every privacy rule, the deterministic name
  checks against anyone not around, and the one memory per participant rule are unchanged.
- **Rumours retain their source account.** A model's separate `claim` field remains accepted
  for schema compatibility but never becomes truth. New canonical text is the source memory's
  own words; altered retellings stay in the versions. Social and correction prompts also read
  the source for old rows, without rewriting history. Breakup accounts remain code-built.
  An unavailable original is labelled as such rather than displaying an unjudged claim.
- **Jealousy no longer feeds on itself.** Its own memories (keys `jealousy:` and `envy:`) are not
  observations, so a saturated level comes down by `DECAY` a night when nothing new happens.
- **Breakup rumours never take a live-rumour slot** (`gossip.organic_rumors`); they exist only
  with the switch on.

## What the switch adds

- **Romances.** A man and a woman of the cast who like each other (affinity 10 both ways) may
  spark (`couples` night stage, after `romance`): 15% a night, at most 2 a night, 0.15 of that for
  a woman drawn to or lusting after him. Never for someone whose household names a spouse, a
  fiancé or a partner of her own (`household_status` married, engaged or partnered: the reading
  the console's relationship status shows; a relative's or a friend's, and a separated or late
  spouse, do not count), someone already in a member romance, a woman whose
  romance with him has begun (banter or later), a pair with an open feud, or two exes within 30
  nights of their breakup, or a woman he has pursued in the past three days (including a
  quiet drift that wrote no breakup). A current partner disclosed in her persona also counts
  as taken; remarriage and partnered households are recognized, past and business partners excluded.
  Their relationship rows climb notices, banter, whispers (a crush), late_nights (dating),
  confession (together) through their scenes: each warm scene adds 8 attraction both ways and may
  take one step once both rows meet the step's gate and its nights have passed; a cold scene
  takes 6, and short of a crush a romance under 15 attraction ends. Couples get a `flirt`
  volition; their scene prompt and both judges allow warm, never explicit flirting between the
  two of them only (`nightly/llm.py` `COUPLE_RULE`).
- **Drawn away.** His pursuit is his deliberate attention at her in the last 3 days: his flirts
  at her (lines and romantic emotes), compliments aimed at her, and his whisper or DM lines to
  her. Raid or party time with her and his guild-chat replies never count. While he pursues her,
  her warm scenes with her partner add nothing on her side and take no step, and each night her
  attraction to the partner erodes by 10. Once her attraction to him is 10 over her partner's
  (plus 10 dating, 20 together) she leaves. Sustained ordinary private conversation is enough;
  flirting is not required (owner ruling 2026-11-21). When he has answered her private lines
  on at least two local nights within the last three days, a fresh answer this night can add
  the ordinary +3 `answered` attraction gain. Each answer must be within five minutes, in the
  same medium and between that pair. A night with any attraction gain or hurt already recorded,
  an already credited line, a one-sided message, or a rude exchange adds nothing. The local
  06:00-to-06:00 night boundary matches the existing curve; the gain is at most once per night,
  honors the 100 cap and stance bands, and resumes with the pair's transaction. A current
  `none` stance prevents both this gain and draw-away, even if a stale attraction score remains.
  From a crush on that is a breakup: `breakup` row, both rows
  `ex`, the partner's hurt memory and distress, capped affinity and trust loss, a live rumour
  about her spread by the partner, and by the partner's traits a feud with her. The split is
  public. Why she left is public only when some of his pursuit reached the whole guild (a
  guild line or public Discord channel): then the partner's
  memory, his resentment and envy of the owner (85 or more), the feud cause and the rumour ("X
  left Y for him") name him. Private voice, party, raid, say, yell and local emotes do not grant
  that permission. Without guild-wide evidence, the partner's memory says
  only that she has been distant, the rumour and feud cause only that they split, and her own
  memory of leaving for him is private with him. Before a crush she just drifts away. A woman
  whose stance toward him is none is never drawn away. A member never draws away a woman who is
  his.
- **Jealousy.** Rivalry at 70 or more gives the jealous member a `confront` volition toward her
  rival (ref `rule:jealousy:<rival>`), her view of the rival drifts by -2 a night, and a
  confrontation that goes bad sours her by up to -8 and the rival by half, under the night cap. A
  blow-up opens a feud once her own affinity is at -30 or below. Envy at 70 or more, fed tonight,
  writes the envious man a weekly memory that he means to tell the owner so (`envy:` key).
- **Live talk.** Member-romance stages appear on relationship cards only in pair turns,
  never public or group turns. Other relationship information is unchanged. Social evaluation's
  `romance_moves` counts the owner's arcs alone. Feud cycles after renewed jealousy remain
  governed by the existing five-day make-up and jealousy decay; no new cooldown is introduced.

Logs: `member_romance_night couples= sparks= breakups= drifted=`, `member_romance_step a= b=
stage=`, `member_romance_fizzled`, `member_breakup id= woman= partner= stage= public= feud=
rumor=`. The Social tab lists couples by stage and a Breakups panel (who left whom for him).

## Threads (recording)

Phase 1 of romantic threads (`drama/pulls.py`): the night records what would pull two members
together and **changes nothing**. No member's behaviour, relationship row, attraction, scene,
prompt or memory reads or is written from it. The pull it keeps is a shadow (`pull_shadow`),
never `relationship.attraction`.

### Operator notes

```toml
[drama]
threads = "record"   # "off" is the default; "on" is reserved for the release that acts on it
```

Restart the mind: the mode is read at its start and pinned (as `member_romance` is), and only
`"record"` adds the model-free night stage `pulls` after `encounters`. `"on"` is refused: it logs
`drama_config_invalid key=threads_on` and reads as off. Any other bad value logs `key=threads`
and reads as off. A bad `threads` never turns `member_romance` off. It does not need
`member_romance`. A manual `nightly run` attaches the stage too when `mind.toml` says
`"record"` (on its trial copy unless `--live-db`).

Migration `0061_pull_threads.sql` adds `pull_event`, `pull_shadow` and `pull_night`. Before
rolling back to code from before it, remove `threads` from `mind.toml` (older code reads an
unknown `[drama]` key as an invalid table and turns `member_romance` off) and restore the
`pre-release-*.db` snapshot (older code refuses a newer schema).

- `python -m leeroy_mind drama threads report [--nights N] [--json]` reads `mind.db` read-only:
  events by kind per night, the top shadow pulls by pair (guids), the would-be threads per
  night with their shapes, how many pulls stand at 10, 20 and 35, the men's and women's highest
  pull, and why candidates were held back.
- `python -m leeroy_mind drama threads replay --from YYYY-MM-DD [--keep] [--json]` runs the same
  recorder over the nights `night_run` already holds (status done, their stored windows) on a
  copy in the data directory's `scratch/` folder, prints the report and deletes the copy
  (`--keep` leaves `mind-threads-replay-<stamp>.db`, which is private data). The live database
  is opened read-only and never written. It replays against today's cast, households, stances
  and relationship rows, since older ones are not stored per night. `talk_move` is pruned after
  3 days, so a replay finds `joked` and `complimented` only on its last nights.
- The console's Social tab has a "Threads (recording)" panel with the same numbers, labelled as
  not in effect.
- Health line `drama-threads`: a note when recording is on and the last three recorded nights
  held no event, or three finished nights after the newest recorded one were not recorded
  (nothing is noted before the first recorded night).
- Logs: `pulls_night night= events= pairs= open= opened= faded= waiting=`, `thread_would_open a=
  b= shape= taken=`, `pulls_night_skipped night= reason=`. Ids, counts and codes only.

### What is recorded

One `pull_event` row per fact that would have moved the directed pull a -> b (a's pull toward
b), for a man and a woman of the cast (same-sex pairs are not in this release; a member on a
break and the owner are left out). Only public evidence is read.

**Presence** (time spent in one place). It saturates: a pair's first presence event of a night
counts whole, every later one at a quarter, and presence gives a pair at most 2 a night. A
thread never opens on presence alone.

| Kind | The fact | Weight |
|---|---|---|
| `raided` | both raided in a guild raid with him | 1 |
| `grouped_away` | both raided in an offline raid (him absent) | 1.5 |
| `kill_alive` / `first_kill` | both stood at a boss kill (the guild's first of that boss) | 1 / 2 |
| `wipe_stood` | both still standing when a wipe was called | 0.5 |

**Social** (something passed between them).

| Kind | The fact | Whose pull | Weight |
|---|---|---|---|
| `partied` | both in his party of five or fewer: a small activity together | both | 4 |
| `warm_scene` | tonight's accepted scene of theirs whose applied changes sum above zero | both | 10 |
| `took_side` | someone set a rumour about her straight to her face (a `correct` scene with her), or sided with her in a feud | hers | 12 |
| `heard` | a reply naming her and agreeing, within 5 minutes of her line on a public channel | hers | 7.5 |
| `defended` | a member answering a taunt aimed at her on a public channel (the owner's taunt counts) | hers | 12 |
| `answered` / `replied` | a Discord reply to her message within 10 minutes, on a public channel | hers 5 / the replier's 4 | |
| `talked` | his message right after hers in a public Discord channel (5 minutes) | both | 2.5 |
| `mentioned` | a public Discord message or game line naming her: her name capitalised, or an @-mention | hers | 4 |
| `reacted` / `banter` | her reaction to his public Discord line; `banter` when she spoke in that conversation within 30 minutes | hers | 2.5 / 6 |
| `riffed` | both spoke in one banter riff (the `[banter]` threads: lines sent as banter, 10 minutes or less apart on one public channel) | both | 6 |
| `joked` | a joke, tease or callback the talk pipeline scored (`talk_move`) at the other on a public channel | both | 4 |
| `complimented` | a compliment scored the same way | the one complimented | 7.5 |
| `spoke_well` / `praised` | a good opinion of her by name on a public channel, as the claims table scored it (`member_claim`) | hers 9 / the speaker's 5 | |
| `admired` | tonight's reflection left her admiring or grateful to him, on memories that all read open | hers | 5 (3.75 to 6.25 by intensity) |
| `voice_room` / `voice_late` | 20 minutes in one public voice room of at most 4 members that night; 30 of them between 22:00 and 04:00 on her own clock | hers | 5 / 7.5 |
| `dkp_close` | a close DKP award between them: an outcome, it moves nothing | both | 0 |
| `dkp_respect` | respect gained for the other within 7 days of a close award (and before the recorded night's end), once per pair and award | the one who gained it | 7.5 |

Each kind has a cap per directed pair and night, and a pair's pull can take at most 12 a night.
One Discord message counts once for a pair: when agreeing with her by name (`heard`) or
defending her already counted on its chat line, its reply or name does not count again.
A gain is multiplied by:

- **the household cost**: 0.6 for a member taken at home (`couples.taken`: married, engaged or
  partnered; a cost, not a ban);
- **her stance toward the owner**, for a woman: fond 0.9, drawn 0.8, lust 0.6. Her pull toward
  a member is about her and him, so the bite is small, and the two costs together never go
  under 0.4;
- **likeness**, 0.8 to 1.25, the same both ways: same region, same night rhythm, same humour,
  shared things they do outside raids, shared likes, less when nothing is shared, and a small
  draw seeded by the pair.

A pull halves over 21 nights without an event; its public part halves over 21 nights without a
public event, and its social part over 21 nights without a social one, so a pair that now meets
only in private scenes keeps no public part it no longer earns.

Not read, because nothing is stored for it: members sitting near each other (the inn seats are
the owner's invitations only), member-only 5-mans (only his party and offline raids are
recorded), battlegrounds (the cast's random queues put hundreds of members a night in one, so
it says nothing about a pair).

**Privacy.** Every row carries the scope of its evidence and the evidence's id, never text.
Whispers, DMs, group DMs, the officers' channels and voice room, the private, interview and
penalty-box voice rooms (the rooms learn reads as not public too) and Discord channels the
layout lacks are never read. A pull moves only by what its holder could have witnessed: a game
line's stored audience decides who read it (a party, raid, say or voice room line reaches only
those there; a line stored without one reaches nobody), a scored joke or compliment counts only
when the speaker's own line carrying it is found and the one it was at could read it, and a
rumour set straight to someone else moves nothing of the one it is about, since nothing stores
that she learned of it.

Scopes: a scene is between the two (`pair`). A feeling of the holder's own that the other never
saw is `private`: an admiration (`admired`) and respect gained after an award (`dkp_respect`).
An admiration is read only when every memory it rests on reads open by its effective scope
(`memory.privacy.effective_scopes`): a memory stored open that rests on a private or secret one
(before the night's settle raises it) is not open. `pull_shadow.public_pull` is the part public
evidence alone would have built, for the later "what the guild knows" view; `social_pull` is the
part that did not come from presence. Nothing recorded is shown to a member or creates a fact.

**The gate for anything shown.** A later phase that shows anything beyond the owner's console
must gate on `public_pull` (what the guild could know) or on `pull_event.scope` (`pair` for
what the two share), **never on `pull`, `thread_state` or `shape`**: those come from the whole
pull, and the would-be threads of the live replay rest mostly on pair and private evidence.
"Public" here is "not private or restricted": a party, raid, say or voice-room line and presence
are public though only those there saw them, so a "what the guild knows" view must not read
them as known to all.

### Would-be threads

After the night's pulls, a directed pull of 35, at least 12 of it social, between two who do
not dislike each other (affinity 0 or more, both ways) would open a thread on a seeded draw
(50% a night) unless the pair is feuding, exes inside 30 days, or the pair's other direction
already holds one. The caps then admit the rarest shape among the live ones first (counted
again after each opening): 3 live guild-wide, 1 with a taken member, one per member, one new
thread a night. `pull_shadow.thread_state` is `open`, `waiting` (with `thread_why`: `draw`,
`affinity`, `social`, `feud`, `ex`, `pair_open`, `cap_member`, `cap_taken`, `cap_live`,
`cap_night`), `faded` (an open one whose pull fell under 10) or `none`. `shape` is computed
from the night's facts: `triangle` (a third member's pull of 25 toward either, either in a
member couple with someone else from `banter` on (two who only notice each other are no third),
or her arc with the owner at `late_nights` or later), `mutual_secret` / `mutual_open` (the
other's pull at 20; secret when either is taken), `slow_burn` (20 nights from the first event)
or `unrequited`. A woman whose arc with the owner is at `late_nights` or later gets `ceiling =
acknowledged`: her thread would never pass that.

The design asked for affinity 20. The stored affinity between members is almost empty (on the
live copy, 143 of 18,715 directed member rows are above 0 and 70 at 20 or more; 277 are below
0), so that gate would admit nobody whatever happened between two members; the social part of
the pull is the liking, and the stored affinity only vetoes a pair where either dislikes the
other.

Not in phase 1: the rescue source (it needs a new native fact), the drama dial on the opening
draw, cooldowns after an ending, pressures, choices, exposure, and anything a member could
notice.
