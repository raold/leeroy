"""The renderer: RealVisXL on the GPU, the NSFW classifier on the CPU, and for an identity worker
the IP-Adapter Plus Face with its image encoder (and, when fetched, the YuNet face detector that
cuts the reference tight around her face: face.py). torch, diffusers, transformers and Pillow are
imported inside the functions that need them, so the CLI and the default test suite never load
them."""
import os
import time
from collections import OrderedDict
from pathlib import Path

from . import caption, critic, detail, face, grid, jpeg, models, similarity
from .fit import fit
from .protocol import PORTRAIT_SIZE, Caption, Critic, Grid, Identity, Render, RenderFailed


RETRY_WAIT_S = 10.0  # a retry's wait for the VRAM an unload is still freeing
VRAM_POLL_S = 0.25
PORTRAIT = PORTRAIT_SIZE  # (832, 1216): width, height of shape "portrait"
REF_CACHE = 8  # reference embeds kept, newest used last
ADAPTER_WEIGHTS = "ip-adapter-plus-face_sdxl_vit-h.safetensors"  # in <ipadapter>/sdxl_models


class ReadyError(Exception):
    """The worker cannot become ready; `reason` is the protocol's error name. `retry`, after a
    vram refusal, tries the GPU again with the pipeline this start loaded on the CPU (None: the
    refusal is final). `fields` are numbers for the ready answer: the start's check_ms and
    load_ms, and the free_gib it read."""

    def __init__(self, reason: str, *, retry=None, fields: dict | None = None):
        super().__init__(reason)
        self.reason = reason
        self.retry = retry
        self.fields = dict(fields or {})


def ref_box(width: int, height: int, crop: tuple[float, float, float, float]
            ) -> tuple[int, int, int, int]:
    """The (l, t, r, b) fractions of `crop` as pixels of a width x height image, rounded and
    clamped to it."""
    l, t, r, b = crop

    def px(f: float, n: int) -> int:
        return min(n, max(0, round(f * n)))
    return px(l, width), px(t, height), px(r, width), px(b, height)


