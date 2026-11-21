"""The NSFW gate, whole: the pinned Falconsai classifier (models.MODEL_PINS["nsfw"]) scores every
image the worker would write, and one scoring LIMIT or more is never written. This file is the
only place the threshold, the classifier and the decision live. Nothing else compares a score:
the worker writes only what `check` passed, and the mind trusts the answer's `flagged`.
torch and transformers are imported inside load(), so the CLI and the tests never load them."""
from pathlib import Path

from . import models

LIMIT = 0.9  # an image scoring this or more is never written


class Gate:
    def __init__(self, processor, classifier, index: int, torch):
        self.processor, self.classifier, self.index, self.torch = processor, classifier, index, torch

    def score(self, image) -> float:
        """The classifier's NSFW probability for `image`, 0-1."""
        with self.torch.no_grad():
            logits = self.classifier(**self.processor(images=image, return_tensors="pt")).logits
        return float(self.torch.softmax(logits, dim=-1)[0, self.index])

    def check(self, image) -> tuple[bool, float]:
        """(flagged, score): flagged when the score reaches LIMIT, and then nothing is written."""
        score = self.score(image)
        return score >= LIMIT, score


def load(data: Path) -> Gate:
    """The gate on the fetched classifier, on the CPU. Raises when it is missing, fails to load
    or has no `nsfw` label: the caller refuses to become ready (ReadyError "model")."""
    import torch
    from transformers import AutoImageProcessor, AutoModelForImageClassification
    folder = models.model_dir(data, "nsfw")
    processor = AutoImageProcessor.from_pretrained(folder, local_files_only=True)
    classifier = AutoModelForImageClassification.from_pretrained(folder, local_files_only=True).eval()
    labels = {str(v).lower(): int(k) for k, v in classifier.config.id2label.items()}
    if "nsfw" not in labels:
        raise ValueError("the classifier has no nsfw label")
    return Gate(processor, classifier, labels["nsfw"], torch)
