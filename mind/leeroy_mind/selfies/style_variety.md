# Photo style variety

With the mind stopped, preview or apply choices using its own environment:

```powershell
python -m leeroy_mind selfies restyle --data-dir PATH --lust-only --dry-run
python -m leeroy_mind selfies restyle --data-dir PATH --lust-only --go
```

Omit `--lust-only` to include plain members. The default is dry-run. Both modes
hold the existing exclusive mind lock; neither starts a model or image worker.
Dry-run opens the database read-only and makes no migration or content writes.
`--go` first uses the startup snapshot helper to save the pre-migration database
under `snapshots/` (label `pre-restyle`). A failed snapshot stops the operation.
It atomically saves a `selfies:restyle:last` meta receipt containing the old kinds,
Lust choices and lighting, and clears selected plain kinds and their style records.
The night re-chooses plain kinds through its existing model and screens; a pending
receipt queues fresh photos only after the new kind is saved, even with variety off.
Lust changes queue fresh sets immediately. The gallery and chosen cover stay as they are until a later
pick. Appearance, identity and romance fields stay intact.

Hand-set kinds are protected through the existing photo-style digest receipt,
including hand edits followed by unrelated generated persona updates. A missing
or unreadable receipt is conservatively treated as a hand choice. Only the
explicit `--include-owner` option includes those members. This option authorizes
replacing photo choices, not appearances.

The assigner uses a deterministic seed from current persona versions and GUIDs.
It counts all cast photo choices, including protected members. New choices have
at most two uses of an outfit/colour pair and at most `floor(cast size / 8)` uses
of a setting (minimum one). It balances outfits, colours, expressions, kinds and
framings among feasible choices. Lust women use eight existing date-night
outfits and the existing non-water, non-gym evening/social settings, with upper
body or three-quarter framing. Plain members keep personal, model-written clothes
and settings; variety suggests gym wear and coherent crops, and excludes body-focused
three-quarter crops for young-coded women. Every composed assignment runs the
existing style and name screens. Manual restyle skips under-25 members with
`adult_floor`; the optional switch never changes the original plain pass's age selection.

The choice space is finite. When no safe choice remains under the caps, the
member is skipped with `no_safe_capacity`. Protected existing choices can already
exceed a cap; the report counts those conflicts rather than modifying them. One
command accepts at most 1,024 members. Dry-run reports GUIDs, fixed choice indices,
kind/framing codes, lighting indices and caps, never names or persona prose. The
indices refer to the lists in `style_variety.py` and `photo_extra.py`.

Six optional fixed phrases cover golden hour, neon, candlelight, city lights,
string lights and a rain-wet street. Free-text settings are classified by indoor/outdoor
and day/night cues; unknown categories get no light. Artificial
lighting removes the generic natural-light tail. The `photo_extra` row binds the chosen phrase
to a digest of the photo kind; a later hand edit invalidates it. Rendering adds
it at priority 9, the lowest priority in the existing 75-token fit. All other
phrases retain the original priorities, so lighting never makes them required.
Unknown or incompatible lighting
strings are ignored and the name filter still applies. Normal content and NSFW
gates remain in the existing renderer.

## Optional automatic night choices

`[selfies] variety = true` is the default, read once at startup. Invalid tables cannot
enable it. The lust pass uses the same balanced assigner. The plain pass still
asks its reflection model, adding up to eight recent outfits and eight settings
as quoted observations in user content. Its built-in system prompt is unchanged;
this is a preference hint, not a hard cap on a free-text model answer.

Manual restyles and enabled nightly saves record photo choices in private
`photo_style_history` with their recorded style, pruning entries older than 31 days on save.
The first enabled night or manual restyle seeds current kinds once. Plain hints
use only plain records in that window, exclude future timestamps, and run the
plain screens including all cast character and real names. Articles, case,
whitespace and garment colours are normalised before counting common outfits.
Hand-set or unrecorded kinds are labelled unknown and never become prompt hints.
As defence in depth, every plain hint excludes outfit and setting vocabulary from
the lust lists, even if an old record was labelled plain. The history
does not read chat or game activity, and neither it nor the lighting phrases are
logged. Turning automatic variety off preserves existing manually saved lighting
and queued work, while disabling new automatic assignments and history writes.

## Varied sets