class Renderer:
    """`identity` says the pipeline has the adapter: a render with a reference is conditioned on
    it, one without renders with the adapter at scale 0 (2026-11-14: one identity worker serves
    a night's sets with and without a reference); without the adapter none may carry one.
    `face_ref` (face.FaceRef, or None) cuts the reference tight around her face. `data` is the
    data dir whose fetched detectors the critic loads at its first request."""

    def __init__(self, pipe, processor, classifier, nsfw_index: int, torch, info: dict,
                 identity: bool = False, face_ref=None, data: Path | None = None):
        self.pipe = pipe
        self.processor = processor
        self.classifier = classifier
        self.nsfw_index = nsfw_index
        self.torch = torch
        self.info = info
        self.identity = identity
        self.face_ref = face_ref
        self._refs: OrderedDict = OrderedDict()  # (ref, st_mtime_ns, crop, cfg) -> embeds
        self._blanks: dict = {}  # cfg -> the embeds a render without a reference carries
        self.data = data
        self._detectors = None  # critic.Detectors, loaded at the first critic or detail request
        self._i2i = None  # the img2img pipeline over the same components, at the first detail

    def _count(self, text: str) -> int:
        return max(len(tok(text, add_special_tokens=False).input_ids)
                   for tok in (self.pipe.tokenizer, self.pipe.tokenizer_2))

    def score(self, image) -> float:
        torch = self.torch
        with torch.no_grad():
            logits = self.classifier(**self.processor(images=image, return_tensors="pt")).logits
        return float(torch.softmax(logits, dim=-1)[0, self.nsfw_index])

    def render(self, req: Render) -> dict:
        """One image, a square of the requested `size` (768, 896 or 1024) or PORTRAIT for shape
        "portrait", after the detailer's pass when the request asks for one (detail.py: its
        detail_ fields are in the answer then), saved as is to `out`, and a thumbnail with its long side `thumb_size` to
        `thumb`; neither when the NSFW score reaches nsfw_max. A render with an identity is
        conditioned on the face in its reference crop, at its scale; one without, on an
        identity pipeline, carries a blank reference at scale 0, which adds nothing. RenderFailed:
        oom, missing_ref (the reference is gone or not a JPEG), identity_unavailable (a reference
        without the adapter).

        With a pass the NSFW check reads the image before and after it. A render that scores
        nsfw_max or more gets no pass and is flagged as ever (detail_code nsfw_base). An image
        the pass made that scores nsfw_max or more is never written: the render as it was is
        (detail_code nsfw_reverted, the counts 0, the score the render's own), so the pass
        never flags a clean render and never clears a flagged one."""
        if req.identity is not None and not self.identity:
            raise RenderFailed("identity_unavailable")
        torch = self.torch
        started = time.perf_counter()
        limit = self.pipe.tokenizer.model_max_length - 2
        prompt, dropped = fit(req.phrases, self._count, limit)
        negative, _ = fit(req.negative, self._count, limit)
        width, height = PORTRAIT if req.shape == "portrait" else (req.size, req.size)
        torch.cuda.reset_peak_memory_stats()
        try:  # the reference is encoded inside, so its VRAM counts in peak_gib
            adapter_kw = {}
            if req.identity is not None:  # diffusers turns CFG off at cfg 1.0 (guidance > 1)
                adapter_kw["ip_adapter_image_embeds"] = self._embeds(req.identity, req.cfg > 1)
                self.pipe.set_ip_adapter_scale(req.identity.scale)
            elif self.identity:  # the adapter's addition is scale * its attention: 0 adds nothing
                adapter_kw["ip_adapter_image_embeds"] = self._blank(req.cfg > 1)
                self.pipe.set_ip_adapter_scale(0.0)
            generator = torch.Generator("cuda").manual_seed(req.seed)
            image = self._draw(self.pipe, prompt=prompt, negative_prompt=negative,
                              num_inference_steps=req.steps, guidance_scale=req.cfg,
                              width=width, height=height, generator=generator,
                              **adapter_kw).images[0]
        except torch.cuda.OutOfMemoryError as e:
            raise RenderFailed("oom") from e
        nsfw = self.score(image)
        detailed = {}
        if req.detail is not None and nsfw >= req.nsfw_max:
            detailed = {"detail_faces": 0, "detail_hands": 0, "detail_skipped": 0,
                        "detail_reverted": 0, "detail_ms": 0, "detail_code": "nsfw_base"}
        elif req.detail is not None:
            lead = ((detail.FACE_LEAD, 0), *((t, min(9, p + 1)) for t, p in req.phrases))
            hand = ((detail.HAND_LEAD, 0), *((t, 1) for t, _ in req.phrases if "wearing" in t))
            refined, detailed = self.detail_pass(
                image, req.detail, seed=req.seed, steps=req.steps, cfg=req.cfg,
                face_prompt=fit(lead, self._count, limit)[0],
                hand_prompt=fit(hand, self._count, limit)[0],
                negative=negative, identity=req.identity)
            if refined is not image:
                after = self.score(refined)
                if after >= req.nsfw_max:  # never written; the render as it was is
                    detailed = {**detailed, "detail_faces": 0, "detail_hands": 0,
                                "detail_code": "nsfw_reverted"}
                else:
                    image, nsfw = refined, after
        flagged = nsfw >= req.nsfw_max
        if not flagged:
            from PIL import Image
            jpeg.write(image, req.out, 92)
            jpeg.write(image.resize(jpeg.thumb_size(width, height, req.thumb_size),
                                    Image.LANCZOS), req.thumb, 85)
        return {"flagged": flagged, "nsfw": round(nsfw, 4), "prompt": prompt,
                "dropped": list(dropped), "tokens": self._count(prompt),
                "seconds": round(time.perf_counter() - started, 2),
                "peak_gib": round(torch.cuda.max_memory_allocated() / 2**30, 2),
                "width": width, "height": height, **detailed}

    def detail_pass(self, image, opts: detail.Detail, *, seed: int, steps: int, cfg: float,
                    face_prompt: str, hand_prompt: str, negative: str,
                    identity: Identity | None = None) -> tuple[object, dict]:
        """(the image after the detailer's pass, its detail_ fields): detail.run with this
        renderer's detectors and refiner. It never raises for the pass: a missing detector, CUDA
        out of memory or any error is a detail_code and the image as far as it got."""
        try:
            if self._detectors is None:
                self._detectors = critic.load_detectors(self.data)
        except Exception:
            self._detectors = None
        return detail.run(image, self._detectors, opts, self.refiner(
            opts, seed=seed, steps=steps, cfg=cfg, face_prompt=face_prompt,
            hand_prompt=hand_prompt, negative=negative, identity=identity))

    def refiner(self, opts: detail.Detail, *, seed: int, steps: int, cfg: float,
                face_prompt: str, hand_prompt: str, negative: str,
                identity: Identity | None = None):
        """detail.run's refine: one crop drawn again by img2img at its kind's strength, with the
        crop's own seed (detail.seed_for). On an identity pipeline the largest face keeps her
        reference at its scale, so the face stays hers; every other crop carries the blank
        reference at scale 0. "Largest" is the face's rank among the faces found (0), never its
        place among the crops kept: when her own face is passed over (a headshot's), a smaller
        face behind her is drawn again without her reference. That face is also held to her
        likeness: when the image encoder's cosine between the crop and her reference falls by
        more than opts.identity_margin in the pass, the crop stays as it was (detail.Keep); an
        encoder that is missing or fails checks nothing. CUDA out of memory frees what it can and ends the pass
        (detail.Skip("oom")): the pass works at `size` x `size` (at most 1024), the latents of a
        square render, on the weights already loaded, so it needs no more VRAM than a render."""
        torch = self.torch

        hers = self.identity and identity is not None
        likeness = self._likeness(identity) if hers else None

        def refine(kind: str, rank: int, crop):
            adapter_kw = {}
            mine = hers and kind == "face" and rank == 0
            if self.identity:
                if mine:
                    adapter_kw["ip_adapter_image_embeds"] = self._embeds(identity, cfg > 1)
                    self.pipe.set_ip_adapter_scale(identity.scale)
                else:
                    adapter_kw["ip_adapter_image_embeds"] = self._blank(cfg > 1)
                    self.pipe.set_ip_adapter_scale(0.0)
            generator = torch.Generator("cuda").manual_seed(detail.seed_for(seed, kind, rank))
            try:
                new = self._draw(self._img2img(),
                    prompt=hand_prompt if kind == "hand" else face_prompt,
                    negative_prompt=negative, image=crop,
                    strength=opts.hand_denoise if kind == "hand" else opts.face_denoise,
                    num_inference_steps=steps, guidance_scale=cfg, generator=generator,
                    **adapter_kw).images[0]
            except torch.cuda.OutOfMemoryError as e:
                try:
                    torch.cuda.empty_cache()
                except Exception:
                    pass
                raise detail.Skip("oom") from e
            if mine and likeness is not None and likeness(crop, new) > opts.identity_margin:
                raise detail.Keep()
            return new
        return refine

    def _likeness(self, identity: Identity):
        """(the crop before, the crop after) -> how far the image encoder's cosine to her
        reference's face crop fell (0 when it rose, or when anything here fails); None without
        an image encoder."""
        encode = critic.encoder(getattr(self.pipe, "image_encoder", None),
                                getattr(self.pipe, "feature_extractor", None), self.torch)
        if encode is None:
            return None
        ref = []

        def fell(before, after) -> float:
            try:
                if not ref:
                    ref.append(encode(critic.reference_crop(self._detectors, identity.ref,
                                                            identity.crop)))
                return max(0.0, critic.cosine(encode(before), ref[0])
                           - critic.cosine(encode(after), ref[0]))
            except Exception:
                return 0.0
        return fell

    def _img2img(self):
        """SDXL img2img over the loaded pipeline's own components (the same objects: no weight
        is loaded, copied or cast), made at the first detail request."""
        if self._i2i is None:
            from diffusers import StableDiffusionXLImg2ImgPipeline
            pipe = self.pipe
            self._i2i = StableDiffusionXLImg2ImgPipeline(
                **pipe.components,
                force_zeros_for_empty_prompt=getattr(pipe.config, "force_zeros_for_empty_prompt",
                                                     True),
                add_watermarker=getattr(pipe, "watermark", None) is not None)
            self._i2i.set_progress_bar_config(disable=True)
        return self._i2i

    def _draw(self, pipe, **kwargs):
        return pipe(**kwargs)

    def _encode_reference(self, crop, cfg: bool):
        return self.pipe.prepare_ip_adapter_image_embeds(
            ip_adapter_image=crop, ip_adapter_image_embeds=None, device="cuda",
            num_images_per_prompt=1, do_classifier_free_guidance=cfg)

    def _embeds(self, identity: Identity, cfg: bool):
        """The adapter's embeds of the reference's crop, computed once per file version, crop
        and `cfg` (the newest REF_CACHE kept). `cfg` must be the pipeline's own choice: CFG
        embeds hold the negative too, and with CFG off diffusers folds that second half into
        the face's tokens without an error. Gradients stay off, so a cached entry holds no
        encoder activations."""
        try:
            key = (identity.ref, os.stat(identity.ref).st_mtime_ns, identity.crop, cfg)
        except OSError as e:
            raise RenderFailed("missing_ref") from e
        if key in self._refs:
            self._refs.move_to_end(key)
            return self._refs[key]
        try:
            img = jpeg.open_rgb(identity.ref)
        except Exception as e:  # Pillow's errors share no narrower base
            raise RenderFailed("missing_ref") from e
        box = ref_box(*img.size, identity.crop)
        crop = self.face_ref(img, box)[0] if self.face_ref is not None else img.crop(box)
        with self.torch.no_grad():
            embeds = self._encode_reference(crop, cfg)
        self._refs[key] = embeds
        while len(self._refs) > REF_CACHE:
            self._refs.popitem(last=False)
        return embeds

    def _blank(self, cfg: bool):
        """The embeds of a plain grey image, once per `cfg`: what a render without a reference
        carries through the adapter, at scale 0."""
        if cfg not in self._blanks:
            from PIL import Image
            with self.torch.no_grad():
                self._blanks[cfg] = self._encode_reference(
                    Image.new("RGB", (224, 224), (128, 128, 128)), cfg)
        return self._blanks[cfg]

    def critic(self, req: Critic) -> dict:
        """The photo critic's measurements of one image (critic.answer): faces, hands, eyes and,
        with a reference, the identity cosine through the adapter's image encoder (an identity
        worker's; a plain one answers identity_code encoder_unavailable)."""
        if self._detectors is None:
            self._detectors = critic.load_detectors(self.data)
        encode = critic.encoder(getattr(self.pipe, "image_encoder", None),
                                getattr(self.pipe, "feature_extractor", None), self.torch)
        return critic.answer(self._detectors, req, encode)

    def caption(self, req: Caption) -> dict:
        """A meme: the caption drawn over the base JPEG (caption.draw), scored by the NSFW
        classifier like every render, and written to out_path (quality 92) only when it scores
        under nsfw_max. RenderFailed("missing_base") when the base is gone or not a JPEG."""
        started = time.perf_counter()
        try:
            img = jpeg.open_rgb(req.base_path)
        except Exception as e:  # Pillow's errors share no narrower base: the base is there or not
            raise RenderFailed("missing_base") from e
        img, boxes = caption.draw(img, req.top, req.bottom)
        nsfw = self.score(img)
        flagged = nsfw >= req.nsfw_max
        if not flagged:
            jpeg.write(img, req.out_path, 92)
        width, height = img.size
        return {"flagged": flagged, "nsfw": round(nsfw, 4), "width": width, "height": height,
                "lines": len(boxes), "seconds": round(time.perf_counter() - started, 2)}

    def grid(self, req: Grid) -> dict:
        """The guild photo composited from its tiles (grid.compose): {"ok": True, width,
        height, seconds, "error": ""}, or {"ok": False, "error": "missing_tile"} with nothing
        written."""
        try:
            fields = grid.compose(req)
        except RenderFailed as e:
            return {"ok": False, "error": str(e)}
        try:
            scores = similarity.measure(self.pipe, self.torch, req.tiles)
        except Exception:  # a diagnostic never fails a good grid
            scores = {"code": "encoder_failed"}
        return {"ok": True, **fields, "error": "", "similarity": scores}


