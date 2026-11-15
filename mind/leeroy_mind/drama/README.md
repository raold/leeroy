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
  a woman drawn to or lusting after him. Never for someone whose household names a spouse or
  partner, someone already in a member romance, a woman whose romance with him has begun
  (banter or later), a pair with an open feud, or two exes within 30 nights of their breakup.
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
