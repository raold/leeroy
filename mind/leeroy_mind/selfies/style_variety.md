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

- slot 0 swimwear (bikini or swimsuit) at a water setting (not the hot tub),
- slot 1 gym wear at the gym,
- slot 2 a casual or street outfit (jeans, shorts, a sundress),
- slot 3 a going-out outfit (separates, a mini or cut-out dress; no cocktail or formal dress).

Each slot gets a different pose (from behind looking back over her shoulder, a three-quarter
rear view, side profile, front view), framed full-length or three-quarter (front and side are
always full-length), and its own colour and expression. The prompt puts the pose, the framing,
who she is, her build and the outfit with its setting at priority 0; the longest such set is
68 of the worker's 75 tokens with the real tokenizers. The negative is the lust one plus
`thong, g-string`. `screen_lust(..., varied=True)` is the only place swimwear may be framed
full-length or three-quarter and shot from behind; it keeps swimwear at the water, gym wear at
the gym and the hot tub swimwear-only, runs the age guard and her name screen on every composed
phrase (pose and framing included) and refuses a pose anywhere outside a varied set. The night's
photo-kind pass, `LUST_SCHEMA` and every existing prompt are unchanged.

Queue one for every current lust-styled member (`--dry-run` is the default and writes nothing):

```powershell
python -m leeroy_mind selfies vary --stance lust --data-dir PATH
python -m leeroy_mind selfies vary --stance lust --go --data-dir PATH
```

`--go` needs `enabled` and `varied_sets` on. With the mind stopped it holds the instance lock,
snapshots (`pre-vary`) and writes; while the mind runs it asks the mind's `selfie` op
(`{"action": "vary", "go": true}`, also reachable as `POST /api/selfie` from the console). It
writes what "4 more" writes (waiting sets passed, one `more` set queued) plus the set's plan
(meta `selfies:set-varied:<guid>:<set>`) and the day (`selfies:varied:day:<guid>`): once per
member per local day, and a member with a set already queued, or no renderable photo kind, is
skipped by code. Output carries guids, codes and list indices only.

The night's `selfies` stage (within `selfie_per_night`) and the booth render them under their
existing GPU and NSFW rules. A set renders varied only while the switch is on, she still has the
lust style and every look still passes the screens; otherwise it renders as an ordinary set of
her own kind and loses its plan. A varied set counts as a lust set for the pick rules. Her photo
kind and cover are unchanged; picking a varied candidate records its look
(`selfies:pick-look:<change>`), the gallery caption names each candidate's look, and the
situational photos then cut her face from that cover by its framing (`full_body` has its own
crop). Images stay square (`size`); full-length poses are not portrait-shaped.
