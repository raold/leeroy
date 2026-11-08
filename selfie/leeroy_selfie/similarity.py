"""Uncalibrated guild tile similarity using an already loaded IP-Adapter encoder.

The fixed head-and-shoulders crop is approximate, not face detection or identity proof.
This module never imports a model loader, downloads weights or retains embeddings.
"""
import math
from itertools import combinations

from . import jpeg

HEAD_CROP = (0.125, 0.05, 0.875, 0.8)


def summarize(vectors: list[list[float]]) -> dict:
    normalized = []
    for vector in vectors:
        if not vector or not all(math.isfinite(x) for x in vector):
            raise ValueError("invalid embedding")
        norm = math.sqrt(sum(x * x for x in vector))
        if norm == 0 or not math.isfinite(norm):
            raise ValueError("invalid embedding")
        normalized.append([x / norm for x in vector])
    pairs = []
    for i, j in combinations(range(len(normalized)), 2):
        if len(normalized[i]) != len(normalized[j]):
            raise ValueError("embedding dimensions")
        cosine = max(-1.0, min(1.0, sum(a * b for a, b in
                                       zip(normalized[i], normalized[j]))))
        pairs.append((cosine, i, j))
    if not pairs:
        return {"code": "too_few_tiles"}
    maximum, i, j = max(pairs)
    return {"code": "ok", "pairs": len(pairs), "max": round(maximum, 4),
            "mean": round(sum(p[0] for p in pairs) / len(pairs), 4), "pair": [i, j]}


def measure(pipe, torch, paths) -> dict:
    """Bounded by grid's 16 tiles. A diagnostic failure never discards a good grid."""
    if len(paths) < 2:
        return {"code": "too_few_tiles"}
    encoder = getattr(pipe, "image_encoder", None)
    processor = getattr(pipe, "feature_extractor", None)
    if encoder is None or processor is None:
        return {"code": "encoder_unavailable"}
    try:
        vectors = []
        for path in paths:
            image = jpeg.open_rgb(path)
            w, h = image.size
            crop = image.crop(tuple(round(n * extent) for n, extent in
                                    zip(HEAD_CROP, (w, h, w, h))))
            with torch.no_grad():
                pixels = processor(images=crop, return_tensors="pt").pixel_values
                pixels = pixels.to(device=encoder.device, dtype=encoder.dtype)
                vector = encoder(pixels).image_embeds[0].float().cpu().tolist()
            vectors.append(vector)
        return summarize(vectors)
    except Exception:
        # No filenames, embeddings or exception text cross the protocol or logs.
        return {"code": "encoder_failed"}