def _ms(seconds: float) -> int:
    return max(0, round(seconds * 1000))


def load(data: Path, *, min_free_gib: float, device: str = "cuda",
         identity: bool = False, backend: str = "realvisxl") -> Renderer:
    """A ready Renderer, or ReadyError: cuda (no torch or no CUDA), model (a file missing, from
    another revision or without its download record, a load that failed, no `nsfw` label),
    vram (less than min_free_gib free just before the pipeline moves to the GPU, or CUDA out of
    memory while it does). The weights load to the CPU first, so another process holding VRAM
    reads as vram, not model; a vram refusal keeps them there for its `retry`. The models are
    checked quickly (records and revisions, no hash of the ~7 GB: doctor and fetch-models hash
    them), so the start reaches its VRAM check soon after the mind made room. With `identity`
    the IP-Adapter pin is checked too, and its image encoder and adapter are loaded into the
    pipeline on the CPU, before that check; the Renderer then says identity."""
    if backend == "flux1-dev":
        from .flux import load as load_flux
        return load_flux(data, min_free_gib=min_free_gib, device=device, identity=identity)
    if backend != "realvisxl":
        raise ReadyError("model")
    try:
        import torch
    except ImportError as e:
        raise ReadyError("cuda") from e
    if device != "cuda" or not torch.cuda.is_available():
        raise ReadyError("cuda")
    started = time.perf_counter()
    for name in models.BASE_PINS + (models.IDENTITY_PINS if identity else ()):
        if any(ok is not True for ok, _ in models.check(data, name, quick=True)):
            raise ReadyError("model", fields={"check_ms": _ms(time.perf_counter() - started),
                                              "load_ms": 0})
    checked = time.perf_counter()
    try:
        import diffusers
        from diffusers import DPMSolverMultistepScheduler, StableDiffusionXLPipeline
        from transformers import AutoImageProcessor, AutoModelForImageClassification
        adapter = {}
        if identity:
            from transformers import CLIPImageProcessor, CLIPVisionModelWithProjection
            ip_dir = models.model_dir(data, "ipadapter")
            adapter = {
                "image_encoder": CLIPVisionModelWithProjection.from_pretrained(
                    ip_dir / "models" / "image_encoder", torch_dtype=torch.float16,
                    local_files_only=True),
                "feature_extractor": CLIPImageProcessor(size=224, crop_size=224)}
        pipe = StableDiffusionXLPipeline.from_pretrained(
            models.model_dir(data, "realvisxl"), torch_dtype=torch.float16, variant="fp16",
            use_safetensors=True, local_files_only=True, **adapter)
        pipe.scheduler = DPMSolverMultistepScheduler.from_config(
            pipe.scheduler.config, use_karras_sigmas=True)
        if identity:
            pipe.load_ip_adapter(str(ip_dir), subfolder="sdxl_models",
                                 weight_name=ADAPTER_WEIGHTS, image_encoder_folder=None,
                                 local_files_only=True)
        nsfw_dir = models.model_dir(data, "nsfw")
        processor = AutoImageProcessor.from_pretrained(nsfw_dir, local_files_only=True)
        classifier = AutoModelForImageClassification.from_pretrained(
            nsfw_dir, local_files_only=True).eval()
    except Exception as e:
        raise ReadyError("model", fields={"check_ms": _ms(checked - started),
                                          "load_ms": _ms(time.perf_counter() - checked)}) from e
    labels = {str(v).lower(): int(k) for k, v in classifier.config.id2label.items()}
    timings = {"check_ms": _ms(checked - started), "load_ms": _ms(time.perf_counter() - checked)}
    if "nsfw" not in labels:
        raise ReadyError("model", fields=timings)
    loaded = _OnCpu(pipe, processor, classifier, labels["nsfw"], torch,
                    {"torch": torch.__version__, "diffusers": diffusers.__version__}, identity,
                    _face_ref(data) if identity else None, data)
    return _to_gpu(loaded, min_free_gib, timings, wait_s=0.0)


