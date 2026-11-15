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

`[selfies] variety = true` opts in, read once at startup; the default is false. Invalid tables cannot
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

**Her face in every new set** (`[selfies] identity_sets = true`, owner 2026-11-14; default false,
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

`[selfies] varied_sets = true` (default false; read at the mind's start; an invalid or
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
(`small`, `medium`, `large`, `very_large`, or not set) in the console's Personas member detail,
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

**Her new figure under a light face.** Measured on the GPU (2026-11-15), her cover's face
reference at `identity_scale` (0.6) carried her slim cover body into every set: 68 kg to 96 kg,
medium to very large, showed no change. So the set an edit queues, or the set already queued,
is marked (meta `selfies:set-looks-edit:<guid>:<set_no>`) and `render._set_faces` gives every
slot of it, front views included, her cover's face at the worker's floor `MIN_SCALE` (0.3): still
recognisably her, clearly fuller. The console says "A new set renders tonight with her new
figure; pick one as her photo to keep it": once he picks a cover from that set, later sets hold
that fuller cover at `identity_scale` as before.

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
3. her skin keeps its tone word and drops its modifiers;
4. her size keeps its first two terms ("plus-size, heavyset", "chubby, thick arms");
5. the kind, then the framing (a varied set: the framing, then the pose) yield to priority 1,
   keeping their places.

Her build (curvy, voluptuous, stocky, heavy and every other) and a set bust are never shortened;
her hair's length and colour and her skin tone are never dropped. A prompt still too long after
all five steps is left as it is (`prompt.over_fit` names it); none was found.

Measured with the worker's `fit.py` and the real tokenizers on a copy of a mind snapshot (145 cast
profiles; 16 varied candidates for each of the 91 lust women, 1456):

| | hair or skin lost | full build shortened | size words full / two terms | too long |
|---|---|---|---|---|
| main, profiles | 27 hair, 32 skin | (no size words) | | 0 |
| main, varied | 52 skin | | | 0 |
| no edit, profiles | 0 | 0 | 50 / 0 | 0 |
| no edit, varied | 0 | 0 | 160 / 0 | 0 |
| +25 kg and very large on the 91, profiles | 0 | 0 | 53 / 38 | 0 |
| +25 kg and very large on the 91, varied | 0 | 0 | 855 / 601 | 0 |
| BMI 38 and very large on the 91, profiles | 0 | 0 | 19 / 72 | 0 |
| BMI 38 and very large on the 91, varied | 0 | 0 | 449 / 1007 | 0 |

What gives way instead: with no edit, 42 of 145 profiles and 485 of 1456 varied candidates drop
their hair style, 3 and 33 their skin modifiers. With +25 kg and a very large bust, 4 profile
prompts lose their kind or framing phrase to the fit (25 of the varied candidates' framings or
poses). The looks pass is never asked for a bust. Negatives, the NSFW gate, the age guard and the
name screens are unchanged.

**An edit while her set renders.** A set is stamped with the persona version its prompts were
built from, not the one in force when it is published. If her looks changed while it rendered
(`render._stale`), a fresh looks-edit set is queued at once and renders from the persona after the
edit; the console says her set is rendering from her previous looks and a set with her new figure
follows it. A set that has started rendering (its `<set>.part` folder exists) is never marked for
the light face: its faces were chosen when it started. A failed check is logged
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
