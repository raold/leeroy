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
unreadable file) turns it off and logs `drama_config_invalid key=<code>` once. It is read at
startup only. Off, no night stage is added, the storyteller forms no `flirt` or jealousy
confrontation, encounter prompts keep "no romance or flirting between them", and jealousy
writes no resentment or envy words. Migration `0049_member_romance.sql` adds `breakup`.

Three changes ship without the switch:

- **Scenes are judged for contradiction only** (`nightly/ground.py` `CONTRADICT_ONLY`,
  `FICTION_ASK`). A scene and its two memories are fiction: the judge rejects one only when its
  evidence contradicts it, or it breaks a rule. Invented detail the evidence does not mention
  passes. The people the scene prompt lists as "might be around" are really around, so the scene
  may name them (an `around:<encounter>` grounding item that the memories never cite). Every
  privacy rule, the deterministic name checks against anyone not around, and the one memory per
  participant rule are unchanged.
- **Rumours are third-person claims.** The scene's gossip carries a `claim`; a new rumour's
  canonical text is that claim, else the retelling, when it names its subject, quotes nobody and
  says no I or we (`gossip.third_person`). A telling with neither still reaches its hearer, with
  no rumour row. Existing live rumours are left to fade.
- **Jealousy no longer feeds on itself.** Its own memories (keys `jealousy:` and `envy:`) are not
  observations, so a saturated level comes down by `DECAY` a night when nothing new happens.

## What the switch adds

- **Romances.** A man and a woman of the cast who like each other (affinity 10 both ways) may
  spark (`couples` night stage, after `romance`): 15% a night, at most 2 a night, 0.15 of that for
  a woman drawn to or lusting after him. Never for someone whose household names a spouse or
  partner, someone already in a member romance, or a woman whose romance with him has begun
  (banter or later). Their relationship rows climb notices, banter, whispers (a crush),
  late_nights (dating), confession (together) through their scenes: each warm scene adds 8
  attraction both ways and may take one step once both rows meet the step's gate and its nights
  have passed; a cold scene takes 6, and short of a crush a romance under 15 attraction ends.
  Couples get a `flirt` volition; their scene prompt and both judges allow warm, never explicit
  flirting between the two of them only (`nightly/llm.py` `COUPLE_RULE`).
- **Drawn away.** On a night he pursued a woman with a member partner (an attraction act in the
  last 3 days), her attraction to the partner erodes by 6; once her attraction to him is 15 over
  her partner's (plus 10 dating, 20 together), she leaves. From a crush on that is a breakup:
  `breakup` row, both rows `ex`, the partner's hurt memory, distress and resentment, capped
  affinity and trust loss, his envy of the owner at 85 or more, a live rumour about her ("X left
  Y for him") spread by the partner, and by the partner's traits a feud with her. Before a crush
  she just drifts away. A woman whose stance toward him is none is never drawn away. A member
  never draws away a woman who is his.
- **Jealousy.** Rivalry at 70 or more gives the jealous member a `confront` volition toward her
  rival (ref `rule:jealousy:<rival>`), her view of the rival drifts by -2 a night, and a
  confrontation that goes bad sours her by up to -8 and the rival by half, under the night cap. A
  blow-up opens a feud once her own affinity is at -30 or below. Envy at 70 or more, fed tonight,
  writes the envious man a weekly memory that he means to tell the owner so (`envy:` key).
- **Live talk.** A card between two members shows their romance ("you're dating", "you broke
  up"), never on group turns.

Logs: `member_romance_night couples= sparks= breakups= drifted=`, `member_romance_step a= b=
stage=`, `member_romance_fizzled`, `member_breakup id= woman= partner= stage= feud= rumor=`.
The Social tab lists couples by stage and a Breakups panel (who left whom for him).