class _OnCpu:
    """What a start loaded before its VRAM check, kept for a retry after a vram refusal."""

    def __init__(self, pipe, processor, classifier, nsfw_index: int, torch, versions: dict,
                 identity: bool, face_ref=None, data: Path | None = None):
        self.pipe, self.processor, self.classifier = pipe, processor, classifier
        self.nsfw_index, self.torch, self.versions = nsfw_index, torch, versions
        self.identity, self.face_ref, self.data = identity, face_ref, data


def _face_ref(data: Path):
    """face.FaceRef on the fetched YuNet model, or None (not fetched, changed, or no OpenCV):
    the mind's square is then cut as it is."""
    try:
        if any(ok is not True for ok, _ in models.check(data, "yunet", quick=True)):
            return None
        return face.FaceRef(models.model_dir(data, "yunet") / face.MODEL_FILE)
    except Exception:
        return None


def _free_gib(torch, min_free_gib: float, wait_s: float) -> float:
    """Free VRAM now; with `wait_s`, polled until min_free_gib is free or the wait is over."""
    end = time.monotonic() + wait_s
    while True:
        free = torch.cuda.mem_get_info()[0] / 2**30
        if free >= min_free_gib or time.monotonic() >= end:
            return free
        time.sleep(VRAM_POLL_S)