**Her face in every new set** (`[selfies] identity_sets = true`, owner 2026-11-14; default true,
read at the mind's start; needs the selfie worker's `ipadapter` pin, and `yunet` for the tight
face crop). Profile sets used to render from text alone, so every candidate and every new set
was a different woman who fit the description. With the switch on, the night's `selfies` stage
and the photo booth start the identity worker, and a set for a member with a chosen photo carries
that photo's face (`store.face_pick`: her cover's newest front or side face, never a back view)
cut by its framing at `identity_scale` (0.6); a varied slot seen from behind (`BACK_VIEWS`) takes
it at half that (0.3, the worker's floor), or the adapter turns her to the camera. A member with
no chosen photo yet (her first set) renders plainly on the same worker. Log `selfies_set_face
guid= set= scales=`. Measured 2026-11-14 on Aima and Maristine's varied plans: the same woman as
her cover in every look, from-behind shots kept, her cover's lighting no longer tints the scene.

`[selfies] varied_sets = true` (default true; read at the mind's start; an invalid or
unknown-key table keeps it off) allows a varied set for each woman of the lust style: one set
whose four candidates each have their own look and pose, composed by code (`varied.py`) from
fixed lists in `kind.py` (`VARIED_LOOKS`, `VARIED_VIEWS`, `VARIED_FRAMINGS`), never by a model:

- slot 0 swimwear (halter bikini, one-piece, high-cut one-piece, sarong over a bikini; no string
  bikini) at a water setting (not the hot tub),
- slot 1 gym wear at the gym,
- slot 2 a casual or street outfit (jeans, shorts, a sundress),
- slot 3 a going-out outfit (separates, a mini or cut-out dress; no cocktail or formal dress).

Each slot gets a different pose (from behind looking over her shoulder, a three-quarter rear
view, side profile, front view) and its own colour and expression; at most one dress a set.
Front and side views and swimwear from behind are always full-length; other back views are
full-length or three-quarter. The negative is the lust one plus `thong, g-string`.
`screen_lust(..., varied=True)` is the only place swimwear may be framed full-length and shot
from behind; it keeps swimwear at the water, gym wear at the gym and the hot tub swimwear-only,
refuses swimwear from behind framed three-quarter, runs the age guard and her name screen on
every composed phrase (pose and framing included) and refuses a pose anywhere outside a varied
set. The night's photo-kind pass, `LUST_SCHEMA` and every existing prompt are unchanged.

The prompt puts the pose, the framing, who she is, her build and the outfit with its setting at
priority 0 (at most 60 of the worker's 75 tokens with the real tokenizers). Her hair, then her
skin, join them at priority 0 while the required set still counts at most 70 by the mind's
count (`tokens.py`: a measured word table, other words one token per two bytes; never below the
real count over every live and test looks phrase); otherwise they stay at priority 1, then the
expression (1), eyes and face details (2) and the phone look (2). Measured on the live lust cast
(91 women, 1,820 candidate prompts): the worst required set is 70 real tokens, and after the
worker's fit hair is kept 100%, skin 95%, the expression 77% and the eyes 62%. With the
RealVisXL tokenizer files on the machine (`LEEROY_SELFIE_DATA`), a test checks the word table and
every composable look against them. A slot the worker still finds too long (`prompt_too_long`)
renders her ordinary photo instead and loses its look; that is not counted as worker contention.

Try one first, with the mind stopped (renders into `scratch/selfies-try/<guid>/`, writes nothing
live, needs no photo kind and no model):

```powershell
python -m leeroy_mind selfies try --varied --guid GUID --data-dir PATH
```

Then queue (`--dry-run` is the default and writes nothing; it lists per member the set, its
looks as codes and how many sets waiting for his choice `--go` would pass):

```powershell
python -m leeroy_mind selfies vary --stance lust --guid GUID[,GUID...] --data-dir PATH
python -m leeroy_mind selfies vary --stance lust --go --data-dir PATH
```

`--go` needs `enabled` and `varied_sets` on. With the mind stopped it holds the instance lock,
snapshots (`pre-vary`) and writes; while the mind runs it asks the mind's `selfie` op
(`{"action": "vary", "go": true[, "guids": [...]]}`, also reachable as `POST /api/selfie` from
the console). It writes what "4 more" writes (waiting sets passed, one `more` set queued) plus
the set's plan (meta `selfies:set-varied:<guid>:<set>`) and the day (`selfies:varied:day:<guid>`):
once per member per local day, and a member with a set already queued, or no renderable photo
kind, is skipped by code. Output carries guids, codes and list indices only.

The night's `selfies` stage (within `selfie_per_night`) and the booth render them under their
existing GPU and NSFW rules. A set renders varied only while the switch is on, she still has the
lust style and every look still passes the screens; otherwise it renders as an ordinary set of
her own kind and loses its plan. A varied set the NSFW gate refuses is counted
(`selfies:set-varied-refused:<guid>:<set>`); on the second refusal its flagged slot is composed
again once as a full-length front view (`selfies_varied_recomposed`), and refused once more its
plan is dropped (`selfies_varied_dropped reason=refused`), so it renders as an ordinary set.

A varied set counts as a lust set for the pick rules. Her photo kind and cover are unchanged;
picking a varied candidate records its look (`selfies:pick-look:<change>`, which the gallery
shows for her cover), and the gallery caption names each candidate's look. The identity
reference of her situational photos, guild tiles and photo requests is the newest pick up to her
cover that shows her face: an ordinary candidate (cut by her kind's framing) or a varied front or
side view (cut by its own framing; `full_body` has its own crop), never a view from behind. The
snap row keeps the cover's pick, so `photo_changed` expiry is unchanged; with no such pick a
situation stays planned (`no_face_ref`), a guild tile leaves her out and a request falls back.
A varied candidate offered as a photo-request fallback records its own look. Images stay square
(`size`); full-length poses are not portrait-shaped.

## Weight and bust (owner edits)

The owner sets a member's weight (kg, the schema's 40-180) and, for a woman, her bust
(`small`, `medium`, `large`, `very_large`, C-H cup presets, or not set) in the console's Personas member detail,
under Looks. Saving goes through the running mind's `looks` control op (`POST /api/looks`,
behind the key, Origin and `--no-control` gates, under the photo changes' lock) to
`personas/looks_edit.py`: an owner persona version (source `owner`, so the variety pass leaves
her looks alone), a persona ledger row of kind `looks` (migration `0050_looks_ledger.sql`) and a
private receipt (meta `personas:looks-undo:<ledger>`). The ledger's Undo button, or
`personas undo <id>` with the mind stopped, puts both values and the prior source back while
they are still what the edit left. Each save or console undo queues a fresh profile set as
"4 more" does (sets waiting for his choice are passed and stay in her gallery): a varied set for
a woman of the lust style while `varied_sets` is on (the once-a-day rule does not apply), else an
ordinary one. A set already queued is not doubled: it renders from the persona as it is at render
time. With `[selfies] enabled = false` the looks save and no set is queued.

**C-H presets** (owner, 2026-11-19). The same Bust select also offers C cup, D cup,
E cup, F cup, G cup and H cup, stored as `c_cup` through `h_cup`. These are visual
presets, not exact measurements: clothing, pose, seed and the reference influence the result.
Bare cup letters produced nearly identical C and H images in the local comparison. The presets
instead use the renderer's body descriptions: C `medium bust`, D `full bust`, E `large bust`,
F `large full bust`, G `very large bust`, H `very large full bust`. Each is carried whole into
the profile, situation and requested-photo prompts, with the existing clothing and identity
requirements. They change only bust, not weight, waist, hips, outfit or romance level.
The 2026-11-19 local visual pass did **not** establish six distinct or progressively larger
results: both wordings overlapped heavily at two fixed seeds. A further C/H check without
the face reference also showed weak separation. These are prompt preferences, not reliable
size controls; inspect the queued images before choosing a cover. The tested reference used
a detected face crop, not a whole-body fallback. No production renderer or identity setting
was changed.
Saving still queues the usual set; select a
new cover to carry the changed proportions into later photos. The broad sizes and unset
retain their original prompts. No migration or new global switch is needed; no member is
changed until the owner selects a preset. Before rolling back to code without these values,
undo those edits or choose a supported broad size, since older schemas reject the new values.

**Her new figure under a light face.** Measured on the GPU (2026-11-15), her cover's face
reference at `identity_scale` (0.6) carried her slim cover body into every set: 68 kg to 96 kg,
medium to very large, showed no change. So the set an edit queues, or the set already queued,
is marked (meta `selfies:set-looks-edit:<guid>:<set_no>`) and `render._set_faces` gives every
slot of it, front views included, her cover's face at the worker's floor `MIN_SCALE` (0.3): still
recognisably her, clearly fuller. The console says "A new set renders tonight with her new
looks; pick one as her photo to keep it": once he picks a cover from that set, later sets hold
that fuller cover at `identity_scale` as before.

**Glutes, waist and hips** (owner, 2026-11-15: "the same adjustment for glutes, waist and
hips"). Three more selects beside the bust in the Looks panel, women only, owner-set only (the
looks pass is never asked; `schema.OWNER_LOOKS`), unset by default; an unset one leaves the stored
JSON and `looks_hash` as they were. They ride the same `looks` op (`glutes`, `waist`, `hips`, each
optional: one left out stays as stored), the same ledger kind and receipt (no migration), the
same Undo (a receipt from before them still undoes weight and bust) and the same light-face set.

| part | values and words |
|---|---|
| glutes | small "slim backside", medium "rounded glutes", large "large round glutes", very_large "very large round glutes" |
| waist | very_narrow "very narrow waist", narrow "narrow waist", average "average waist", wide "thick waist" |
| hips | narrow "narrow hips", average "average hips", wide "wide hips", very_wide "very wide hips" |

Waist and hips are said in place of the build's own words (curvy's "wide hips, narrow waist",
voluptuous's "wide hips") or after the build, like the bust; a curvy build with a wide waist drops
"hourglass" ("curvy figure, ..., thick waist"). From BMI 28 a slim, athletic or average build's own
words give way to the size and they stay as phrases of their own. Glutes are a phrase of their own:
required right after the build in a varied candidate seen from behind, where her face is mostly
hidden (`prompt.GLUTES_VIEWS`); from the side, where her face shows, priority 2 right after her eye
colour and the details that tell her apart, so they are said when there is room
(`prompt.GLUTES_SIDE`, controller ruling, 2026-11-15); elsewhere, where the framing shows her
figure (a front view, a three-quarter or full-length profile or situation), the last phrase of
priority 2 (review of #318: there it had taken their room); not said in a closer photo. None of these words is in a negative, `EXPLICIT_RE`, `WEAR_RE` or the age
guard.

The pose of a varied candidate ("from behind, looking over her shoulder", "side profile") is never
yielded (controller ruling, 2026-11-15, after the GPU: a back view without it renders from the
front). When a back look cannot fit after the other steps, the framing yields, then her
glutes, and last of all "from behind, looking over her shoulder" says "from behind"
(`prompt.POSE_SHORT`), still a view from behind. A look still over the limit is left for the
worker to refuse, and that slot renders her ordinary photo instead; that happens only in a
synthetic test case (a voluptuous woman of BMI 32 or more with every shape at its largest and long
hair and skin words, 2 of 458 looks at 76 tokens), never on the live cast.

Measured on a copy of the 09:50 snapshot with the worker's `fit.py` and the real tokenizers (145
cast profiles; 16 varied candidates for each of the 91 lust women, 1456):

| | pose lost | hair or skin lost | full build shortened | glutes required kept (from behind) | glutes said from the side | too long (real) |
|---|---|---|---|---|---|---|
| no edit | 0 | 0 | 0 | | | 0 |
| glutes very_large alone on the 91 | 0 | 0 | 0 | 726 of 728 | 42 of 364 | 0 |
| all four largest on the 91 | 0 | 0 | 0 | 701 of 728 | 14 of 364 | 0 |
| all four largest and 25 kg more on the 91 | 0 | 0 | 0 | 544 of 728 | 1 of 364 | 0 |

"From behind" stands in for the whole pose in 1 and 17 of those candidates. Glutes never cost her
eye colour or an identity detail where they are optional (side and front views, three-quarter
profiles): 0 in every scenario, against the same candidate without glutes. From behind, where they
are required, they take room from priority-2 details: an eye colour or identity detail is lost in
174 behind and 138 rear three-quarter looks of 364 each with glutes alone, 108 and 149 with all
four largest. Waist and hips are body words, required like the bust: with all four largest, a
side look loses a detail against main's prompt (bust alone) in 111 of 364, a front look in 103,
and 26 of 91 profiles; none of those is the glutes.

**The words.** Every profile, varied and situational prompt has the same required (priority-0)
set after its kind and framing: who she is ("36-year-old American woman"), her body
(`prompt.body_phrases`: her size by BMI, `prompt.size_words`, then the build's figure words with
her bust in them, `prompt.build_words`; never a man's bust), her hair, her skin (controller
ruling, 2026-11-15: hair and skin are never traded for body words), then her outfit and setting
where the kind has them.

| BMI | a woman | a man |
|---|---|---|
| below 18.5 | slender | very thin |
| 18.5-22 | slender | lean |
| 22-25 | (nothing) | (nothing) |
| 25-28 | slightly chubby | slightly chubby |
| 28-32 | chubby, thick arms | chubby, thick arms |
| 32-36 | plus-size, heavyset, thick arms, full round face | overweight, heavyset, thick arms, full round face |
| 36 and up | plus-size, heavyset, very full figure, full round face | very overweight, heavyset, full round face |

Size words never pull against the build: a curvy, voluptuous, stocky or heavy build below BMI 25
has no size word, an athletic one from 25 to 28 none either (muscle), and from BMI 28 a slim,
athletic or average build's own words give way to the size (her bust, when set, stays as its own
phrase). A woman is never said "very thin". A plain average build with no bust and no size word
stays optional after her looks, as before. The words count at most 14 tokens (measured on the real
tokenizers).

When the required set would count more than the worker's 75 (`prompt.fit_required`, by the mind's
own count, never below the real one), it shortens in this order (controller ruling, 2026-11-15,
after the GPU showed that a short "voluptuous figure" and a bare "plus-size" both rendered her
slim):

