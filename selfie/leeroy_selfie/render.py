"""The renderer: RealVisXL on the GPU, the NSFW classifier on the CPU. torch, diffusers and
transformers are imported inside the functions that need them, so the CLI and the default test
suite never load them."""
import os
import time
from pathlib import Path

from . import models
from .fit import fit
from .protocol import Render, RenderFailed


RETRY_WAIT_S = 10.0  # a retry's wait for the VRAM an unload is still freeing
VRAM_POLL_S = 0.25


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


def _write_jpeg(img, path: str, quality: int) -> None:
    tmp = path + ".tmp"
    img.save(tmp, format="JPEG", quality=quality)
    os.replace(tmp, path)


class Renderer:
    def __init__(self, pipe, processor, classifier, nsfw_index: int, torch, info: dict):
        self.pipe = pipe
        self.processor = processor
        self.classifier = classifier
        self.nsfw_index = nsfw_index
        self.torch = torch
        self.info = info

    def _count(self, text: str) -> int:
        return max(len(tok(text, add_special_tokens=False).input_ids)
                   for tok in (self.pipe.tokenizer, self.pipe.tokenizer_2))

    def score(self, image) -> float:
        torch = self.torch
        with torch.no_grad():
            logits = self.classifier(**self.processor(images=image, return_tensors="pt")).logits
        return float(torch.softmax(logits, dim=-1)[0, self.nsfw_index])

    def render(self, req: Render) -> dict:
        """One image at the requested square `size` (768, 896 or 1024), saved as is to `out`,
        and a `thumb_size` square to `thumb`; neither when the NSFW score reaches nsfw_max."""
        torch = self.torch
        started = time.perf_counter()
        limit = self.pipe.tokenizer.model_max_length - 2
        prompt, dropped = fit(req.phrases, self._count, limit)
        negative, _ = fit(req.negative, self._count, limit)
        torch.cuda.reset_peak_memory_stats()
        generator = torch.Generator("cuda").manual_seed(req.seed)
        try:
            image = self.pipe(prompt=prompt, negative_prompt=negative,
                              num_inference_steps=req.steps, guidance_scale=req.cfg,
                              width=req.size, height=req.size, generator=generator).images[0]
        except torch.cuda.OutOfMemoryError as e:
            raise RenderFailed("oom") from e
        nsfw = self.score(image)
        flagged = nsfw >= req.nsfw_max
        if not flagged:
            from PIL import Image
            _write_jpeg(image, req.out, 92)
            _write_jpeg(image.resize((req.thumb_size, req.thumb_size), Image.LANCZOS),
                        req.thumb, 85)
        return {"flagged": flagged, "nsfw": round(nsfw, 4), "prompt": prompt,
                "dropped": list(dropped), "tokens": self._count(prompt),
                "seconds": round(time.perf_counter() - started, 2),
                "peak_gib": round(torch.cuda.max_memory_allocated() / 2**30, 2)}


def _ms(seconds: float) -> int:
    return max(0, round(seconds * 1000))


def load(data: Path, *, min_free_gib: float, device: str = "cuda") -> Renderer:
    """A ready Renderer, or ReadyError: cuda (no torch or no CUDA), model (a file missing, from
    another revision or without its download record, a load that failed, no `nsfw` label),
    vram (less than min_free_gib free just before the pipeline moves to the GPU, or CUDA out of
    memory while it does). The weights load to the CPU first, so another process holding VRAM
    reads as vram, not model; a vram refusal keeps them there for its `retry`. The models are
    checked quickly (records and revisions, no hash of the ~7 GB: doctor and fetch-models hash
    them), so the start reaches its VRAM check soon after the mind made room."""
    try:
        import torch
    except ImportError as e:
        raise ReadyError("cuda") from e
    if device != "cuda" or not torch.cuda.is_available():
        raise ReadyError("cuda")
    started = time.perf_counter()
    for name in models.MODEL_PINS:
        if any(ok is not True for ok, _ in models.check(data, name, quick=True)):
            raise ReadyError("model", fields={"check_ms": _ms(time.perf_counter() - started),
                                              "load_ms": 0})
    checked = time.perf_counter()
    try:
        import diffusers
        from diffusers import DPMSolverMultistepScheduler, StableDiffusionXLPipeline
        from transformers import AutoImageProcessor, AutoModelForImageClassification
        pipe = StableDiffusionXLPipeline.from_pretrained(
            models.model_dir(data, "realvisxl"), torch_dtype=torch.float16, variant="fp16",
            use_safetensors=True, local_files_only=True)
        pipe.scheduler = DPMSolverMultistepScheduler.from_config(
            pipe.scheduler.config, use_karras_sigmas=True)
        nsfw_dir = models.model_dir(data, "nsfw")
        processor = AutoImageProcessor.from_pretrained(nsfw_dir, local_files_only=True)
        classifier = AutoModelForImageClassification.from_pretrained(
            nsfw_dir, local_files_only=True).eval()
    except Exception as e:
        raise ReadyError("model", fields={"check_ms": _ms(checked - started),
                                          "load_ms": _ms(time.perf_counter() - checked)}) from e
    labels ={str(v).lower(): int(k) for k, v in classifier.config.id2label.items()}
    timings = {"check_ms": _ms(checked - started), "load_ms": _ms(time.perf_counter() - checked)}
    if "nsfw" not in labels:
        raise ReadyError("model", fields=timings)
    loaded = _OnCpu(pipe, processor, classifier, labels["nsfw"], torch,
                    {"torch": torch.__version__, "diffusers": diffusers.__version__})
    return _to_gpu(loaded, min_free_gib, timings, wait_s=0.0)


class _OnCpu:
    """What a start loaded before its VRAM check, kept for a retry after a vram refusal."""

    def __init__(self, pipe, processor, classifier, nsfw_index: int, torch, versions: dict):
        self.pipe, self.processor, self.classifier = pipe, processor, classifier
        self.nsfw_index, self.torch, self.versions = nsfw_index, torch, versions


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
    return Renderer(pipe, c.processor, c.classifier, c.nsfw_index, torch, info)