def _to_gpu(c: _OnCpu, min_free_gib: float, timings: dict, *, wait_s: float) -> Renderer:
    """The pipeline moved to the GPU, or ReadyError: vram with a retry that tries again from
    the CPU (waiting up to RETRY_WAIT_S for the VRAM the mind's unload frees, and loading
    nothing again), model when the move fails otherwise."""
    torch = c.torch
    again = {"check_ms": 0, "load_ms": 0}  # a retry checks and loads nothing

    def retry() -> Renderer:
        return _to_gpu(c, min_free_gib, again, wait_s=RETRY_WAIT_S)
    free_gib = _free_gib(torch, min_free_gib, wait_s)
    fields = {**timings, "free_gib": round(free_gib, 1)}
    if free_gib < min_free_gib:
        raise ReadyError("vram", retry=retry, fields=fields)
    try:
        pipe = c.pipe.to("cuda")
        pipe.set_progress_bar_config(disable=True)
    except torch.cuda.OutOfMemoryError as e:
        try:  # what moved goes back, so a retry starts from the CPU again
            c.pipe.to("cpu")
            torch.cuda.empty_cache()
        except Exception:
            raise ReadyError("vram", fields=fields) from e
        raise ReadyError("vram", retry=retry, fields=fields) from e
    except Exception as e:
        raise ReadyError("model", fields=fields) from e
    info = {"device": "cuda", "free_gib": round(free_gib, 1), "versions": dict(c.versions),
            **timings}
    return Renderer(pipe, c.processor, c.classifier, c.nsfw_index, torch, info, c.identity,
                    c.face_ref, c.data)