1. the kind and the framing take their short forms (`prompt.SHORT`: "candid photo", "headshot",
   "waist-up shot", "three-quarter shot, waist and hips in frame");
2. her hair keeps its length and colour and drops its style;
3. her skin keeps its tone, the first clause before a "," or " with " ("fair with faint
   freckles" says "fair skin"), or with no such separator its last word ("warm brown" says
   "brown skin");
4. her size keeps its first two terms ("plus-size, heavyset", "chubby, thick arms");
5. the kind, then the framing (a varied set: the framing, then the pose) yield to priority 1,
   keeping their places.

Her build (curvy, voluptuous, stocky, heavy and every other) and a set bust are never shortened;
her hair's length and colour and her skin tone are never dropped. A prompt still too long after
all five steps is left as it is (`prompt.over_fit` names it); none was found.

Measured with the worker's `fit.py` and the real tokenizers on a copy of the 08:14 mind snapshot (145 cast
profiles; 16 varied candidates for each of the 91 lust women, 1456):

| | hair or skin lost | full build shortened | size words full / two terms | too long |
|---|---|---|---|---|
| main, profiles | 27 hair, 32 skin | (no size words) | | 0 |
| main, varied | 52 skin | | | 0 |
| no edit, profiles | 0 | 0 | 50 / 0 | 0 |
| no edit, varied | 0 | 0 | 160 / 0 | 0 |
| +25 kg and very large on the 91, profiles | 0 | 0 | 57 / 34 | 0 |
| +25 kg and very large on the 91, varied | 0 | 0 | 987 / 469 | 0 |
| BMI 38 and very large on the 91, profiles | 0 | 0 | 23 / 68 | 0 |
| BMI 38 and very large on the 91, varied | 0 | 0 | 679 / 777 | 0 |

