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
  Invented detail the evidence does not mention passes. The people the scene prompt lists as
  "might be around" are really around, so the scene may name them (an `around:<encounter>`
  grounding item that the memories never cite). Every privacy rule, the deterministic name
  checks against anyone not around, and the one memory per participant rule are unchanged.
- **Rumours are third-person claims.** The scene's gossip must carry a `claim`; a new rumour's
  canonical text (what the Social tab shows) is that claim, else the retelling only when it was
  told plainly (operation `level`), and only when it names its subject, quotes nobody and says
  no I or we (`gossip.third_person`). A telling with neither still reaches its hearer, with no
  rumour row. A correction still tells the source memory's own words, never a model's claim.
  Existing live rumours are left to fade.
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
  nights of their breakup.
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
  (plus 10 dating, 20 together) she leaves, so steady pursuit breaks even a couple who are
  together within about two weeks. From a crush on that is a breakup: `breakup` row, both rows
  `ex`, the partner's hurt memory and distress, capped affinity and trust loss, a live rumour
  about her spread by the partner, and by the partner's traits a feud with her. The split is
  public. Why she left is public only when some of his pursuit was public (a guild, party, raid,
  say or yell line, a public Discord channel, or an emote in the game): then the partner's
  memory, his resentment and envy of the owner (85 or more), the feud cause and the rumour ("X
  left Y for him") name him. When all of it was in whispers or DMs, the partner's memory says
  only that she has been distant, the rumour and feud cause only that they split, and her own
  memory of leaving for him is private with him. Before a crush she just drifts away. A woman
  whose stance toward him is none is never drawn away. A member never draws away a woman who is
  his.
- **Jealousy.** Rivalry at 70 or more gives the jealous member a `confront` volition toward her
  rival (ref `rule:jealousy:<rival>`), her view of the rival drifts by -2 a night, and a
  confrontation that goes bad sours her by up to -8 and the rival by half, under the night cap. A
  blow-up opens a feud once her own affinity is at -30 or below. Envy at 70 or more, fed tonight,
  writes the envious man a weekly memory that he means to tell the owner so (`envy:` key).
- **Live talk.** A card between two members shows their romance ("you're dating", "you broke
  up"), never on group turns.

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
`member_romance`.

Migration `NNNN_pull_threads.sql` adds `pull_event`, `pull_shadow` and `pull_night`. Before
rolling back to code from before it, remove `threads` from `mind.toml` (older code reads an
unknown `[drama]` key as an invalid table and turns `member_romance` off) and restore the
`pre-release-*.db` snapshot (older code refuses a newer schema).

- `python -m leeroy_mind drama threads report [--nights N] [--json]` reads `mind.db` read-only:
  events by kind per night, the top shadow pulls by pair (guids), the would-be threads and why
  candidates were held back.
- `python -m leeroy_mind drama threads replay --from YYYY-MM-DD [--keep] [--json]` runs the same
  recorder over the nights `night_run` already holds (status done, their stored windows) on a
  copy in the data directory's `scratch/` folder, prints the report and deletes the copy
  (`--keep` leaves `mind-threads-replay-<stamp>.db`, which is private data). The live database
  is opened read-only and never written. It replays against today's cast, households, stances
  and relationship rows, since older ones are not stored per night.
- The console's Social tab has a "Threads (recording)" panel with the same numbers, labelled as
  not in effect.
- Health line `drama-threads`: a note when recording is on and the last three recorded nights
  held no event, or three finished nights were not recorded.
- Logs: `pulls_night night= events= pairs= open= opened= faded= waiting=`, `thread_would_open a=
  b= shape= taken=`, `pulls_night_skipped night= reason=`. Ids, counts and codes only.

### What is recorded

One `pull_event` row per fact that would have moved the directed pull a -> b (a's pull toward
b), for a man and a woman of the cast (same-sex pairs are not in this release; a member on a
break and the owner are left out):

| Kind | The fact | Weight | Scope |
|---|---|---|---|
| `raided` | both raided in a guild raid with him | 1 | public |
| `grouped_away` | both raided in an offline raid (him absent) | 1.5 | public |
| `kill_alive` / `first_kill` | both stood at a boss kill (the guild's first of that boss) | 1 / 2 | public |
| `wipe_stood` | both still standing when a wipe was called | 0.5 | public |
| `warm_scene` | tonight's accepted scene of theirs whose applied changes sum above zero | 2 | pair |
| `took_side` | someone set a rumour about her straight (a `correct` scene), or sided with her in a feud | 4 | restricted / public |
| `heard` | a member's reply naming her and agreeing, within 5 minutes of her line on that channel | 2 | the channel's |
| `defended` | a member answering a taunt aimed at her (the owner's taunt counts) | 4 | the channel's |
| `banter` | her reaction to his Discord line in a conversation she spoke in within 30 minutes | 2 | the channel's |
| `voice_late` | 30 minutes or more in one voice room between 22:00 and 04:00 on the holder's own clock (90 minutes: 2); a room that held more than 6 members that night is a crowd and counts for nobody | 1 / 2 | public, or restricted for the officers' and private rooms |
| `dkp_close` | a close DKP award between them: an outcome, it moves nothing | 0 | public |
| `dkp_respect` | respect gained for the other within 7 days of a close award, once per pair and award | 3 | public |

Each kind has a cap per directed pair and night, and a pair's pull can take at most 8 a night.
A gain is multiplied by 0.6 for a member taken at home (`couples.taken`: married, engaged or
partnered; a cost, not a ban) and, for a woman, by her stance toward the owner (fond 0.8, drawn
0.5, lust 0.3). A pull halves over 21 nights without an event.

**Privacy.** Every row carries the scope of its evidence and the evidence's id, never text. A
pull moves only by what its holder could have witnessed: whispers and DMs (which are with the
owner) are never read; a line on a channel whose stored audience leaves her out counts for
nothing; a restricted channel (the officers', a group DM, a Discord channel the layout lacks)
with no stored audience counts for nothing; a group DM line counts only for a reader in its
stored audience and is restricted evidence. `pull_shadow.public_pull` is the part public
evidence alone would have built, for the later "what the guild knows" view. Nothing recorded is
shown to a member or creates a fact.

### Would-be threads

After the night's pulls, a directed pull of 35 with affinity 20 would open a thread on a seeded
draw (25% a night) unless the pair is feuding, exes inside 30 days, or the pair's other
direction already holds one; the caps then admit the rarest shape among the live ones first:
3 live guild-wide, 1 with a taken member, one per member. `pull_shadow.thread_state` is `open`,
`waiting` (with `thread_why`: `draw`, `affinity`, `feud`, `ex`, `pair_open`, `cap_member`,
`cap_live`, `cap_taken`), `faded` (an open one whose pull fell under 10) or `none`. `shape` is
computed from the night's facts: `triangle` (a third member's pull of 20 toward either, either
in a member couple with someone else, or her arc with the owner at `late_nights` or later),
`mutual_secret` / `mutual_open` (the other's pull at 25; secret when either is taken),
`slow_burn` (20 nights from the first event) or `unrequited`. A woman whose arc with the owner
is at `late_nights` or later gets `ceiling = acknowledged`: her thread would never pass that.

Not in phase 1: the design's likeness multiplier, the rescue source (it needs a new native
fact), the drama dial on the opening draw, cooldowns after an ending, pressures, choices,
exposure, and anything a member could notice.
