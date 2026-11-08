from types import SimpleNamespace

import pytest

from leeroy_selfie import similarity
from fakes import FakeImage, make_renderer, make_torch
from test_render import grid


def test_cosine_is_normalized_and_reports_the_most_similar_pair():
    assert similarity.summarize([[3, 0], [0, 7], [2, 0]]) == {
        "code": "ok", "pairs": 3, "max": 1.0, "mean": .3333, "pair": [0, 2]}
    assert similarity.summarize([[1], [-1]])["max"] == -1
    assert similarity.summarize([[1]]) == {"code": "too_few_tiles"}


@pytest.mark.parametrize("vectors", [[[0, 0], [1, 0]], [[float("nan")], [1]],
                                     [[float("inf")], [1]], [[1, 2], [1]], [[], []]])
def test_invalid_vectors_fail_closed(vectors):
    with pytest.raises(ValueError):
        similarity.summarize(vectors)


def test_an_absent_encoder_or_single_tile_never_loads_anything():
    assert similarity.measure(object(), object(), ["a", "b"]) == {"code": "encoder_unavailable"}
    assert similarity.measure(object(), object(), ["a"]) == {"code": "too_few_tiles"}


def test_existing_encoder_reused_with_own_device_dtype_no_grad_and_head_crop(monkeypatch):
    torch = make_torch()
    crops, placements, calls = [], [], []
    monkeypatch.setattr(similarity.jpeg, "open_rgb", lambda path: FakeImage())

    class Tensor:
        def to(self, **kw):
            placements.append(kw)
            return self

        def float(self): return self
        def cpu(self): return self
        def tolist(self): return [1.0, 2.0]

    class Encoder:
        device, dtype = "cpu", "fp16"

        def __call__(self, pixels):
            assert torch.grad_is_off()
            calls.append(pixels)
            return SimpleNamespace(image_embeds=[pixels])

    def processor(*, images, return_tensors):
        assert return_tensors == "pt" and torch.grad_is_off()
        crops.append(images.box)
        return SimpleNamespace(pixel_values=Tensor())

    pipe = SimpleNamespace(image_encoder=Encoder(), feature_extractor=processor)
    answer = similarity.measure(pipe, torch, ["first", "second"])
    assert answer == {"code": "ok", "pairs": 1, "max": 1.0, "mean": 1.0, "pair": [0, 1]}
    assert crops == [(128, 51, 896, 819)] * 2
    assert placements == [{"device": "cpu", "dtype": "fp16"}] * 2 and len(calls) == 2
    assert not torch.grad_is_off()


def test_failed_encoding_is_a_private_skip_and_does_not_discard_grid(monkeypatch, tmp_path):
    r = make_renderer()
    r.pipe.image_encoder = object()
    r.pipe.feature_extractor = object()
    monkeypatch.setattr(similarity.jpeg, "open_rgb", lambda path: (_ for _ in ()).throw(
        ValueError("private filename")))
    monkeypatch.setattr("leeroy_selfie.render.grid.compose",
                        lambda req: {"width": 512, "height": 256, "seconds": .01})
    answer = r.grid(grid(tiles=[tmp_path / "a", tmp_path / "b"], out=tmp_path / "out"))
    assert answer["ok"] is True
    assert answer["similarity"] == {"code": "encoder_failed"}
    assert "private" not in str(answer)
