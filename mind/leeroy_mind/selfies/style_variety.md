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
