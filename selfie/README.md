# leeroy-selfie

The guild's profile photos. The mind (`../mind`, Python 3.14) decides who gets a photo and what
it shows; this package renders candidates locally with RealVisXL V5.0 and checks each with a
local NSFW classifier. The mind starts the worker as a subprocess at night only, never while the
owner is in game, and never imports this package: a JSON-lines protocol is the boundary.

Content limits: adults only, clothed and non-explicit, no real person's likeness (no person's
name ever reaches a prompt), and every image passes the NSFW check before it is written. Logs
hold ids, counts, scores and timings, never a prompt or persona text. Generated images are never
committed.

## Running it

It needs Python 3.12, in its own venv (never the mind's or the voice service's):

```bash
cd selfie
uv venv --python 3.12 .venv
uv pip install --python .venv/Scripts/python.exe -e ".[gpu,test]" \
  --extra-index-url https://download.pytorch.org/whl/cu128
.venv/Scripts/python -m leeroy_selfie fetch-models      # --only realvisxl,nsfw,ipadapter
.venv/Scripts/python -m leeroy_selfie doctor            # --quick: no torch import, no hashing
.venv/Scripts/python -m leeroy_selfie where
.venv/Scripts/python -m leeroy_selfie smoke --out DIR   # one invented person; needs the GPU
.venv/Scripts/python -m leeroy_selfie smoke --out DIR --identity --ref FILE  # the same, with a face
.venv/Scripts/python -m pytest -q                       # no model, GPU or network
```

`fetch-models` with no `--only` also fetches `ipadapter` (IP-Adapter Plus Face for SDXL and its
ViT-H image encoder, about 3.4 GB); only an `--identity` worker needs it, so `doctor` notes it
as not fetched without failing. `smoke --identity --ref FILE` renders the same invented person
as an 832 x 1216 portrait conditioned on the whole of FILE (a JPEG) at scale 0.6, and prints
the same fields, `peak_gib` among them: the release measures the identity worker's VRAM with
it before trusting the mind's `identity_min_free_gib`.

`LEEROY_SELFIE_DATA` overrides the data directory (default `selfie-data` beside the repo, never
committed); models land in `<data>/models/<name>` at the revisions pinned in
`leeroy_selfie/models.py`. The mind's doctor runs `doctor --quick`.

`pytest -m engines` runs the checks that need the fetched model files, `-m gpu` those that need
the GPU; the default run deselects both.

`python -m leeroy_selfie worker --data DIR --min-free-gib N` is the night worker, started by the
mind and never by hand: JSON lines on stdin and stdout, one render at a time, `ready` first
(`ok: false` with an error when it refuses, for example with too little free VRAM). Its start
checks the models' download records and revisions without hashing them (`doctor` and
`fetch-models` hash them) and reports `check_ms` and `load_ms`. A VRAM refusal says `retry:
true` and keeps the pipeline loaded on the CPU: the mind makes room and sends `{"id": "ready",
"op": "retry"}`, and the worker checks the free VRAM again (waiting up to 10 s for an unload to
free it) without loading anything twice. The end of stdin ends it at once, mid-render too.
`worker --identity` also loads the adapter (a missing adapter file refuses ready with `model`)
and says `identity: true` in its ready line; it then renders only requests that carry an
`identity` reference (a square crop of a JPEG and a scale; the embeds of the last 8 references,
each a file version and crop, are kept), and a worker without it answers `identity_unavailable`
to one.
`worker --embed-only` loads only the already fetched IP-Adapter image encoder,
with local files only. It uses the CPU if the VRAM floor is unavailable, and
falls back to the CPU after a GPU allocation failure. An `embed` request has
`id`, `op: "embed"` and an absolute JPEG `ref`; its response carries a normalized
1,024-float head-crop vector and model id `ipadapter-vith-head-v1`. Vectors travel
only over the worker protocol, never logs. This mode cannot render images. The
mind's stopped-service `selfies variety` command owns its lifetime and cache;
see `../mind/leeroy_mind/personas/looks_diversity.md`.
`shape: "portrait"` renders 832 x 1216, its thumbnail keeping the aspect. Either worker
answers a `grid` request (1-16 JPEG tiles) by compositing the guild photo with Pillow, no GPU:
each tile resized to `tile`, set on a dark `#0e141a` ground with `gutter` pixels around and
between, 2 x 2 up to 4 x 4; a tile that is missing or not a JPEG answers `missing_tile` and
writes nothing. The mind's `[selfies]` table in `mind.toml` turns
rendering on (off by default) and sets the classifier limit, steps, guidance, size and the
VRAM floor.

Licences: this package is GPL-3.0-or-later (see `LICENSE`); RealVisXL is openrail++, the
classifier Apache-2.0, diffusers Apache-2.0, IP-Adapter Apache-2.0 (its image encoder, OpenCLIP
ViT-H/14, MIT). Pins and sources are in `../references.bib`.
