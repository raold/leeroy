import sys
import types

import pytest

from leeroy_selfie import models, nsfw

from fakes import make_gate


@pytest.mark.parametrize("score,flagged", [(0.0, False), (0.8999, False), (0.9, True), (1.0, True)])
def test_the_gate_flags_a_score_at_or_over_its_one_limit(score, flagged):
    assert nsfw.LIMIT == 0.9
    assert make_gate(nsfw=score).check("image") == (flagged, score)


def stub_classifier(monkeypatch, labels):
    classifier = types.SimpleNamespace(config=types.SimpleNamespace(id2label=labels))
    classifier.eval = lambda: classifier
    monkeypatch.setitem(sys.modules, "torch", types.SimpleNamespace())
    monkeypatch.setitem(sys.modules, "transformers", types.SimpleNamespace(
        AutoImageProcessor=types.SimpleNamespace(from_pretrained=lambda p, **kw: "proc"),
        AutoModelForImageClassification=types.SimpleNamespace(
            from_pretrained=lambda p, **kw: classifier)))
    monkeypatch.setattr(models, "model_dir", lambda data, name: data / name)


def test_load_finds_the_nsfw_label_whatever_its_case(tmp_path, monkeypatch):
    stub_classifier(monkeypatch, {0: "normal", 1: "NSFW"})
    gate = nsfw.load(tmp_path)
    assert (gate.processor, gate.index) == ("proc", 1)


def test_a_classifier_without_an_nsfw_label_never_makes_a_gate(tmp_path, monkeypatch):
    stub_classifier(monkeypatch, {0: "a", 1: "b"})
    with pytest.raises(ValueError):
        nsfw.load(tmp_path)
