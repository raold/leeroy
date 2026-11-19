# Heat

One dial for how far the women's romance toward the owner goes, in text, voice notes and photos.
Stance still decides *who* and how warm she is toward him; the `forward` dial still decides how
flirty her personality is; heat decides *how far*.

Everything reads one pure function, `heat.resolve()` (`resolve.py`), and the page explains a
value with `heat.explain_row()` / `heat.explain()`, built from the same table (`table.py`), so
the words on the page cannot drift from the behaviour.

## Switches

`[heat]` in `mind.toml`, read once at the mind's start (restart the mind, and the console, to
change it). An invalid table means off.

| Key | Default | What it does |
|---|---|---|
| `enabled` | `false` | The dial is available. Off: every reader takes the path it took before; stored heat rows do nothing; the tune op refuses heat in words. |
| `public_cap` | `false` | Once heat is set: cap every channel other people read at the flirty tier. Off is the behaviour as it was (the owner's 2026-10-30 "whatever channel" ruling). |

**On and not set is exactly today.** With `enabled = true` and no world heat stored, heat governs
nobody (`HeatLive.reaches`): the world romance dial, each woman's own dial, every clock and every
photo rate keep their exact effect, at whatever the romance dial is (the live one is 10, not 9).
Offsets stored before a world value wait for it. The page says "Heat is on but not set: the
romance dial (N) ... in force exactly as before", and its slider starts at the value that dial
stands for but never above 9 (today: the live dial 10 would be heat 10, which goes further than
the dial 10 does). `tests/heat/test_heat_neutral.py` holds it:
the full prompt over every stance, arc stage, channel, own dial (none, 1-10) and world dial
0-10, the clocks, the photo side and the dials the older readers take. A replay of the 500
newest captured turns (`bakeoff_turn`) plus turns to him from every woman with an attitude, on a
copy of the live database, composed the same prompts on and unset as on main (review of #373,
fix round 1).

**Heat takes over when the owner sets a world value.** Heat 9 is the wording from before the
dial: with the world set to 9 the tuning goldens are byte-identical
(`tests/heat/test_heat_golden.py`). What a set 9 changes against a live romance dial of 10: the
derived romance dial is 9 (the whisper lift is 1.4, not 1.5), and an explicit line is typed
rather than voiced (below).

The one thing that reaches a member while heat is unset is his own sentence or button for the
evening (the session override, below): an act of his, not a stored setting.

## The dial

Effective heat = clamp(world + her offset + her groups' offsets, 0, 10), then the session
override, then how far his own lines have led, then (with `public_cap`) the public cap.

- **World** 0-10. Once set, the romance dial the older readers take is derived from it
  (`[romance] dial` in `mind.toml` is never written, so clearing heat gives it back exactly).
- **Her offset** -5 to +5, women only.
- **A dial of her own** (`persona.romance.dial`, two women at 10 live) counts as her offset once
  heat is set: her dial minus 9, within -5..+5. So the world value reaches her too (world 0
  quiets everyone; she is then at 1), and her card reads "Heat 10 (world 9, hers +1 from her
  own dial)". A pinned offset, 0 included, replaces it. At world 9 she is as today: the wording
  and the every-turn rule are her dial's, and **the rates (how often she starts it, how often a
  photo is offered) follow only what the owner set** (world, a pinned offset, groups, the
  session), never that stored dial, so no x1.3 or x1.2 for her at world 9; the older readers
  (below) take the dial they took. Her full prompt at world 9 is the one without heat
  (`test_heat_neutral.py`, every stage and channel). Note that the offset
  is in heat steps: an own dial of 6 or 8 at world 9 is heat 6 (suggestive) or 8 (frank), where
  the old dial 6 was flirty and 8 suggestive; no live persona has such a dial.
- **Groups'** offsets add up.

| Heat | Wording (private, to him) | Wants | His lead | She starts it | Photos | Photo offers |
|---|---|---|---|---|---|---|
| 0 | none | flirt and reassurance off | never | never | P0 | x0.5 |
| 1-3 | warm | flirt x0.5 | after two of his flirts | x0.3 | P0 | x0.7 |
| 4-5 | flirty | as written | after one | x0.6 | P1 | x0.8 |
| 6-7 | suggestive | as written | after one | x0.8 | P2 | x0.9 |
| 8 | frank | as written | after one; late at night she may take one step herself | x0.9 | P3 | x1.0 |
| 9 | explicit | as before | as before | x1.0 | P4 | x1.0 |
| 10 | explicit | as before | none needed | x1.3; every private turn to him carries it (every turn to him for a woman at her own dial 10, as before) | P4 | x1.2 |