What gives way instead: with no edit, 42 of 145 profiles and 485 of 1456 varied candidates drop
their hair style, 3 and 33 their skin modifiers (on the live cast every one of the 74 multi-word
skins keeps its tone: "fair", "pale", "tan", "light"). With +25 kg and a very large bust, 2 profile
prompts lose their kind or framing phrase to the fit (17 of the varied candidates' framings or
poses). Her face details come in the order the fit should keep them: her eye colour, then what
tells her apart (glasses, facial hair, scars, tattoos, piercings), then the rest, before
"looking at the camera" (`prompt.identity_first`); with no edit no profile loses glasses or
facial hair that main keeps (29 members lose another detail, mostly an expression or posture).
The looks pass is never asked for a bust. Negatives, the NSFW gate, the age guard and the name
screens are unchanged.

**An edit while her set renders.** A set is stamped with the persona version its prompts were
built from, not the one in force when it is published. If her looks changed while it rendered
(`render._stale`), a fresh looks-edit set is queued at once and renders from the persona after the
edit; the console says her set is rendering from her previous looks and a set with her new figure
follows it. That rendering set is marked for the light face too: a render that started chose its
faces already, but one that fails or is cancelled (his login cancels the booth) renders again
from her new looks under the light face. A failed check is logged
(`selfies_stale_check_failed guid= set= error=`) and the stage goes on.

**Undo** puts back both values and the persona's source, and removes the `persona_looks_protection`
row the edit added (or puts back the one it replaced), unless a later owner change of her looks
holds it.

**What else an edit touches.** Her looks hash changes, so her stocked situational photos (made at
night on the GPU) expire `looks_changed` and the gallery hides them; an undo restores the hash but
not the expired photos (nothing is deleted). A very large bust with the lust swimwear outfits,
whose negatives let cleavage through, makes the NSFW gate refuse more candidates; a refused
candidate retries its seeds (`slot.TRIES`), so such a set costs more GPU time and can be refused.

Note for the first night after release: every non-average build moves to right after who she is,
and hair and skin become required in every profile and situational prompt (situational body words
move from priority 1 to 0). A member with a full build above BMI 25, or a slim or average build
outside 22-25 (athletic outside 22-28), gets a size word. Of the 91 lust women (measured
2026-11-15: curvy median BMI 21.3, range 19.6-24.8; voluptuous median 23.9, range 20.5-28.7), 81
get no size word; 9 voluptuous members get "slightly chubby" and one "chubby, thick arms". Raise a
weight in the Looks panel to make a body read heavier; that set renders under the light face.

## Situational photos: anatomy and the face on wide shots

The owner saw deformed hands, feet, limbs and eyes in situational and request photos
(2026-11-16). These are tall portraits (832 x 1216) with hands, arms and often legs in frame. Their
negative (`scene.SNAP_NEGATIVE`, `SNAP_NEGATIVE_LUST`: the profile `NEGATIVE` plus the one-person
lines) counted 61-67 tokens at priority 0, so the worker's 75-token fit always dropped its
priority-1 "deformed face, extra fingers, bad hands". Square profile sets are unaffected: their
`NEGATIVE` keeps that line.

