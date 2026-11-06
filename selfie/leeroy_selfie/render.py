"""The renderer: RealVisXL on the GPU, the NSFW classifier on the CPU. torch, diffusers and
transformers are imported inside the functions that need them, so the CLI and the default test
suite never load them."""
import os
import time
from pathlib import Path

from . import models
from .fit import fit
from .protocol import Render, RenderFailed


class ReadyError(Exception):
    """The worker cannot become ready; `reason` is the protocol's error name."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


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


def load(data: Path, *, min_free_gib: float, device: str = "cuda") -> Renderer:
    """A ready Renderer, or ReadyError: cuda (no torch or no CUDA), vram (less than
    min_free_gib free), model (a file missing or changed, a load that failed, no `nsfw`
    label)."""
    try:
        import torch
    except ImportError as e:
        raise ReadyError("cuda") from e
    if device != "cuda" or not torch.cuda.is_available():
        raise ReadyError("cuda")
    free_gib = torch.cuda.mem_get_info()[0] / 2**30
    if free_gib < min_free_gib:
        raise ReadyError("vram")
    for name in models.MODEL_PINS:
        if any(ok is not True for ok, _ in models.check(data, name)):
            raise ReadyError("model")
    try:
        import diffusers
        from diffusers import DPMSolverMultistepScheduler, StableDiffusionXLPipeline
        from transformers import AutoImageProcessor, AutoModelForImageClassification
        pipe = StableDiffusionXLPipeline.from_pretrained(
            models.model_dir(data, "realvisxl"), torch_dtype=torch.float16, variant="fp16",
            use_safetensors=True, local_files_only=True)
        pipe.scheduler = DPMSolverMultistepScheduler.from_config(
            pipe.scheduler.config, use_karras_sigmas=True)
        pipe = pipe.to("cuda")
        pipe.set_progress_bar_config(disable=True)
        nsfw_dir = models.model_dir(data, "nsfw")
        processor = AutoImageProcessor.from_pretrained(nsfw_dir, local_files_only=True)
        classifier = AutoModelForImageClassification.from_pretrained(
            nsfw_dir, local_files_only=True).eval()
    except Exception as e:
        raise ReadyError("model") from e
    labels = {str(v).lower(): int(k) for k, v in classifier.config.id2label.items()}
    if "nsfw" not in labels:
        raise ReadyError("model")
    info = {"device": "cuda", "free_gib": round(free_gib, 1),
            "versions": {"torch": torch.__version__, "diffusers": diffusers.__version__}}
    return Renderer(pipe, processor, classifier, labels["nsfw"], torch, info)