- The tier is a ceiling. Her stance and arc stage still set where she starts
  (`romance.ATTITUDE_REACH`, `STAGE_DEPTH`); a fond woman at heat 10 is still warm, and a woman
  with stance none, or a man, has no romance at any heat.
- **Frank** sits between suggestive and explicit: direct about wanting, nothing graphic. Its
  words are the owner's file; without one it reads the suggestive text (logged once).
- **His lead**: below 9 she is one tier under her ceiling until his own lines turn it up
  (`romance.his_heat`, counted for three hours, in memory). `[romance] realism` still answers
  him first at every heat.
- **She starts it** multiplies only the romantic share of the existing clocks (the whisper
  planners' lift, the FLIRT conversion of a social turn). No new clock. With heat set the lift
  itself also follows the derived romance dial, so the two stack.
- **Photo offers** multiply only the chance a turn to him offers a `him` photo. How many photos
  a week stays the `selfies` dial's.
- **Voice notes**: below heat 10, and off a no-limits evening, a line that is *itself* plainly
  explicit is typed instead of voiced (never dropped). The line is judged with its own screen
  (`guard.voice_hit`: a broader built-in list, `VOICE_RE`, of plainly sexual words about bodies,
  undressing and sex, plus everything the public guard catches), not the tier its prompt was
  worded at; every other line is spoken as before. The voice list is kept apart from the public
  guard's short one, so it never widens `public_cap`. His `public_block.txt` words count for
  both.
- **What else reads it** once heat is set, through one helper (`live.member_dial`): the learn
  coach's lesson screen and `learn/apply`, the outreach fill's "miss you", the bond's night,
  invitations and recall (the pipeline's bond memories). Each already reads the romance dial
  derived from the world heat; her heat can only lower that dial for her (her offset, her
  groups, a clean evening), never raise it, and at a heat with no romance wording (0) each
  leaves romance out. A raise (her own 10, a positive offset, an open evening) stays with the
  wording, voice and photos, so at world 9 the two women at their own 10 read what they read
  before. Unset, each reads the dial it always read (`tests/heat/test_heat_reach.py`).
- `resolve` is monotonic over its whole input space (`tests/heat/test_heat_property.py`):
  raising the world, an offset, a group, her own dial or his lead, opening the session, lifting
  the cap, going private or late never lowers any field; nor, while heat is unset, does raising
  the romance dial a session override's derived value stands for.

## Photo tiers (a proposal for the owner to edit)

`photos.py`. Every tier is a subset of the lists that already exist in `selfies/kind.py`, each
tier is a subset of the next, and **P4 is those lists exactly as they are**. Nothing is added: no
outfit, setting, pose, screen or negative. Import-time checks and
`tests/heat/test_heat_photos.py` keep the tiers a partition of the existing lists; moving a
string from one tuple to another in `photos.py` is the whole edit.

| Tier | Outfits added | Places added | Poses |
|---|---|---|---|
| P0 conservative | fitted blazer dress, cropped cardigan and wide-leg trousers, square-neck top and a pencil skirt, satin midi dress, fitted t-shirt and high-waisted jeans, flowy sundress | her living room couch, her hallway at home, a sunny window seat at home, a bookshop, a cafe terrace, a park in summer | front, side |
| P1 dressed | off-the-shoulder sweater dress, sweetheart-neckline sundress, off-the-shoulder top and jeans | a garden party, a city street at night, a ski lodge terrace | front, side |
| P2 going out | fitted little dress, low-cut cocktail dress, crop top and high-waisted shorts, bodycon mini dress, halter top and a miniskirt, tank top and denim shorts, backless summer dress, plunging wrap dress, one-shoulder top and leather trousers, midi dress with a high slit, sequin party dress, cut-out dress, ribbed tank dress, leather jacket over a crop top, silk blouse and a leather miniskirt | a rooftop bar at night, a wine bar, a nightclub, a dance floor, a hotel balcony at dusk | plus the three-quarter rear view |
| P3 gym | the gym outfits (at the gym only, as the pairings already say) | the gym | plus from behind |
| P4 swim (today) | the swimwear (at the water only, as the pairings already say) | the six water settings | all |

So the 22 dressed outfits split into 7 covered (P0 and P1) and 15 going out (P2). The places
follow main's photo audit (`selfies/safe.py`): the three it added (`NEW_SETTINGS`) are P0, and
the three it retired (the hallway mirror, the car's front seat, the concert crowd) are in no
tier, so a stored scene naming one reads P4 (fail closed). The audit's rules hold at every tier:
a selfie is waist-up and takes no pose, a varied set has `body_shot_share`'s number of body
shots below P4 as at P4 (never more), and heat's check on a kind runs after main's framing.

Where the tier is read:

- the night's photo-kind pass and the situational scenes (the lust reply schema's outfit and
  setting enums are narrowed; the pairings and every screen run on the reply as before), and
  with `[selfies] variety` the balanced pick (`style_variety`: its outfits and places as the
  tier allows);
- varied sets (`varied.compose`: the categories, outfits, settings and poses the tier allows);
- a photo read from his words (`words_scene`: swimwear only at P4, gym wear from P3, going-out
  wear from P2, else a casual outfit of her tier; fallback code `wearing:heat`);
- sending: a stored photo above her tier is **passed over**, never expired, hidden, deleted or
  rendered again (`live.sends`, `live.sends_tier`, in `photos/send.py` and
  `photos/request_stock.py`). A situational photo's tier is read from its stored scene
  (`photos.scene_tier`). A gallery photo's is read from the prompt it was rendered with (its
  outfit and place in the words of before or after the audit: `kind.OLD_TEMPLATES`,
  `safe.setting_words`) and its varied look's pose (`photos.prompt_tier`, `look_tier`), never
  from her photo kind of the day;
  the copy made for a request keeps that tier as a receipt (meta `photos:heat-tier:<snap>`), and
  the check at sending reads the receipt. So photos from before the dial are covered, and one
  reserved before heat was lowered is not delivered above the new tier. Raising heat makes the
  stock eligible again. Her cover and the gallery are untouched.

Only a woman with the lust photo style has a tier above P0, and that style is decided before
heat by `selfies/style.py` (stance lust, 25 or older, looks that pass the age guard). Everyone
else keeps the plain rules, which heat never reads.

## "Keep it clean tonight" / "no limits tonight"

`session.py`. Read by fixed patterns in his whisper or one-to-one DM to a member; no model call.

- clean: "keep it clean", "not tonight", "behave tonight": her heat is at most 2.
- open: "no limits tonight", "don't hold back tonight": her heat is 10.
- "back to normal" ends it.

It reaches the member he said it to; the Tuning page's buttons set either for everyone. It lifts
or lowers only the dial: never her stance (a woman with stance none and a man are left out),
the photo lists, the gate, the age rules or the public cap. She acknowledges it once, in her own
words. It works while heat is unset too (on a world value derived from the romance dial, for
that member only). Said in game it ends at his logout, and six hours after it was set whatever
happens (a mind stopped while he played and started with the worldserver down hears no logout).
Said in a Discord DM out of game (or set from the console then) it ends after six hours without
a line from him in that conversation or at 06:00 local; that idle clock is stored, so the
console and a restart read the same one. The first hello of a mind start with him offline
clears every one. It lives in meta (`heat:session:<guid>`, `heat:session:all`), not in the
change log. Logs: `heat_session guid= code= medium=`, `heat_session_ended reason= n=`, never
his words.

## Public channels (`public_cap`, off by default)

With `public_cap = true` and heat set, every channel that is not a whisper or his one-to-one DM
is flirty at most, in three places so one bug cannot undo it:

1. `resolve`: the cap is applied last;
2. `overlays`: the frank and explicit files are never opened for such a channel;
3. `guard.py`: a generated line aimed at him there with a plainly explicit word (a short
   built-in list, plus the owner's `public_block.txt`) is asked again once and never sent (tell
   `heat public`); an answer he is owed falls back to a clean generation, screened the same way.
   A line to another member, or to nobody, is not its business.

## The owner's words

Built-in text is non-explicit at every heat. Explicit wording lives only in his files; the repo
holds no content for them and no tool writes into the folder.

```
mind-data/prompts/
  romance_explicit.txt            existing: stays the explicit tier's text
  romance_explicit_whisper.txt    existing: stays the explicit whisper tail
  nightly_photo_lust.txt          existing: stays the photo pick's flavour
  heat/
    suggestive.txt                optional; else romance_suggestive.txt, else the built-in
    frank.txt                     optional; else the suggestive tier's text
    explicit.txt                  optional; if present it wins over romance_explicit.txt
    frank_whisper.txt             optional tail for the frank tier in a whisper
    explicit_whisper.txt          optional; if present it wins over romance_explicit_whisper.txt
    voice.txt                     optional; listed, not read by anything yet (see below)
    session_open.txt              optional: one line she reads once on a no-limits evening
    public_block.txt              optional: one word or phrase a line, never in public; also
                                  what counts as explicit for the voice rule
```

Same format as the existing overlays: plain text, a line of just `---` separates variants, each
capped at 200 estimated tokens. A missing file is never an error. With no file in `heat/` every
tier reads exactly what it read before the dial.

## Seeing and changing it

- Console, Personas tab, **Heat** panel: whether heat is set or the romance dial is still in
  force, the slider, the row for its value in words, what would change before he saves, whether
  public channels are capped, the photo lists the value allows (text, no render), tonight's
  override with its buttons, and each file by name, variant count and size (never its text). A
  member's detail shows "Heat 9 (world 9, hers +0)", her resolved lines and why anything is
  capped, her offset slider (and that her own dial counts as her offset) and her own session
  buttons.
- The `tune` op: `set` / `undo` / `revert` / `preview` with dial `heat` (scope `world`, `group`
  or `member`), and the action `heat` `{session: "clean" | "open" | "clear", guid?}`. A preview
  explains the value, lists the photo side and, while the live model is already loaded, renders
  one private line before and after. Behind the key, the Origin and `--no-control`, as every
  tune action.
- CLI, read-only: `python -m leeroy_mind tuning --data-dir D heat status`, `heat explain
  [--heat N] [--guid N]`, `heat overlays`.

## Storage

No migration. `tuning_value` rows with dial `heat`: a world row 0-10, a member's or a group's row
as an offset -5..+5, with the change log's undo and revert. Heat is not in `MEMBER_DIALS` or
`WORLD_DIALS`: it is not a persona dial (nothing is folded into the effective persona) and has
no key in `mind.toml`.

## What never moves

Enforced by construction and by `tests/heat`:

- Images stay clothed and non-explicit: the tiers only select among the vetted lists.
- The NSFW gate, the screens, the negatives and the age guard (`AGE_RE`, `SUGGESTIVE_MIN_AGE`
  25, `SEND_MIN_AGE`) are not read or written by anything here; the age rules are decided
  before heat is consulted.
- No real people; no name reaches an image prompt.
- Member-to-member scenes: heat is only about her talk with him.
- Privacy and audience rules, and the owed-answer rule: a clean cap never becomes a reason not
  to answer him.

## Rollback

Clear the world heat on the Tuning page ("Back to the romance dial"): heat governs nobody again
and the old behaviour returns exactly. Or set `[heat] enabled = false` and restart.
`[romance] dial` was never written. Before rolling back to code from before this package, remove
the `[heat]` table (older code warns on an unknown section); the stored `heat` rows are ignored
by it.

## Not built, or built differently from the design

- `heat/voice.txt` is listed but not read: a voice note is decided after the line is written, so
  there is no point before generation at which "say it differently" could be told.
- P0 on the lust-list surfaces is the most covered subset of the lust lists, not the plain
  style's own lists: a lust-styled woman's replies must be lust-shaped for the existing screens.
- The tier is read where a photo is newly composed and where one is sent. A set or scene already
  queued renders as it was composed. The `style_variety` restyle pass, and the `selfies` and
  `nightly` CLIs run with the mind stopped, compose from the lists as they are (no running mind,
  no heat).
- His lead and a session set inside a rolled-back Discord post are memory only.
- The resolved record is on the member's detail in the Personas tab, not on the Comms card.
- The doctor does not list the files; `tuning heat overlays` and the panel do.
