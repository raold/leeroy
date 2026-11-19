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

## Optional FLUX.1-dev backend

RealVisXL remains the default. Standard FLUX.1-dev is an explicit alternative; it uses its
own transformer, text encoders and XLabs IP-Adapter v2, retaining the shared image checks,
reference cropping, captions, grids and detail pass. SDXL adapter weights cannot be reused.
The existing embed-only worker still uses the SDXL ViT-H encoder so stored diversity vectors
remain comparable. FLUX fetch includes that `ipadapter` pin too; its doctor checks it and the
mind requires it when critic or variety is enabled. No prompt lists or content settings change
when selecting a backend.

Install the extra into the worker's Python 3.12 environment, accept the model's access terms
on Hugging Face, and sign in locally (never put a token in TOML, shell arguments or the repo):

```powershell
uv pip install --python .venv/Scripts/python.exe -e ".[gpu,flux,test]" --extra-index-url https://download.pytorch.org/whl/cu128
.venv/Scripts/hf.exe auth login
$env:LEEROY_SELFIE_DATA = 'F:/azerothcore/selfie-data'
.venv/Scripts/python.exe -m leeroy_selfie fetch-models --backend flux1-dev --auth
.venv/Scripts/python.exe -m leeroy_selfie doctor --backend flux1-dev --quick
```

`--auth` uses the local Hugging Face login; only the explicit fetch command accesses the
network. The default fetch still downloads only the existing models. FLUX weights and its
adapter are pinned by revision and checked like SDXL. The initial download is large: it fetches
the original weights, then quantizes the transformer and T5 encoder to NF4 during loading.
It requires a CUDA GPU with BF16 support and the pinned bitsandbytes extra, and uses model CPU
offload plus VAE slicing/tiling. Measured load time, peak VRAM and identity quality must be
checked on the intended hardware before release; unit tests do not establish those results.

After deploying both packages, add `backend = "flux1-dev"` inside the existing `[selfies]`
table and restart the mind. Remove that key or set `"realvisxl"` to switch back; old images
and picks remain. Worker startup and render waits are bounded at 600 seconds for FLUX only.
Fresh-photo requests also get a 600-second total work budget, including drain, load, renders
and judging; SDXL keeps 180 seconds. Login, voice and GPU pause still interrupt immediately,
and worker teardown finishes before the live chat model reloads.
The existing VRAM floors still apply. A low-VRAM refusal happens before quantization and
retries loading only after memory has been freed. The mind doctor checks the selected model
and requires the FLUX identity weights when snaps or identity sets are enabled.

In an owner-approved GPU window with the mind stopped and its models unloaded, the worker
can be checked independently using `smoke --backend flux1-dev --out DIR`, then
`smoke --backend flux1-dev --identity --ref FILE --out ANOTHER_DIR` with a generated fictional
adult's reference. The output reports render time, peak VRAM and classifier score. Restore
the mind afterward. The owner should inspect face consistency and appearance before enabling
automatic rendering. The existing conservative prompt budget is retained. The protocol's
CFG controls FLUX's native distilled guidance. Its default is 3.5 when the backend is selected
in TOML without an explicit `guidance`; an existing value is preserved, so set `guidance = 3.5`
when switching a table that already sets it. Smoke/detail commands default to 3.5 too.
Negative prompts use a separate true CFG of 1.5 (1 when native guidance is 1). Reusing SDXL's
5.0 as true CFG in addition to native guidance severely clipped colours and sometimes produced
a colour blob in the initial GPU test. The corrected setting was compared at the same prompt
and seed. SDXL parameter rankings need a fresh visual assessment on FLUX.