**The anatomy line sits at priority 0.** `scene.SNAP_ANATOMY` ("extra limbs, extra legs, extra
arms, fused fingers, missing fingers, extra fingers, malformed hands, deformed feet, cross-eyed,
lazy eye, asymmetric eyes, mutated, disfigured") is required in both snap negatives
(`scene._snap_negative`). To make room the style list keeps "cartoon, painting, 3d render" at 0
(the lust style "cartoon, painting": her covered line is longer) and the one-person guard keeps
"another person"; "second face, duplicate person", "plastic skin, airbrushed, doll", "crowd, group
of people" and the period look move to priority 1, "anime, illustration, cgi" (lust: with "3d
render"), the screen and webcam guard and "watermark, text, logo" to 2. The covered line, minors and
likeness stay required. The required set counts 72 (plain) and 74 (lust) of the 75 on the real
tokenizers, so in practice nothing below priority 0 is kept. Guild tiles keep the old negative
(`scene.TILE_NEGATIVE`): a square at a desk with a monitor in frame, where "face on screen" must
stay required.

**The fit test.** `tests/selfies/test_snap_anatomy.py` fits both negatives with the worker's own
`fit.py` and RealVisXL's tokenizer files when they are on the machine (`LEEROY_SELFIE_DATA`, else
the checkout's `selfie-data`; CI has none and skips it, and pins both tuples exactly). A priority-0
addition that pushes either set over 75 fails there: the worker would refuse every situational
photo `prompt_too_long`.

**A lighter face on wide shots.** A scene framed `upper_body` or `three_quarter` takes her face
reference at `render.WIDE_SCALE` (0.75) of `identity_scale`, 0.45 at the default 0.6, never below
`MIN_SCALE` (`render.scene_scale`); `face` and `head_and_shoulders` scenes keep `identity_scale`.
The scene's framing decides it, not the reference's (whose framing still cuts the crop). It
applies to the night's snaps (`snapstage`), photo requests (`request_render`) and `selfies try
--snaps`; guild tiles keep `identity_scale`. Each render logs `selfies_snap_face snap= guid=
scale=` (a request adds `request=`).

**No hand or limb details.** A face detail naming her hands, fingers, thumbs, nails, wrists, arms,
legs, knees, ankles, feet or toes ("Rough fingertips", "Small scar on left thumb",
`scene.limb_detail`; crow's feet and a detail of her glasses such as "Glasses with taped arm" are
not) is left out of situational and request prompts: the fit keeps it at priority 2 and it pulls
the model's attention to the hands. Profile sets and guild tiles keep every detail. On the live
cast (2026-11-16, read-only) that is 84 details of 73 of the 169 stored personas, most of them
thumb scars, knuckles and nails.

Measured by the controller on the GPU (identity worker, 832 x 1216, the live seed and two more for
four stored snaps; counts are images with a hand, foot, limb or eye defect):

| variant | images | anatomy defect |
|---|---|---|
| live (old negative, scale 0.6) | 11 | 6 |
| anatomy line at priority 0, scale 0.6 | 11 | 1 |
| that, and 0.45 on wide scenes (shipped) | 11 | 0 |

Her likeness held at 0.45 in all but one seed. 40 steps at CFG 6.0 (slower, more NSFW-flagged)
and a "hands visible" positive phrase did no better.

**Open: waist-up scenes still render full length.** A scene framed "waist-up shot" often renders
her full body in the tall portrait: #316's body phrases (her build and size, required) plus 832 x
1216 pull the camera back. Untested fixes: a `three_quarter` crop hint in the framing phrase, or
an 896 x 1152 portrait.

## Hair, wardrobe, makeup and accessories (owner, 2026-11-17)

The Looks panel has three groups, **Body** (weight, and for women bust, glutes, waist, hips),
**Hair** and **Style**, for women and men. Every new value is an owner-only `Appearance` field
(`schema.OWNER_STYLE`; no model is asked for one, `enrich.ENRICH_SCHEMA` leaves them out), unset by
default; unset leaves the stored JSON, `looks_hash` and every prompt exactly as they were. One
Save sends everything through the same `looks` op (each new argument optional: one left out stays
as stored) and makes one persona version, one `looks` ledger row with its Undo, and one queued set
marked `selfies:set-looks-edit:<guid>:<set>` (face scale 0.3), as #316. No migration.

| field | values | in the prompt |
|---|---|---|
| `owner_hair_colour` | black, dark_brown, brown, light_brown, auburn, red, copper, strawberry_blonde, dark_blonde, blonde, platinum, grey, silver, pastel_pink, blue, purple | said in place of the looks' own hair colour text |
| `owner_hair_length` | buzzed, short, chin, shoulder, long, very_long | in place of the looks' own length (a shaved head stays one until a length is set) |
| `owner_hair_style` | straight, wavy, curly, coily, ponytail, high_bun, messy_bun, braid, braids, bob, pixie, bangs, half_up, slicked_back, shaved_sides; a man also crew_cut, fade, quiff | in place of the looks' own style text |
| `wardrobe` | up to 4 of casual, sporty, smart, edgy, boho, glam, cosy, workwear, vintage, outdoorsy, minimalist, swim | none: a bias on the outfits picked (below) |
| `palette` | up to 3 of the 12 lust colours | none: the colours picked |
| `makeup` (women) | none, natural, defined, glam | "no makeup", "natural makeup", "defined makeup", "glam makeup" |
| `jewellery` | none, minimal, statement | "delicate necklace" / "statement jewellery" (a man: "chain necklace" / "chunky jewellery"); none adds a negative |
| `glasses` | never, always | never leaves out the face details that name glasses and adds a negative; always says her own glasses detail first, or "glasses" when the looks name none |
| `tattoos` | none, small, sleeve | "small tattoo" / "tattoo sleeve", said in place of the clauses that name a tattoo; none leaves them out and adds a negative |
| `piercings` | none, ears, nose, lip | "pierced ears" / "nose piercing" / "lip piercing", the same way |

**Hair overrides, it never rewrites.** The stored free text (`hair_colour`, `hair_length`,
`hair_style`) is untouched; `prompt.hair_words` says each owner value in its place. Hair's length
and colour stay required in every profile, varied and situational prompt and the style still gives
way first (`hair_skin_short`), so the 75-token fit holds: the enum words are shorter than long free
text was (tests/selfies/test_looks_style.py, the longest combination on every build, kind and
framing). The words were counted on RealVisXL's tokenizers on 2026-11-17 (`tokens.WORD_TOKENS`).
No value or word is one of the age guard's (no pigtails, no bunches); the lust style still reads
the stored free text (`style.looks_age_coded`), so overriding an age-coded stored style does not
bring her back into the lust style.

**Accessories keep what is there.** Other face details (a beard, a scar, freckles) are always
kept; `prompt.face_details` only touches the clause of a detail that names glasses, a tattoo or a
piercing, and only when the owner set that field ("a faded tattoo and a scar on the chin" keeps
the scar).

**Accessories get room first** (review of #358: at priority 3 and 2 the worker's fit dropped them
in almost every prompt). What the owner set is said in one or two tokens each
(`prompt.accessory_phrases`) at priority 1, right after her hair and skin, in the order glasses,
tattoo, piercing, makeup, jewellery, so the worker's fit offers them room before every other
optional phrase. `fit_required` takes their size as a `reserve`: the required set shortens for it
(the kind and framing's short forms, her hair's style, her skin's modifiers, her size's last
terms), then the kind of photo (a profile, a situation) or a varied front or side view's framing
moves to priority 2. Her body, hair length and colour, skin tone, a profile's framing and a
varied pose never make way. With nothing set the steps are exactly what they were. Checked with
the worker's own `fit` on RealVisXL's tokenizers (tests/selfies/test_looks_style.py): a voluptuous
woman of 88 kg with a large bust keeps her glasses and tattoo in every profile, her lust profile,
every varied front and side look and a situational photo; a curvy woman keeps all five. Known
limits, which the Looks panel states: a varied **back view** yields nothing for them, so on a full
figure they may not show there, and with a full figure and the longest outfit or activity the last
of them may not fit (glasses, the first, do).

**Negatives.** Each "never"/"none" is its own one-word negative phrase at priority 3
(`style_negative`: glasses, tattoo, piercing, jewellery), added only where the worker's fit has
room for it beside the fixed negatives, which keep every place: two words on the plain negative,
three on the lust one, none on a varied set's (it fills the window; there only the face-detail
removal applies). A word with no room is left out by the mind, never sent to be dropped.
Situational photos' negatives are unchanged.

**Shaved heads.** Looks that say a shaved head have no colour or style worth saying, so over them
only the owner's values count: the mind refuses a colour or style without a length, and a length
without a colour.

**Wardrobe and palette bias the kind, not the looks** (`selfies/wardrobe.py`). They put no word
in a prompt.
- *Photo-kind pass.* A lust woman's schema keeps its shape with shorter enums: the lust outfits
  her tags name (`LUST_TAGS`, every one from the existing vetted lists) and her palette's colours.
  A narrowed outfit list also narrows what the pairings would refuse, so at least half of the
  picks left are valid for every tag subset (`wardrobe.MIN_SHARE`; the full lists stand at 76%):
  only swimwear left gives the water settings, upper body and no headshot; swimwear among other
  outfits loses the hot tub and keeps as many swimsuits as the share allows; no swimwear loses the
  hot tub. Everyone else's free-text ask gains one sentence of fixed words (`ask_hint`); the reply
  meets the same screens. A kind the pass chose is chosen once more when the wardrobe or palette
  changes (the kind's record keeps the digest of the wardrobe the pass read; restyle, variety and
  world photos record the stored one), and her queued ordinary sets wait for that
  (`selfies_set_held reason=wardrobe`; the Looks panel says so). **One refused re-choice ends the
  wait**: her kind stays, the record takes the wardrobe as attempted, `selfies_wardrobe_released
  guid= reason=refused` is logged and her sets render with the kind she has. A kind set by hand
  is never chosen again.
- *Varied sets.* Still one look of each category; within a category the outfits her tags name
  come first, and the colours are her palette's. A varied set reads no photo kind, so it never
  waits for the kind's re-choice.
- *Situational scenes.* The lust scene's enums as above. A plain scene offers the outfits her
  tags name, with seven clothed outfits added for the tags the old four did not cover
  (`SCENE_MORE`, offered only to a member whose wardrobe names them) and the gym's own always.
- *Swim* counts only for a woman who has the lust style now (the panel offers it only to her and
  the mind refuses it for anyone else),
  and only through the existing pairings: swimwear at the water.
Photo requests (`words_scene.py`) take the hair and accessories, not the wardrobe.

**Rollback hazard.** Older code refuses a persona whose looks carry any of these fields (unknown
`Appearance` key: the member's persona does not load). Before rolling back, with the mind stopped:

```bash
python -m leeroy_mind personas --data-dir "$D" looks --clear-style          # the plan: who, which fields
python -m leeroy_mind personas --data-dir "$D" looks --clear-style --go     # snapshot, then clear them all
python -m leeroy_mind personas --data-dir "$D" looks --clear-style --guid 1385,1895 --go   # only these
```

Each cleared member gets an owner persona version and a `looks` ledger row (`personas undo <id>`
puts the values back after rolling forward again); weight and figure stay. Undo by hand works
too, newest row first (an older row is refused once a later edit moved the same looks). A member
is clear when her stored persona's `appearance` has none of the keys `owner_hair_*`, `wardrobe`,
`palette`, `makeup`, `glasses`, `jewellery`, `tattoos`, `piercings`; a second `--clear-style`
plan then lists nobody.

## Compositions the photo audit designed out (2026-11-17)

**Partly reverted by the owner (2026-11-19)**; see "What the owner put back" at the end of this
section. The table below is the audit's change as made; its mirror, car-seat, concert, pose and
negative rows no longer hold where that section says so.

A visual audit of about 1,290 rendered photos (145 covers, 55 situational photos, the newest two
sets of every member) tied each defect to the composition behind it. 8% were defective (severity
2 or more), 1% badly (a third hand, an extra arm, the subject rendered twice, three feet).
Headshots were clean (1%); defects grew with how much body shows (waist-up 12%, three-quarter
11%, full-length 21%) and with what the hands touch (no hand in view 2%, one or more 12%).
`safe.py` holds what changed; the keys the model picks and the stored kinds and plans did not.

| trigger (defective share in the audit) | what changed |
|---|---|
| mirror selfies of the lust profile kinds (27% of 64, half a wrong or second reflection; plain head-and-shoulders mirror selfies 3% of 92) | the lust style is no longer offered a mirror selfie or the hallway-mirror setting; a stored lust one renders as a selfie in `her hallway at home`. A plain mirror selfie stays one, and so does one he asks for in his words (`scene_from_words`): there the negative names neither the mirror nor the phone, and the caption still says "a mirror selfie" |
| a phone held in view (doubled, oversized, back to the camera) | a selfie is said `selfie, close arm's-length shot` (short form `selfie`), a timer photo `photo taken on a timer`; `mirror, phone` required in the negative wherever no mirror is rendered |
| the car's front seat (the subject cloned as a passenger), the concert crowd (a frame full of faces) | retired; stored ones render as a quiet street beside her parked car and a festival field |
| "bike shorts" at the gym (38%: a garbled bicycle every time) | the outfit says gym shorts; the exercise-bike activity is "resting after a workout" |
| a hand on the hip plus another arm action, hands on rails, glasses, mugs, pens, sticks | a three-quarter or full-length photo requires one of 3-4 simple poses per view, drawn by seed (`safe.POSES`: standing or walking, arms at her sides, hands behind her back or clasped in front; none on a hip, none seated); a waist-up snapshot asks for `arms down, hands out of frame`; a selfie takes no pose at any framing (an arm holds the camera, so "arms at her sides" would contradict it) and a lust selfie kind is waist-up (`safe.selfie_framing`: the three-quarter mirror selfie was the audit's worst cell, 38%), chosen so and, stored three-quarter before, rendered so; props rest on the table and activities are said as what is there, not what the hands do |
| seated and splayed swimwear poses (merged legs and feet) | no pose sits or lies |
| busy places (2 or more people in frame: 17%, and most of the severity 3) | `another person, duplicate person` required in the body negative; a garden party is a summer garden, a dance floor an empty one; `alone, no one else in frame` at priority 3 |
| garbled signs and equipment lettering | `text, logo` among the first optional words of the body negative |
| men rendered shirtless, bare shoulders with no top | the outfit sat at priority 2 and never reached the worker's 75 tokens (0 of 12 waist-up prompts kept it, 7 of 42 men's); it is now required as `wearing ...` wherever shoulders show (a close-up of the face keeps it optional), a top and for a waist-up or wider photo a bottom, a man always a shirt (`safe.complete`: the default top goes first, "a plain t-shirt, a rain jacket and jeans"); an outfit too long for the window, or one that leaves the accessories no room, gives way to default garments (`prompt.fit_clothed`); free text naming swimwear or underwear is never required |
| one strap, fused sleeves, a top with no bottom | every list outfit (the wardrobe's scene outfits too) names a top and a bottom or a one-piece with its length; the one-shoulder top is a wrap top; `bottomless, shirtless` required in the body negative |

**One fit order** (`prompt.fit_clothed`, with #358's `reserve` and `give`). Required: who she is,
her body and size, her hair's length and colour, her skin tone, what she wears, the framing and
a body shot's pose. Then what the owner set her to wear (the first optional phrases, with room
reserved). Then her hair's style, eye colour and the other details. To make room, in order: the
short forms and her hair's style, skin's modifiers and size's last terms; for the accessories'
reserve the kind of photo, then the pose, move behind them; for the worker's limit the kind, the
pose and the framing yield. A request from his words (`words_scene`) puts what the owner set her
to wear first among its optional phrases, then the moment, then a timer photo's pose; its selfie
framing takes its short form (`selfie`) in the first step of the fit, before an accessory or her
hair's style would go, and never yields.

Measured on the live cast (145 members, a copy of `mind.db`) with the real tokenizers and the
worker's fit, read-only; this work / main (requests: the longest work uniform and the casual
outfit, at a staff bathroom, so two prompts a member and framing):

| prompt (as stored, 2026-11-18) | n | too long | `wearing` kept | pose kept (whole form) | hair style kept | eye colour kept |
|---|---|---|---|---|---|---|
| lust profile, three-quarter snapshot or headshot | 54 | 0 | 54 / 54 | 54 (0) | 17 / 27 | 49 / 46 |
| lust profile, stored three-quarter selfie (renders waist-up) | 8 | 0 | 8 / 8 | none (a selfie) | 4 / 2 | 7 / 6 |
| lust profile, waist-up | 30 | 0 | 30 / 30 | none (selfies) | 22 / 21 | 25 / 24 |
| plain, head and shoulders | 44 | 0 | 44 / 0 | n/a | 44 / 44 | 41 / 44 |
| plain, waist-up | 9 | 0 | 9 / 0 | 8 (8) | 8 / 9 | 7 / 9 |
| varied, the two back views | 184 | 0 | 184 / 184 | 175 (28) | 71 / 94 | 90 / 127 |
| varied, front / side | 92 / 92 | 0 | all | 92 / 92 (33 / 30) | 58 / 70, 59 / 66 | 61 / 74, 55 / 72 |
| request, handheld selfie | 290 | 0 | 290 / 290 | none (a selfie) | 258 / 271 | 211 / 245 |
| request, mirror selfie | 290 | 0 | 290 / 290 | none (a selfie) | 256 / 271 | 230 / 245 |
| request, timer | 290 | 0 | 290 / 290 | 158 (158) | 233 / 237 | 197 / 225 |
| request, from behind | 290 | 0 | 290 / 290 | n/a | 219 / 237 | 207 / 225 |

A body shot's pose takes its short form ("standing", "walking", "walking away": `safe.POSE_SHORT`)
with the other short forms, before her hair would lose its style. The hair style and eye colour
still kept less often than on main are the clothed rule's words: a dress's length, a jacket's
top and bottom and a uniform's trousers (every request prompt that loses the hair style where
main keeps it, 9 to 18 per framing, is the longest work uniform's, whose real count stays far
below 75 but whose count by the mind's upper bound crosses it).

With every accessory set on every member (glasses always, a tattoo sleeve, a nose piercing,
defined makeup, statement jewellery; a request leaves out the tattoo sleeve, a limb detail), all
of them keep their room in, this work / main: requests handheld 258 / 241, mirror 252 / 241,
timer 211 / 160, from behind 200 / 160 (of 290 each); lust profiles waist-up 26 / 26 and
three-quarter 27 / 32 of 54; varied back views 49 / 62 of 184, front 86 / 87, side 86 / 86; plain
profiles 53 / 53. The profile and varied gaps are the outfit and setting words (the clothed rule's
price, one to three required tokens).

**The body negative.** A photo that shows the body (waist-up or wider; every situational one)
takes `prompt.body_negative`. Required: the covered line, minors, likeness, the anatomy line
without its eye words (no eye defect at severity 2 or more in 1,288 photos) and with `extra
hands`, `another person, duplicate person`, `bottomless, shirtless`, and `mirror, phone` unless
a mirror is rendered (a varied set: `mirror` alone). Required sets: 61 tokens (plain), 67 (lust),
71 (varied). Then the optional words in the order the fit offers them room
(`safe.BODY_OPTIONAL`), and the owner's "never" words ahead of them (`prompt.negative_for`): all
four have room in the plain, lust, mirror and situational body negatives, the first two (glasses,
tattoo) in a varied set's (main: none there). A head-and-shoulders profile negative keeps them
where they have room beside its required `shirtless, mirror, phone`: one (glasses), where main's
profile negative kept two. No stored persona sets a "never" or "none" today.

| negative | optional words kept | dropped |
|---|---|---|
| plain body, situational | `plastic skin`, `airbrushed, doll`, `text, logo` | `deformed face, bad hands`; `3d render, anime, cgi`; `second face, crowd, group of people`; `vintage, retro, film grain, sepia, old photo`; `illustration, watermark` |
| lust body, lust situational | `plastic skin`, `airbrushed, doll` | the above and `text, logo` |
| varied | `plastic skin` | the above and `airbrushed, doll` |

The old profile negative kept `plastic skin, airbrushed, doll, deformed face, extra fingers, bad
hands` and the vintage line. `bad hands` and `extra fingers` are covered by the required anatomy
line (`malformed hands`, `extra fingers`, `fused fingers`). `deformed face`, the render styles
and the vintage line are what a body shot loses: compare on the GPU (same seeds, old negative
against the new) before trusting it.

**`[selfies] body_shot_share`** (0-1, default 1.0; read at the mind's start): the share of a
varied set's four candidates that show her figure (below 1.0 the views from behind keep theirs
first, then the front, then the side; the rest are waist-up) and of the lust profile kinds that
are three-quarter (else waist-up); since 2026-11-19 (owner) a lust selfie follows the share like
any other kind (the waist-up rule that the audit's 38%-defective three-quarter mirror selfie
prompted is lifted; a selfie still takes no pose). 1.0, four body views, is the owner's ask (#307) and what the
labels support: waist-up was no safer than three-quarter (12% against 11%). Front and side
candidates are three-quarter rather than full-length (planned three-quarter 6% defective,
full-length 12%).

**`[selfies] snap_body_share`** (0-1, default 0.5): the share of situational photos that keep a
waist-up or wider framing; the rest are head and shoulders (29% of the waist-up ones were
defective, none of the closer ones).

**`selfies audit --labels DIR`** reads an audit's label files (JSON lines: guid, path, severity,
same_person_as_cover) and prints, as guids, set numbers and photo ids, the covers in force and
the stocked situational photos they mark defective. With `--go` (the mind stopped; a snapshot
first) those stocked photos end `flagged` (rows only, no file is deleted) and a fresh set is
queued for each member with a defective cover. A cover is never changed: the owner picks.

