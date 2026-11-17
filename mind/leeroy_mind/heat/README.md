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
| `enabled` | `false` | The dial is in force. Off: every reader takes the path it took before; stored heat rows do nothing; the tune op refuses heat in words. |
| `public_cap` | `false` | Cap every channel other people read at the flirty tier. Off is the behaviour as it was (the owner's 2026-10-30 "whatever channel" ruling). |

**Heat 9 is the behaviour from before the dial.** With `enabled = true` and no heat set, the world
heat follows `[romance] dial` (9 is 9), so turning the switch on moves nothing until he moves the
dial. `tests/heat/test_heat_golden.py` holds that: the tuning goldens byte for byte with heat on,
and the romance text under a derived heat equal to the text at every legacy dial.

The one thing that differs at heat 9 with the switch on is the voice rule (his ruling 5): a voice
note speaks the frank tier at most, so a line worded at the explicit tier is typed.

## The dial

Effective heat = clamp(world + her offset + her groups' offsets, 0, 10), then the session
override, then how far his own lines have led, then (with `public_cap`) the public cap.

- **World** 0-10. Unset, it follows `[romance] dial`. Once set, the romance dial the older
  readers take is derived from it (`[romance] dial` in `mind.toml` is never written, so clearing
  heat gives it back exactly).
- **Her offset** -5 to +5, women only. A woman with a dial of her own (`persona.romance.dial`)
  keeps it until an offset is pinned for her: at world 9 the women at their own 10 show as +1.
  An offset of 0 is a real setting for her (it stands in for that stored dial).
- **Groups'** offsets add up.

| Heat | Wording (private, to him) | Wants | His lead | She starts it | Voice notes | Photos | Photo offers |
|---|---|---|---|---|---|---|---|
| 0 | none | flirt and reassurance off | never | never | plain lines only | P0 | x0.5 |
| 1-3 | warm | flirt x0.5 | after two of his flirts | x0.3 | as typed | P0 | x0.7 |
| 4-5 | flirty | as written | after one | x0.6 | as typed | P1 | x0.8 |
| 6-7 | suggestive | as written | after one | x0.8 | as typed | P2 | x0.9 |
| 8 | frank | as written | after one; late at night she may take one step herself | x0.9 | suggestive at most | P3 | x1.0 |
| 9 | explicit | as before | as before | x1.0 | frank at most | P4 | x1.0 |
| 10 | explicit | as before | none needed | x1.3; every private turn to him carries it | as typed | P4 | x1.2 |

- The tier is a ceiling. Her stance and arc stage still set where she starts
  (`romance.ATTITUDE_REACH`, `STAGE_DEPTH`); a fond woman at heat 10 is still warm, and a woman
  with stance none, or a man, has no romance at any heat.
- **Frank** sits between suggestive and explicit: direct about wanting, nothing graphic. Its
  words are the owner's file; without one it reads the suggestive text (logged once).
- **His lead**: below 9 she is one tier under her ceiling until his own lines turn it up
  (`romance.his_heat`, counted for three hours, in memory). Above it, `[romance] realism` still
  answers him first at every heat.
- **She starts it** multiplies only the romantic share of the existing clocks (the whisper
  planners' lift, the FLIRT conversion of a social turn). No new clock; chattiness, casual
  openers and outreach are untouched.
- **Photo offers** multiply only the chance a turn to him offers a `him` photo. How many photos
  a week stays the `selfies` dial's.

## Photo tiers (a proposal for the owner to edit)

`photos.py`. Every tier is a subset of the lists that already exist in `selfies/kind.py`, each
tier is a subset of the next, and **P4 is those lists exactly as they are**. Nothing is added: no
outfit, setting, pose, screen or negative. Import-time checks and
`tests/heat/test_heat_photos.py` keep the tiers a partition of the existing lists; moving a
string from one tuple to another in `photos.py` is the whole edit.

| Tier | Outfits added | Places added | Poses |
|---|---|---|---|
| P0 conservative | fitted blazer dress, cropped cardigan and wide-leg trousers, square-neck top and a pencil skirt, satin midi dress, fitted t-shirt and high-waisted jeans, flowy sundress | her living room couch, the mirror in her hallway, the front seat of her car, a cafe terrace, a park in summer | front, side |
| P1 dressed | off-the-shoulder sweater dress, sweetheart-neckline sundress, off-the-shoulder top and jeans | a garden party, a city street at night, a ski lodge terrace | front, side |
| P2 going out | fitted little dress, low-cut cocktail dress, crop top and high-waisted shorts, bodycon mini dress, halter top and a miniskirt, tank top and denim shorts, backless summer dress, plunging wrap dress, one-shoulder top and leather trousers, midi dress with a high slit, sequin party dress, cut-out dress, ribbed tank dress, leather jacket over a crop top, silk blouse and a leather miniskirt | a rooftop bar at night, a wine bar, a nightclub, a dance floor, a hotel balcony at dusk, a concert crowd | plus the three-quarter rear view |
| P3 gym | the gym outfits (at the gym only, as the pairings already say) | the gym | plus from behind |
| P4 swim (today) | the swimwear (at the water only, as the pairings already say) | the six water settings | all |

So the 22 dressed outfits split into 7 covered (P0 and P1) and 15 going out (P2).

Where the tier is read:

- the night's photo-kind pass and the situational scenes (the lust reply schema's outfit and
  setting enums are narrowed; the pairings and every screen run on the reply as before);
- varied sets (`varied.compose`: the categories, outfits, settings and poses the tier allows);
- a photo read from his words (`words_scene`: swimwear only at P4, gym wear from P3, going-out
  wear from P2, else a casual outfit of her tier; fallback code `wearing:heat`);
- sending: a stored photo above her tier is **passed over**, never expired, hidden, deleted or
  rendered again (`live.sends`, in `photos/send.py` and `photos/request_stock.py`). Its tier is
  read from its stored scene (`photos.scene_tier`), so photos made before the dial are covered.
  Raising heat makes the stock eligible again. Her cover and the gallery are untouched.

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
words. Said in game it ends at his logout; said in a Discord DM out of game (or set from the
console then) it ends after six hours without a line from him in that conversation or at 06:00
local. The first hello of a mind start with him offline clears every one. It lives in meta
(`heat:session:<guid>`, `heat:session:all`), not in the change log. Logs: `heat_session guid=
code= medium=`, `heat_session_ended reason= n=`, never his words.

## Public channels (`public_cap`, off by default)

With `public_cap = true`, every channel that is not a whisper or his one-to-one DM is flirty at
most, in three places so one bug cannot undo it:

1. `resolve`: the cap is applied last;
2. `overlays`: the frank and explicit files are never opened for such a channel;
3. `guard.py`: a generated line to him there with a plainly explicit word (a short built-in
   list, plus the owner's `public_block.txt`) is asked again once and never sent (tell
   `heat public`); an answer he is owed falls back to a clean generation, screened the same way.

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
    public_block.txt              optional: one word or phrase a line, never in public
```

Same format as the existing overlays: plain text, a line of just `---` separates variants, each
capped at 200 estimated tokens. A missing file is never an error. With no file in `heat/` every
tier reads exactly what it read before the dial.

## Seeing and changing it

- Console, Personas tab, **Heat** panel: the slider, the row for its value in words, what would
  change before he saves, whether public channels are capped, the photo lists the value allows
  (text, no render), tonight's override with its buttons, and each file by name, variant count
  and size (never its text). A member's detail shows "Heat 9 (world 9, hers +0)", her resolved
  lines and why anything is capped, her offset slider and her own session buttons.
- The `tune` op: `set` / `undo` / `revert` / `preview` with dial `heat` (scope `world`, `group`
  or `member`), and the action `heat` `{session: "clean" | "open" | "clear", guid?}`. A preview
  explains the value, lists the photo side and, while the live model is already loaded, renders
  one private line before and after.
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

Clear the world heat on the Tuning page ("Back to the romance dial"), or set
`[heat] enabled = false` and restart. `[romance] dial` was never written, so the old behaviour
returns exactly. Before rolling back to code from before this package, remove the `[heat]`
table (older code warns on an unknown section); the stored `heat` rows are ignored by it.

## Not built, or built differently from the design

- `heat/voice.txt` is listed but not read: a voice note is decided after the line is written, so
  there is no point before generation at which "say it differently" could be told. The voice
  rule itself (which tier may be spoken) is in force.
- A photo's tier is read from its stored scene instead of a separate meta receipt, so photos from
  before the dial are covered and nothing extra is written.
- P0 on the lust-list surfaces is the most covered subset of the lust lists, not the plain
  style's own lists: a lust-styled woman's replies must be lust-shaped for the existing screens.
- The tier is read where a photo is newly composed and where one is sent. A set or scene already
  queued renders as it was composed. The `style_variety` restyle pass, and the `selfies` and
  `nightly` CLIs run with the mind stopped, compose from the lists as they are (no running mind,
  no heat).
- The resolved record is on the member's detail in the Personas tab, not on the Comms card.
- The doctor does not list the files; `tuning heat overlays` and the panel do.
