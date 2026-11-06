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
.venv/Scripts/python -m leeroy_selfie fetch-models      # --only realvisxl,nsfw
.venv/Scripts/python -m leeroy_selfie doctor            # --quick: no torch import, no hashing
.venv/Scripts/python -m leeroy_selfie where
.venv/Scripts/python -m leeroy_selfie smoke --out DIR   # one invented person; needs the GPU
.venv/Scripts/python -m pytest -q                       # no model, GPU or network
```

`LEEROY_SELFIE_DATA` overrides the data directory (default `selfie-data` beside the repo, never
committed); models land in `<data>/models/<name>` at the revisions pinned in
`leeroy_selfie/models.py`. The mind's doctor runs `doctor --quick`.

`pytest -m engines` runs the checks that need the fetched model files, `-m gpu` those that need
the GPU; the default run deselects both.

`python -m leeroy_selfie worker --data DIR --min-free-gib N` is the night worker, started by the
mind and never by hand: JSON lines on stdin and stdout, one render at a time, `ready` first
(`ok: false` with an error when it refuses, for example with too little free VRAM). The end of
stdin ends it at once, mid-render too. The mind's `[selfies]` table in `mind.toml` turns
rendering on (off by default) and sets the classifier limit, steps, guidance, size and the
VRAM floor; the mind's stages, GPU rules and file layout are in `../CLAUDE.md`.

Licences: this package is GPL-3.0-or-later (see `LICENSE`); RealVisXL is openrail++, the
classifier Apache-2.0, diffusers Apache-2.0. Pins and sources are in `../references.bib`.