FLUX.1-dev and the adapter use the FLUX.1-dev non-commercial license; the CLIP ViT-L/14
encoder uses MIT. See the [model card](https://huggingface.co/black-forest-labs/FLUX.1-dev)
and [adapter card](https://huggingface.co/XLabs-AI/flux-ip-adapter-v2) for their terms.

## Worker protocol

`python -m leeroy_selfie worker --data DIR --min-free-gib N` is the night worker, started by the
mind and never by hand: JSON lines on stdin and stdout, one render at a time, `ready` first
(`ok: false` with an error when it refuses, for example with too little free VRAM). Its start
checks the models' download records and revisions without hashing them (`doctor` and
`fetch-models` hash them) and reports `check_ms` and `load_ms`. A VRAM refusal says `retry:
true` and keeps the pipeline loaded on the CPU: the mind makes room and sends `{"id": "ready",
"op": "retry"}`, and the worker checks the free VRAM again (waiting up to 10 s for an unload to
free it) without loading anything twice. The end of stdin ends it at once, mid-render too.
`worker --identity` also loads the adapter (a missing adapter file refuses ready with `model`)
and says `identity: true` in its ready line; it renders requests that carry an `identity`
reference (a square crop of a JPEG and a scale; the embeds of the last 8 references, each a file
version and crop, are kept) and, since 2026-11-14, requests without one too (a blank reference at
scale 0, which adds nothing), so one worker serves a night's profile sets with and without a
face; a worker without the adapter answers `identity_unavailable` to a reference.
The reference face (`face.py`): with the `yunet` pin fetched (`fetch-models --only yunet`, YuNet
from OpenCV Zoo, MIT, about 230 KB; needs `opencv-python-headless` from the `gpu` extra) the
worker finds the face in the reference and cuts a square of 1.8 times its longer side around it,
preferring a face inside the mind's square, then evens out the crop's colour cast (gray world),
so a nightclub's purple light does not tint every new scene. Not fetched, no OpenCV, no face
found or any detector error: the mind's square is cut as it is. `doctor` notes `yunet` as not
fetched without failing.
The photo critic (`critic.py`, the mind's `[selfies] critic`): a `critic` request (`id`, `op:
"critic"`, an absolute JPEG `path`, optionally her reference `ref` with its square `crop`) is
answered by any worker, `--embed-only` too, with numbers and codes only: `faces` (YuNet), `hands`
and per hand its landmark confidence, `fingers` (fingertips that stand apart from every other tip:
merged fingers share one) and `plausible` (no finger far longer than the palm or folded back
through it), `eyes` (`tilt`, the eye line against the mouth line in degrees; `level`; `gaze_gap`,
how far apart the irises sit in their eyes), `identity` (the image encoder's cosine between the
face crops, an identity worker's or the embed-only worker's encoder) and per detector `ok`,
`unavailable` or `failed`. The hands come from the `palm` and `handpose` pins (MediaPipe's palm
detector and hand landmarker, OpenCV Zoo's ONNX exports, Apache-2.0, about 4 MB each, run by
`cv2.dnn`; `fetch-models --only palm,handpose`); not fetched, `doctor` notes them and the critic
reports hands `unavailable`. The `mediapipe` package was not used: it would install
`opencv-contrib-python` beside the pinned `opencv-python-headless`. The landmarker always draws
five fingers, so these are measurements, not verdicts: against the labelled snap-anatomy sheet
of 2026-11-16 they caught none of its subtle defects; they are there for the gross ones, a
second face or a third hand. `pytest -m engines tests/test_critic_sheet.py` with
`LEEROY_CRITIC_SHEET` pointing at that sheet prints their precision and recall.
**The mind's vision judge is a placeholder; the identity cosine is the useful signal.** On the same
sheet (9 defective images: merged fingers around a mug, a shoe without a leg, a stray forearm, one
drifting eye) no judge tried reached a usable call, on the CPU (2026-11-16; the GPU was the live
mind's): `qwen2.5vl:7b` (the default) flagged 0 of 9 and graded all 72 images 3, with the counting
prompt too (1 of 9); `gemma3:27b` flagged 0 of 9 on the schema and 1 of 9 counting, and only its
grade carried signal (overall 2 or less: 6 of 9 defects, but also 5 of 9 clean images, so about
35 false flags across the sheet's 63 clean ones); `mistral-small3.2:24b` flagged 0 of 9 and graded
everything 3. The cosine to her reference separates her from the other members (her own min 0.73,
mean 0.83; others max 0.74, mean 0.46), so best-of-N ranks seeds mostly by her likeness. Swap
`[selfies] critic_model` when a better local vision model arrives; measure it on the sheet first.
The detailer (`detail.py`, the mind's `[selfies] detailer` and `detailer_hands`; both off by
default): SDXL draws a small face or hand with too few pixels (crossed eyes, six fingers), so a
render request may ask for a second pass over its image with `detail_faces` and `detail_hands`
(booleans), and optionally `detail_face_denoise` (default 0.35) and `detail_hand_denoise` (0.40),
each 0.1-0.7, `detail_size` (768, 896 or 1024, default 1024), `detail_face_max` (0.05-0.5,
default 0.18) and `detail_identity_margin` (0-1, default 0.03). Without a true flag there is no
pass and the render and its answer are what they were. With one, each face YuNet finds (at most
2, the largest first) and each palm the palm detector finds (at most 2, the surest first, scored
0.6 or more) is cut out as a padded square (2 times the face box; 2.5 times the palm box and at
most 45% of the frame's shorter side; shifted to stay in the frame), enlarged to `detail_size`,
drawn again by SDXL img2img at its denoise with the image's steps, CFG and negative and a seed
derived from the image's, shrunk and pasted back through a mask feathered over an eighth of the
crop. Hands go first and faces last. A face's prompt is `detailed face, natural eyes` and then
the render's own phrases as far as they fit; a hand's is `a natural hand, five fingers` and then
the render's phrases that say what she is wearing, so the pass does not redraw her clothes.
Passed over: a face box under 48 px or over `detail_face_max` of the frame's shorter side (it has
its pixels already), a palm box under 24 px or over 25%, and a crop that would not be enlarged.
The mind leaves `detail_faces` off for a photo from behind.
Her reference: on an identity worker only the largest face found (rank 0) keeps the request's
reference at its scale; every other crop carries the blank reference at scale 0, and when her
own face is passed over no smaller face takes her reference. That face is also held to her
likeness: the image encoder's cosine between the crop and her reference's face crop is read
before and after, and when it falls by more than `detail_identity_margin` the crop stays as it
was (`detail_reverted` counts these).
The NSFW check reads the image before and after the pass. A render that scores `nsfw_max` or
more gets no pass and is flagged as ever (`detail_code` `nsfw_base`); an image the pass made
that scores `nsfw_max` or more is never written, and the render as it was is written instead
(`nsfw_reverted`, counts 0, the render's own score): the pass never flags a clean render and
never clears a flagged one.
The answer then also carries `detail_faces` and `detail_hands` (how many were drawn again),
`detail_skipped`, `detail_reverted`, `detail_ms` and `detail_code`: empty, `nsfw_base`,
`nsfw_reverted`, `faces_unavailable`, `hands_unavailable`, `detectors_unavailable` (a detector
not fetched: the faces need `yunet`, the hands `palm` and `handpose`; the other kind still runs),
`oom` (CUDA out of memory in a crop: the cache is freed, the pass ends, what was pasted stays) or
`error`. The pass never fails a render. It loads nothing: the img2img pipeline is built at the
first pass from the loaded pipeline's own components, and it works at 1024 x 1024 at most, the
latents of a 1024 square render (`peak_gib` covers the pass; a 768 set's peak rises to a 1024
render's). Each crop costs about a third of a render's steps, about 2 s.
`python -m leeroy_selfie detail --in FILE --out FILE [--no-faces] [--hands] [--denoise X]
[--hand-denoise X] [--size N] [--face-max F] [--ref FILE --scale S --identity-margin M]
[--seed N] [--steps N] [--cfg X] [--prompt TEXT] [--hand-prompt TEXT] [--negative TEXT]` applies
the pass to one of the worker's own JPEGs without the mind (it needs the GPU; `--ref` loads the
identity adapter) and prints one JSON line with the fields above, `nsfw_in`, `nsfw` and
`peak_gib`; an input the gate flags gets no pass. `--boxes` only finds and prints the crops (and
the faces' ranks) on the CPU and writes nothing. `smoke --detail` renders the smoke image with
both passes.
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
writes nothing. Either worker also answers a `caption` request (the mind's culture memes):
`base_path` (an existing JPEG), `top` and `bottom` (at most 60 printable characters each, one
may be empty, and only characters the font draws: `protocol.CAPTION_CHARS`, printable ASCII,
Latin-1, Latin Extended-A and common typographic marks; an emoji is a bad request), `out_path`
and `nsfw_max`. It draws the classic outlined capitals at the top and
bottom with Pillow in the bundled Anton font (SIL Open Font License 1.1, `leeroy_selfie/fonts/`
with its `OFL.txt`), each block sized to the width; the text is never part of a diffusion
prompt. The captioned image goes through the NSFW classifier like every render and is written
only when it scores under `nsfw_max` (`flagged: true` writes nothing); a base that is missing
or not a JPEG answers `missing_base`. The mind's `[selfies]` table in `mind.toml` turns
rendering on (off by default) and sets the classifier limit, steps, guidance, size and the
VRAM floor.

Licences: this package is GPL-3.0-or-later (see `LICENSE`); RealVisXL is openrail++, the
classifier Apache-2.0, diffusers Apache-2.0, IP-Adapter Apache-2.0 (its image encoder, OpenCLIP
ViT-H/14, MIT), the Anton caption font SIL OFL 1.1 (`leeroy_selfie/fonts/OFL.txt`). Pins and sources are in `../references.bib`.