Not verified on the GPU: every wording here was chosen from the audit's numbers and the token
counts, not from new renders. The labels carry no pose, so the poses avoid what failed rather
than repeat what worked. Check the first night's sets and snaps.

### What the owner put back (2026-11-19)

On the owner's instruction four of the audit's composition choices are as they were before it:

- **The three lust settings.** `the mirror in her hallway`, `the front seat of her car` and `a
  concert crowd` are on `kind.LUST_SETTINGS` again (24 settings: the audit's three plain places,
  `safe.NEW_SETTINGS`, stay), and so in the lust schema, the variety balancer's date settings and
  `selfies variety`'s re-roll. The lust situational schema takes the car seat again; the hallway
  mirror and the crowd it never offered, before the audit too. `safe.SETTING_WORDS` no longer
  rewrites them (a garden party, a dance floor and the gym keep their audit words). Heat gives
  them the tiers they had before: the hallway mirror and the car seat P0, the crowd P2.
- **Mirror selfies on the lust lists.** The lust schema, the lust situational schema, the
  variety balancer and the re-roll offer `mirror_selfie` again, and a stored lust mirror selfie
  renders as `mirror selfie` (`safe.kind_of` is gone), with a negative that names neither mirror
  nor phone (`HEAD_NEGATIVE_LUST_MIRROR`, `BODY_NEGATIVE_LUST_MIRROR`; the situational one was
  there already). A selfie of any kind still takes no body pose.
