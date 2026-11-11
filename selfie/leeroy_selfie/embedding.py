"""Chosen-photo diagnostics with the already fetched IP-Adapter image encoder.

No diffusion pipeline, classifier, model fetch or generated image. The approximate
head crop is not face detection and its similarity is not identity verification.
"""
import math
import time
from . import jpeg, models
from .render import ReadyError
from .similarity import HEAD_CROP

DIMENSIONS = 1024
MODEL = "ipadapter-vith-head-v1"


def encode(encoder, processor, torch, path):
    image = jpeg.open_rgb(path)
    w, h = image.size
    crop = image.crop(tuple(round(n * extent) for n, extent in zip(HEAD_CROP, (w,h,w,h))))
    with torch.no_grad():
        pixels = processor(images=crop, return_tensors="pt").pixel_values
        pixels = pixels.to(device=encoder.device, dtype=encoder.dtype)
        vector = encoder(pixels).image_embeds[0].float().cpu().tolist()
    if len(vector) != DIMENSIONS or any(type(v) not in (int,float) or not math.isfinite(v) for v in vector):
        raise ValueError("embedding_invalid")
    norm = math.sqrt(sum(v*v for v in vector))
    if not math.isfinite(norm) or norm <= 0:
        raise ValueError("embedding_invalid")
    return [v/norm for v in vector]


class Embedder:
    identity = False
    embedding_only = True

    def __init__(self, encoder, processor, torch, info):
        self.encoder, self.processor, self.torch, self.info = encoder, processor, torch, info

    def embed(self, req):
        started = time.perf_counter()
        try:
            vector = encode(self.encoder, self.processor, self.torch, req.ref)
        except self.torch.cuda.OutOfMemoryError:
            self.encoder.to(device="cpu", dtype=self.torch.float32)
            self.torch.cuda.empty_cache()
            vector = encode(self.encoder, self.processor, self.torch, req.ref)
        return {"model": MODEL, "vector": vector, "device": str(self.encoder.device),
                "seconds": round(time.perf_counter()-started, 3)}


def load(root, *, min_free_gib):
    started = time.perf_counter()
    if any(ok is not True for ok, _ in models.check(root, "ipadapter", quick=True)):
        raise ReadyError("encoder_unavailable")
    checked = time.perf_counter()
    try:
        import torch
        from transformers import CLIPImageProcessor, CLIPVisionModelWithProjection
        encoder = CLIPVisionModelWithProjection.from_pretrained(
            models.model_dir(root, "ipadapter") / "models" / "image_encoder",
            torch_dtype=torch.float32, local_files_only=True).eval()
        processor = CLIPImageProcessor(size=224, crop_size=224)
        free = torch.cuda.mem_get_info()[0]/2**30 if torch.cuda.is_available() else 0
        if free >= min_free_gib:
            try:
                encoder.to(device="cuda", dtype=torch.float16)
            except torch.cuda.OutOfMemoryError:
                encoder.to(device="cpu", dtype=torch.float32)
                torch.cuda.empty_cache()
        return Embedder(encoder, processor, torch, {
            "device": str(encoder.device), "free_gib": free, "versions": {"torch": str(torch.__version__)},
            "check_ms": round((checked-started)*1000), "load_ms": round((time.perf_counter()-checked)*1000)})
    except Exception:
        raise ReadyError("encoder_failed") from None
