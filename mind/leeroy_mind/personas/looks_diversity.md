# Varied fictional appearances

`[looks] diversity = true` enables the deterministic sampler for cast generation
and nightly newcomers. It defaults to false; malformed or unknown settings disable
it. The generator still makes its existing two model calls. Sampling supplies the
appearance; nightly enrichment can still fill missing disclosures and tastes.
Manual re-rolls below are explicit operations and do not require this switch.

All sampled members must be at least 25. Existing name, likeness, age, clothing and
content screens run on the complete appearance. Ancestry is an authored fictional
descriptor in `appearance.face`; hair texture fits the existing `hair_style` field.
It is never inferred from a photograph or a real person.

The coarse regional weights are:

| Fictional descriptor | UK | US |
|---|---:|---:|
| European ancestry | 86 | 63 |
| South Asian ancestry | 6 | 2 |
| East Asian ancestry | 2 | 3 |
| African ancestry | 3 | 12 |
| Latin American ancestry | 0 | 16 |
| Mixed ancestry | 3 | 3 |
| Indigenous American ancestry | 0 | 1 |

These are authored period priors, informed by the
[ONS 2011 England and Wales overview](https://www.ons.gov.uk/peoplepopulationandcommunity/culturalidentity/ethnicity/articles/ethnicityandnationalidentityinenglandandwales/2012-12-11/)
and the [2010 US Census overview](https://www2.census.gov/programs-surveys/decennial/2010/program-management/4-release/press-kit/operational-press-briefings/2011-03-24/slides-230pm.pdf).
England and Wales is only a proxy for the fictional UK pool. Census ethnicity,
race and ancestry categories are not interchangeable; the weights are not a
demographic estimate. Skin ranges overlap. Every hair, face and eye option comes
from a fixed screened list, with separate deterministic draws per member.

Lust women draw curvy/voluptuous builds at 3:2. Other members draw slim, athletic,
average, curvy, stocky and heavy at 20:22:28:12:10:8. Height and weight ranges are
authored visual constraints, not health guidance. A weighted categorical look
vector rejects cosine distances below `MIN_LOOK_DISTANCE = 0.22` against existing
and already planned appearances, including the member's old look. After 128
unsuccessful draws it refuses with a fixed code.

## Diagnose and re-roll similar chosen photos

Run with the mind stopped, from its own environment:

```powershell
python -m leeroy_mind selfies variety --data-dir PATH --top 30 --dry-run
python -m leeroy_mind selfies variety --data-dir PATH --top 30 --go
```

The command takes the existing exclusive mind lock. Dry-run opens the database
read-only and writes no cache, persona, queue or image. Both modes may start the
encoder worker for uncached current picks, using immutable `history/<pick>.jpg`
files. Only the already fetched IP-Adapter ViT-H encoder is loaded, locally;
there are no model downloads, diffusion renders or Ollama unloads. It uses the
CPU when the configured VRAM floor is unavailable, including a GPU OOM fallback.
The subprocess tree is stopped on completion, failure and cancellation.

The cache stores normalized 1,024-float head-crop vectors by GUID, pick and encoder
version. Cosine distance at most `PHOTO_CLUSTER_DISTANCE = 0.08` joins a cluster.
These uncalibrated visual clusters are not identity matches. Largest clusters go
first; one member remains in each, preferring a protected member. `--top` defaults
to 30; one invocation accepts at most 1,024 chosen photos. Output contains GUIDs,
counts, distances and fixed codes, never persona text, names or vectors.

`--go` creates a new `source='variety'` persona version with new looks and a newly
screened photo choice. Identity, age, region, romance stance and other persona
fields stay intact. It queues a fresh set through the existing queue and preserves
the old gallery and chosen cover. A pending marker prevents repeat re-rolls while
the old cover is still selected (`awaiting_new_pick`). The ordinary photo booth
renders queued work later with its existing content and GPU gates.

## Owner provenance

Older databases retain only the latest persona row, so a later generated update
can hide an earlier owner appearance edit. Migration conservatively protects all
pre-existing appearances as `legacy_unknown`, and current owner rows as `owner`.
There is no override switch. Reliable external provenance is needed before any
uncertain legacy appearance can be made eligible; dry-run lists those GUIDs.

For future edits, database triggers permanently protect an appearance changed by
an owner save, including clearing it. An unrelated owner field edit that leaves
the generated appearance unchanged does not claim the appearance. Re-rolls check
protection, persona version and current pick again inside each write transaction.
All validation here uses synthetic photos and fake embeddings. Real encoder
quality and GPU measurements remain release checks.