- **The poses.** `safe.POSES`, `POSE_SHORT`, `POSE_WAIST_UP`, `pose` and `pose_priority` are gone:
  no profile, varied, situational or request prompt carries a standing or waist-up pose, and a
  varied candidate's pose is its view again, as before the audit (the view never yields; the
  framing yields first). A privately styled scene keeps its own poses (`romance_styling`).
- **The quality words in the negatives**, at their priorities before the audit:
  - profile body shots and varied sets (`prompt.body_negative`): the style words required again
    as the profile negative says them (`safe.BODY_STYLE`: "cartoon, anime, painting, illustration,
    3d render, cgi"); `deformed face, bad hands`, the vintage line and `second face, crowd, group of
    people` at 1, `watermark` at 2;
  - situational and request photos (`scene._snap_negative`): the earlier anatomy line with its
    limbs, fingers and eye words required (`extra limbs, ..., cross-eyed, lazy eye, asymmetric
    eyes`), `3d render` required for a plain photo and at 2 for a lust one, `anime, cgi` and
    `illustration, watermark` at 2, the second face and the vintage line at 1.

**What did not change**, deliberately: every negative keeps its covered line required (`nsfw,
nude, cleavage, lingerie, swimwear`; the lust line `nsfw, nude, topless, lingerie, underwear,
see-through, sheer`, `_COVERED` / `_COVERED_LUST` taken directly, never through `NEGATIVE`),
`child, teenager`, `celebrity, famous person` and `bottomless, shirtless`; the clothing
requirement (`wearing ...` required, completed to a top and a bottom, `prompt.fit_clothed`,
`safe.complete`, free text naming swimwear or underwear never required), the age guard, the
scene screens, the NSFW gate and the pairings.

**The 75-token window.** With the style or anatomy words back at 0 beside the audit's required
lines, some sets would count over the worker's 75 and every such photo would be refused. So the
audit's lines the owner did not name move to priority 1, first among the optional phrases, where
they no longer fit: `mirror, phone` in the lust body negative, `another person, duplicate
person` in the varied one, and `extra hands`, `duplicate person`, `mirror, phone` in the
situational ones (`another person` stays required there, as before the audit). In the
situational negatives `bottomless, shirtless` stays required too, which leaves one part of the
revert undone: `mutated, disfigured` waits at 1 (first among the optional phrases); with it the
required sets would count 77 (plain) and 79 (lust).

Measured on the real tokenizers with the worker's fit:

| negative | required | optional words the fit keeps (no "never" words set) |
|---|---|---|
| plain body | 71 | `plastic skin` |
| mirror body | 67 | `plastic skin`, `airbrushed, doll` |
| lust body, lust mirror body | 73, 73 | none |
| varied | 74 | none |
| situational plain / mirror | 71 / 71 | `extra hands` |
| situational lust / lust mirror | 73 / 73 | none |
| head (plain, lust, mirror, lust mirror) | 38, 44, 34, 40 | the realism and vintage lines (and the watermark line beside the mirror one) |

So on body shots most of the quality words at 1 and 2 are still dropped by the window, and the
owner's "never" words (`prompt.negative_for`, ahead of the optional phrases) have less room than
under the audit's negatives: all four in the mirror body negative, glasses and tattoo in the plain
body and situational ones, glasses alone in the lust ones, none in the varied one (the audit's:
all four, and two in the varied one). On 2026-11-18 no stored persona set one (#362).
