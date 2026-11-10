# Photo style variety

With the mind stopped, preview or apply choices using its own environment:

```powershell
python -m leeroy_mind selfies restyle --data-dir PATH --lust-only --dry-run
python -m leeroy_mind selfies restyle --data-dir PATH --lust-only --go
```

Omit `--lust-only` to include plain members. The default is dry-run. Both modes
hold the existing exclusive mind lock; neither starts a model or image worker.
Dry-run opens the database read-only and makes no migration or content writes.
`--go` atomically saves photo choices, their lighting and style receipts, and
queues fresh sets. The gallery and chosen cover stay as they are until a later
pick. Appearance, identity and romance fields stay intact.

Hand-set kinds are protected through the existing photo-style digest receipt,
including hand edits followed by unrelated generated persona updates. A missing
or unreadable receipt is conservatively treated as a hand choice. Only the
explicit `--include-owner` option includes those members. This option authorizes
replacing photo choices, not appearances.

The assigner uses a deterministic seed from current persona versions and GUIDs.
It counts all cast photo choices, including protected members. New choices have
at most two uses of an outfit/colour pair and at most `ceil(cast size / 8)` uses
of a setting (minimum one). It balances outfits, colours, expressions, kinds and
framings among feasible choices. Lust women use eleven existing date-night
outfits and the existing non-water, non-gym evening/social settings, with upper
body or three-quarter framing. Plain members use the screened plain scene
vocabulary, coloured clothing and all four framings. Every assignment runs the
existing style screens, name screens and an explicit age floor of 25.

The choice space is finite. When no safe choice remains under the caps, the
member is skipped with `no_safe_capacity`. Protected existing choices can already
exceed a cap; the report counts those conflicts rather than modifying them. One
command accepts at most 1,024 members. Dry-run reports GUIDs, fixed choice indices,
kind/framing codes, lighting indices and caps, never names or persona prose. The
indices refer to the lists in `style_variety.py` and `photo_extra.py`.

Six optional fixed phrases cover golden hour, neon, candlelight, city lights,
string lights and a rain-wet street. The `photo_extra` row binds the chosen phrase
to a digest of the photo kind; a later hand edit invalidates it. Rendering adds
it at priority 1, allowing the existing 75-token fit to drop it. Required subject,
chosen build and lust outfit/setting phrases remain priority 0. Unknown lighting
strings are ignored and the name filter still applies. Normal content and NSFW
gates remain in the existing renderer.

## Optional automatic night choices

`[selfies] variety = true` opts in; the default is false. Invalid tables cannot
enable it. The lust pass uses the same balanced assigner. The plain pass still
asks its reflection model, adding up to eight recent outfits and eight settings
as quoted observations in user content. Its built-in system prompt is unchanged;
this is a preference hint, not a hard cap on a free-text model answer.

Manual restyles and enabled nightly saves record photo choices in private
`photo_style_history`, pruning entries older than 31 days on save. The hint uses
only that window, excludes future timestamps, and filters names. A newly migrated
database has no historical choices to report until such saves occur. The history
does not read chat or game activity, and neither it nor the lighting phrases are
logged. Turning automatic variety off preserves existing manually saved lighting
and queued work, while disabling new automatic assignments and history writes.
